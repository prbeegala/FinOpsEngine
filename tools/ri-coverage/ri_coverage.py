"""
ri-coverage — Reservation / Savings Plan coverage intelligence.

Part of the FinOps Engine — Reservation / Savings Plan coverage intelligence.

What it does
============
1. Pulls last N months of *PAYG (OnDemand)* virtual-machine consumption from
   Azure Cost Management (`/query` REST API) across all in-scope subscriptions.
2. Aggregates by (VM family, region) — the natural unit for a Reservation or
   Compute Savings Plan commitment.
3. Computes month-over-month stability (coefficient of variation) per
   family × region group.
4. Estimates potential savings against published list-price reductions for
   Compute SP / RI 1Y / RI 3Y (rates are blended estimates — see README).
5. Risk-scores each recommendation against:
     - Workload stability (CV).
     - Cumulative refund-buffer exposure (the configured cancellation-exposure cap).
     - Family generation hygiene (deprecated families flagged).
6. Produces a **shortlist** of recommendations that fits inside the
   refund-buffer guardrail, plus the full coverage-gap map for context.

Usage
-----
    python ri_coverage.py \
        --subs <subId>[,<subId>...] \
        --months 3 \
        --refund-buffer-gbp 5000 \
        --out-dir ./out/ri-coverage

Auth: relies on `az login`. Uses `az rest` to call Cost Management (avoids
shell-quoting issues seen in the rightsizing engine).

Reservation-utilisation pull is *optional* and skipped automatically if the
caller lacks `Microsoft.Capacity/reservationOrders/read`. The headline
output (commitable opportunity) does not depend on it.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# HTML report sink (shared utility — no third-party deps)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from html_sink import write_html, write_index  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Published savings rates — blended estimates over Linux / Windows, current
# generations. Real per-SKU savings vary; these are the headline figures
# Microsoft publishes. The tool clearly labels output as an *estimate* —
# always verify per SKU at commitment time.
SAVINGS_RATES = {
    "compute_sp_1y": 0.17,
    "compute_sp_3y": 0.28,
    "vm_ri_1y":      0.30,
    "vm_ri_3y":      0.50,
}

# Families likely deprecated / non-current-gen — flagged but not auto-excluded.
DEPRECATED_FAMILY_HINTS = ("A Series", "Dv2", "Dv3", "Ev3", "Fsv2")

# Risk thresholds on coefficient of variation of monthly £ burn.
CV_HIGH = 0.30
CV_MED = 0.15

# Worst-case cancellation fee assumed when modelling refund-buffer exposure.
CANCELLATION_FEE_PCT = 0.12

# Default % of measured PAYG burn we'd commit (variable workloads).
COMMIT_FRACTION_DEFAULT = 0.65

# ---------------------------------------------------------------------------
# Subprocess helpers (Windows-safe)
# ---------------------------------------------------------------------------

def _is_win() -> bool:
    return sys.platform.startswith("win")


def _q(s: str) -> str:
    """Quote a single arg for cmd.exe."""
    if not s or any(c in s for c in ' \t"^&|<>()'):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def az(args: list[str]) -> dict:
    cmd = ["az", *args, "-o", "json"]
    if _is_win():
        flat = [(" ".join(a.split()) if "\n" in a else a) for a in cmd]
        p = subprocess.run(" ".join(_q(a) for a in flat),
                           capture_output=True, text=True, shell=True)
    else:
        p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"az failed: {' '.join(cmd[:6])}…\n{p.stderr[:600]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def az_rest(method: str, url: str, body: dict,
            max_retries: int = 6) -> dict:
    """Call ARM via `az rest`, body via temp file. Retries 429/503 w/ backoff."""
    import time, random
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(body, f)
        body_path = f.name
    try:
        last_err = None
        for attempt in range(max_retries):
            try:
                return az([
                    "rest", "--method", method, "--url", url,
                    "--body", f"@{body_path}",
                    "--headers", "Content-Type=application/json",
                ])
            except RuntimeError as e:
                msg = str(e)
                last_err = e
                if "429" in msg or "Too Many Requests" in msg or "503" in msg:
                    delay = min(60, (2 ** attempt) + random.uniform(0, 1.5))
                    print(f"    … rate-limited, sleeping {delay:.1f}s "
                          f"(attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                raise
        raise last_err if last_err else RuntimeError("az rest failed")
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cost Management query
# ---------------------------------------------------------------------------

@dataclass
class UsageRow:
    sub_id: str
    sub_name: str = ""
    month: str = ""
    location: str = ""
    family: str = ""
    cost_gbp: float = 0.0


def fetch_payg_vm_costs(sub_id: str, months: int) -> list[UsageRow]:
    end = datetime.now(timezone.utc).replace(day=1) - timedelta(seconds=1)
    cur = end.replace(day=1)
    for _ in range(months - 1):
        cur = (cur - timedelta(days=1)).replace(day=1)
    start = cur

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start.strftime("%Y-%m-%dT00:00:00Z"),
            "to":   end.strftime("%Y-%m-%dT23:59:59Z"),
        },
        "dataset": {
            "granularity": "Monthly",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [
                {"type": "Dimension", "name": "ResourceLocation"},
                {"type": "Dimension", "name": "MeterSubCategory"},
            ],
            "filter": {"and": [
                {"dimensions": {"name": "MeterCategory",
                                "operator": "In",
                                "values": ["Virtual Machines"]}},
                {"dimensions": {"name": "PricingModel",
                                "operator": "In",
                                "values": ["OnDemand"]}},
            ]},
        },
    }
    url = (f"https://management.azure.com/subscriptions/{sub_id}"
           f"/providers/Microsoft.CostManagement/query"
           f"?api-version=2023-11-01")
    try:
        resp = az_rest("POST", url, body)
    except RuntimeError as e:
        print(f"  ! Cost Management query failed for {sub_id}: {e}")
        return []

    rows: list[UsageRow] = []
    cols = [c["name"] for c in resp.get("properties", {}).get("columns", [])]
    for r in resp.get("properties", {}).get("rows", []):
        d = dict(zip(cols, r))
        ud = str(d.get("UsageDate", ""))
        month = f"{ud[:4]}-{ud[4:6]}" if len(ud) >= 6 else ""
        rows.append(UsageRow(
            sub_id=sub_id,
            month=month,
            location=str(d.get("ResourceLocation", "")).lower(),
            family=str(d.get("MeterSubCategory", "")).strip(),
            cost_gbp=float(d.get("Cost", 0.0) or 0.0),
        ))
    return rows


def fetch_sub_name(sub_id: str) -> str:
    try:
        resp = az(["account", "show", "--subscription", sub_id])
        return resp.get("name", sub_id)
    except RuntimeError:
        return sub_id


# ---------------------------------------------------------------------------
# Aggregation + scoring
# ---------------------------------------------------------------------------

@dataclass
class Group:
    family: str
    location: str
    monthly: dict[str, float] = field(default_factory=dict)
    sub_ids: set = field(default_factory=set)

    @property
    def avg_monthly(self) -> float:
        v = list(self.monthly.values())
        return sum(v) / len(v) if v else 0.0

    @property
    def cv(self) -> float:
        v = list(self.monthly.values())
        if len(v) < 2 or self.avg_monthly == 0:
            return 0.0
        return statistics.stdev(v) / self.avg_monthly

    @property
    def annual_payg(self) -> float:
        return self.avg_monthly * 12.0


def aggregate(rows: list[UsageRow]) -> dict:
    out: dict = {}
    for r in rows:
        if not r.family:
            continue
        key = (r.family, r.location)
        g = out.setdefault(key, Group(family=r.family, location=r.location))
        g.monthly[r.month] = g.monthly.get(r.month, 0.0) + r.cost_gbp
        g.sub_ids.add(r.sub_id)
    return out


@dataclass
class Recommendation:
    family: str
    location: str
    sub_count: int
    avg_monthly_payg: float
    annual_payg: float
    cv: float
    stability: str
    commit_fraction: float
    recommended_product: str
    annual_commit: float
    annual_savings: float
    cancellation_exposure: float
    deprecated_family: bool
    risk: str
    rationale: str


def score(g: Group) -> Recommendation:
    cv = g.cv
    if cv < CV_MED:
        stability, fraction, product = "STABLE", 0.80, "VM RI 1Y"
    elif cv < CV_HIGH:
        stability, fraction, product = "VARIABLE", COMMIT_FRACTION_DEFAULT, "Compute SP 1Y"
    else:
        stability, fraction, product = "UNSTABLE", 0.30, "Compute SP 1Y"

    annual_commit = g.annual_payg * fraction
    rate_key = {
        "VM RI 1Y": "vm_ri_1y",
        "VM RI 3Y": "vm_ri_3y",
        "Compute SP 1Y": "compute_sp_1y",
        "Compute SP 3Y": "compute_sp_3y",
    }[product]
    annual_savings = annual_commit * SAVINGS_RATES[rate_key]
    cancel_exp = annual_commit * CANCELLATION_FEE_PCT

    deprecated = any(h.lower() in g.family.lower()
                     for h in DEPRECATED_FAMILY_HINTS)

    bits = []
    if stability == "STABLE":
        bits.append(f"PAYG burn varies only ±{cv * 100:.0f}% MoM — high "
                    "confidence for a 1Y RI commit.")
    elif stability == "VARIABLE":
        bits.append(f"Moderate variability (±{cv * 100:.0f}%); Compute SP "
                    "gives family-flex while still locking ~17%.")
    else:
        bits.append(f"High volatility (±{cv * 100:.0f}%); recommend a small "
                    "SP covering only the stable floor (~30%).")
    if deprecated:
        bits.append(f"⚠️  Family '{g.family}' is older-gen — verify "
                    "migration roadmap before committing 3Y.")
    if g.annual_payg < 5000:
        bits.append("Annual PAYG burn under £5k — modest savings; consider "
                    "rolling into a wider Compute SP rather than per-family RI.")

    if cv < CV_MED and not deprecated:
        risk = "LOW"
    elif cv < CV_HIGH and not deprecated:
        risk = "MEDIUM"
    else:
        risk = "HIGH"

    return Recommendation(
        family=g.family, location=g.location, sub_count=len(g.sub_ids),
        avg_monthly_payg=g.avg_monthly, annual_payg=g.annual_payg,
        cv=cv, stability=stability, commit_fraction=fraction,
        recommended_product=product, annual_commit=annual_commit,
        annual_savings=annual_savings, cancellation_exposure=cancel_exp,
        deprecated_family=deprecated, risk=risk, rationale=" ".join(bits),
    )


def build_shortlist(recs, refund_buffer_gbp, min_savings_gbp: float = 50.0):
    """
    Sort by risk band then by absolute annual savings; greedy-pack into the
    refund-buffer. The savings:exposure ratio is constant across products of
    the same kind, so absolute savings is the meaningful key. Recs with
    trivial absolute savings (< min_savings_gbp/yr) are filtered out — they
    add noise without moving the £ needle and can't justify a contract.
    """
    candidates = [r for r in recs if r.annual_savings >= min_savings_gbp]
    candidates.sort(key=lambda r: (
        0 if r.risk == "LOW" else (1 if r.risk == "MEDIUM" else 2),
        -r.annual_savings,
    ))
    picked, spent = [], 0.0
    for r in candidates:
        if spent + r.cancellation_exposure <= refund_buffer_gbp:
            picked.append(r)
            spent += r.cancellation_exposure
    return picked


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def gbp(x: float) -> str:
    if abs(x) >= 1000:
        return f"£{x:,.0f}"
    return f"£{x:,.2f}"


def write_coverage_csv(path: Path, recs):
    fields = ["family", "location", "sub_count",
              "avg_monthly_payg_gbp", "annual_payg_gbp", "cv",
              "stability", "deprecated_family",
              "recommended_product", "commit_fraction",
              "annual_commit_gbp", "annual_savings_gbp",
              "cancellation_exposure_gbp", "risk", "rationale"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for r in sorted(recs, key=lambda r: -r.annual_payg):
            w.writerow([
                r.family, r.location, r.sub_count,
                f"{r.avg_monthly_payg:.2f}", f"{r.annual_payg:.2f}",
                f"{r.cv:.3f}", r.stability,
                "TRUE" if r.deprecated_family else "FALSE",
                r.recommended_product, f"{r.commit_fraction:.2f}",
                f"{r.annual_commit:.2f}", f"{r.annual_savings:.2f}",
                f"{r.cancellation_exposure:.2f}", r.risk, r.rationale,
            ])


def write_coverage_md(path: Path, recs, months: int, sub_count: int):
    total_payg = sum(r.annual_payg for r in recs)
    total_savings = sum(r.annual_savings for r in recs)
    by_risk = {"LOW": 0, "MEDIUM": 0, "HIGH": 0}
    for r in recs:
        by_risk[r.risk] += 1

    lines = [
        "# RI / Savings-Plan Coverage Map",
        "",
        f"**Generated**: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}  ",
        f"**Window**: last {months} months of PAYG VM consumption  ",
        f"**Subscriptions in scope**: {sub_count}",
        "",
        "## Headline",
        "",
        f"- Total annualised PAYG VM spend in scope: **{gbp(total_payg)}**",
        f"- Family×region commitable groups identified: **{len(recs)}**",
        f"- Theoretical best-case annual savings (if 100% adopted): "
        f"**{gbp(total_savings)}** *(estimate — see assumptions)*",
        f"- LOW-risk groups: **{by_risk['LOW']}** | "
        f"MEDIUM: **{by_risk['MEDIUM']}** | HIGH: **{by_risk['HIGH']}**",
        "",
        "## Top 20 commitable groups (by annual PAYG £)",
        "",
        "| Family | Region | Subs | Mo. PAYG | Yr PAYG | CV | Stability | "
        "Product | Yr commit | Yr savings | Risk |",
        "|---|---|---:|---:|---:|---:|---|---|---:|---:|---|",
    ]
    for r in sorted(recs, key=lambda r: -r.annual_payg)[:20]:
        lines.append(
            f"| {r.family} | {r.location} | {r.sub_count} | "
            f"{gbp(r.avg_monthly_payg)} | {gbp(r.annual_payg)} | "
            f"{r.cv:.2f} | {r.stability} | {r.recommended_product} | "
            f"{gbp(r.annual_commit)} | {gbp(r.annual_savings)} | {r.risk} |"
        )
    lines += [
        "",
        "## Assumptions",
        "",
        "- Savings rates are blended estimates: Compute SP 1Y ~17%, "
        "Compute SP 3Y ~28%, VM RI 1Y ~30%, VM RI 3Y ~50%. Real per-SKU "
        "rates vary — confirm in the Azure portal at commit time.",
        f"- Commit fraction: STABLE = 80%, VARIABLE = "
        f"{int(COMMIT_FRACTION_DEFAULT * 100)}%, UNSTABLE = 30% of measured "
        "PAYG.",
        f"- Cancellation exposure modelled at "
        f"{int(CANCELLATION_FEE_PCT * 100)}% of annual commit value.",
        "- Reservation **utilisation** of existing RIs is not pulled in this "
        "run (caller lacks `Microsoft.Capacity/reservationOrders/read`). "
        "Coverage gap is therefore relative to *measured PAYG*, not "
        "*PAYG net of partial RI cover*.",
        "",
        "_Generated by `ri-coverage`._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_shortlist_md(path: Path, shortlist, all_recs, buffer_gbp):
    spent = sum(r.cancellation_exposure for r in shortlist)
    savings = sum(r.annual_savings for r in shortlist)
    commit = sum(r.annual_commit for r in shortlist)

    lines = [
        "# RI / Savings-Plan Risk-Scored Shortlist",
        "",
        f"**Generated**: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}  ",
        f"**Refund-buffer guardrail**: {gbp(buffer_gbp)} of cancellation "
        "exposure (the configured cancellation-exposure cap)",
        "",
        "## Headline",
        "",
        f"- Recommendations fitting inside the buffer: **{len(shortlist)}** "
        f"of {len(all_recs)} candidates",
        f"- Total annual commitment proposed: **{gbp(commit)}**",
        f"- Modelled annual savings: **{gbp(savings)}**",
        f"- Cancellation-exposure spent: **{gbp(spent)}** "
        f"of {gbp(buffer_gbp)} buffer "
        f"({(spent / buffer_gbp * 100 if buffer_gbp else 0):.0f}%)",
        "",
        "## Recommended commitments (within buffer)",
        "",
        "| # | Family | Region | Product | Yr commit | Yr savings | "
        "Cancel exposure | Risk | Rationale |",
        "|---:|---|---|---|---:|---:|---:|---|---|",
    ]
    for i, r in enumerate(shortlist, 1):
        lines.append(
            f"| {i} | {r.family} | {r.location} | {r.recommended_product} | "
            f"{gbp(r.annual_commit)} | {gbp(r.annual_savings)} | "
            f"{gbp(r.cancellation_exposure)} | {r.risk} | {r.rationale} |"
        )

    rejected = [r for r in all_recs if r not in shortlist
                and r.annual_savings > 0]
    if rejected:
        lines += [
            "",
            "## Candidates excluded by the guardrail",
            "",
            "These have headline savings but adding them would push "
            "cumulative cancellation exposure beyond the buffer. Revisit "
            "when the buffer increases or move into the next FY plan.",
            "",
            "| Family | Region | Yr savings | Cancel exposure | Risk |",
            "|---|---|---:|---:|---|",
        ]
        for r in sorted(rejected, key=lambda r: -r.annual_savings)[:15]:
            lines.append(
                f"| {r.family} | {r.location} | {gbp(r.annual_savings)} | "
                f"{gbp(r.cancellation_exposure)} | {r.risk} |"
            )

    lines += [
        "",
        "## How to read this",
        "",
        "1. Take the top recommendation and confirm savings in the Azure "
        "portal *Reservations → Recommendations* view (per SKU).",
        "2. Cross-check against `rightsizing-peak` for the same family "
        "+ region — never reserve a workload that should be downsized first.",
        "3. Apply the buffer in stages (half this quarter, half next) to "
        "avoid concentrating cancellation risk.",
        "",
        "_Generated by `ri-coverage`._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(subs, months, refund_buffer, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")

    print(f"[engine] Pulling {months}-month PAYG VM costs across "
          f"{len(subs)} sub(s)…")
    all_rows: list[UsageRow] = []
    for sid in subs:
        name = fetch_sub_name(sid)
        rows = fetch_payg_vm_costs(sid, months)
        for r in rows:
            r.sub_name = name
        print(f"  ✓ {name}: {len(rows)} rows")
        all_rows.extend(rows)

    if not all_rows:
        print("[engine] No PAYG VM consumption returned. Exiting.")
        return

    groups = aggregate(all_rows)
    recs = [score(g) for g in groups.values() if g.annual_payg > 0]
    print(f"[engine] {len(recs)} family×region groups identified.")
    shortlist = build_shortlist(recs, refund_buffer)
    print(f"[engine] Shortlist within £{refund_buffer:.0f} buffer: "
          f"{len(shortlist)} recommendation(s).")

    coverage_csv  = out_dir / f"ri-coverage-{date}.csv"
    coverage_md   = out_dir / f"ri-coverage-{date}.md"
    coverage_html = out_dir / f"ri-coverage-{date}.html"
    shortlist_md  = out_dir / f"ri-shortlist-{date}.md"
    shortlist_html = out_dir / f"ri-shortlist-{date}.html"
    write_coverage_csv(coverage_csv, recs)
    write_coverage_md(coverage_md, recs, months, len(subs))
    write_html(coverage_md, coverage_html)
    write_shortlist_md(shortlist_md, shortlist, recs, refund_buffer)
    write_html(shortlist_md, shortlist_html)
    write_index(out_dir, [
        ("RI / Savings-Plan Coverage Map", coverage_html.name),
        ("RI / Savings-Plan Risk-Scored Shortlist", shortlist_html.name),
    ])

    total_savings = sum(r.annual_savings for r in shortlist)
    print(f"[engine] Done.")
    print(f"  - {coverage_md}")
    print(f"  - {coverage_html}")
    print(f"  - {coverage_csv}")
    print(f"  - {shortlist_md}")
    print(f"  - {shortlist_html}")
    print(f"[engine] Modelled annual savings on shortlist: "
          f"£{total_savings:,.0f}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--subs",
                     help="Comma-separated subscription IDs (or names).")
    src.add_argument("--all-subs", action="store_true",
                     help="Run across every enabled subscription returned "
                          "by `az account list`. Pair with --exclude-subs "
                          "and/or --tenant to narrow scope.")
    ap.add_argument("--exclude-subs",
                    help="Comma-separated subscription IDs or names to "
                         "skip when --all-subs is used (e.g. sandboxes).")
    ap.add_argument("--tenant",
                    help="Limit --all-subs to a single tenant ID.")
    ap.add_argument("--include-disabled", action="store_true",
                    help="Include subscriptions whose state is not "
                         "Enabled (default: skip them).")
    ap.add_argument("--months", type=int, default=3,
                    help="Months of history to pull (default 3).")
    ap.add_argument("--refund-buffer-gbp", type=float, default=5000.0,
                    help="Cap on cumulative cancellation exposure (default 5000).")
    ap.add_argument("--out-dir", required=True, help="Output directory.")
    args = ap.parse_args()
    subs = _resolve_subs(args)
    run(subs, args.months, args.refund_buffer_gbp, Path(args.out_dir))


def _resolve_subs(args) -> list[str]:
    """Return the list of subscription IDs to run against.

    Honours --subs (explicit list) or --all-subs (enumerate via
    `az account list`), with optional --tenant / --exclude-subs /
    --include-disabled filters. Raises SystemExit with a human-readable
    message if the resolution yields nothing.
    """
    if args.subs:
        return [s.strip() for s in args.subs.split(",") if s.strip()]
    state_filter = "" if args.include_disabled else "[?state=='Enabled']"
    query = state_filter + ".{id:id,name:name,tenantId:tenantId}"
    accounts = az(["account", "list", "--query", query, "-o", "json"])
    if args.tenant:
        accounts = [a for a in accounts if a.get("tenantId") == args.tenant]
    excludes = set()
    if args.exclude_subs:
        excludes = {s.strip() for s in args.exclude_subs.split(",")
                    if s.strip()}
    ids = [a["id"] for a in accounts
           if a["id"] not in excludes
           and a.get("name", "") not in excludes]
    if not ids:
        raise SystemExit(
            "--all-subs resolved zero subscriptions. Check `az account "
            "list` and any --tenant / --exclude-subs / "
            "--include-disabled filters.")
    print(f"[engine] --all-subs resolved {len(ids)} subscription(s).")
    return ids


if __name__ == "__main__":
    main()
