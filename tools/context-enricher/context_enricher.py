"""
context-enricher — Part of the FinOps Engine — context enrichment & per-owner remediation queue.

What it does
============
Reads any of the engine output CSVs from Phases 1–3, joins each finding with
its resource-tag context via Resource Graph (owner, criticality, environment,
cost-centre, application), classifies each finding's *approve-readiness*
confidence, and emits per-owner GitHub Issue body templates so the next
nightly run can post them as Issues / PRs against your FinOps repo with no
spreadsheet round-trip.

Confidence model (deterministic, by design — no LLM here)
---------------------------------------------------------
- HIGH:  has owner tag AND (criticality OR environment) AND monthly_gbp ≥ £100
- MED:   has owner OR criticality, monthly_gbp ≥ £25
- LOW:   no useful tags OR monthly_gbp < £25

LOW findings are still enumerated in the report but are *not* auto-issued —
they belong in the platform-team backlog as tagging-debt, not as remediation
work.

Usage
-----
    python context_enricher.py \
        --hidden-waste-csv ./out/hidden-waste/hidden-waste-<date>.csv \
        --rightsizing-csv ./out/peak-rightsizing/tenant-peak-rightsizing-savings-<date>.csv \
        --out-dir ./out/enriched

`az login` required. Tool batches 100 resource IDs per Resource Graph query
and retries 429/503 with exponential backoff (same pattern as Phases 1–3).
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# HTML report sink (shared utility — no third-party deps)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from html_sink import write_html, write_index  # noqa: E402
from finops_currency import detect_currency  # noqa: E402

# ---------------------------------------------------------------------------
# Display currency (overridden in main() from CLI / billing-account auto-detect).
# Defaults to GBP so existing fixtures / sample snapshots keep passing.
# ---------------------------------------------------------------------------
CURRENCY: str = "£"
CURRENCY_ISO: str = "GBP"

# ---------------------------------------------------------------------------
# Tag conventions — case-insensitive lookup, first match wins.
# Single source of truth lives in ``tools/tag_keys.py``; re-exported
# here so existing call-sites (and ``from context_enricher import
# ENVIRONMENT_KEYS``) keep working.
# ---------------------------------------------------------------------------

from tag_keys import (  # noqa: E402
    OWNER_KEYS,
    CRITICALITY_KEYS,
    ENVIRONMENT_KEYS,
    COSTCENTRE_KEYS,
    APP_KEYS,
    TAG_PLACEHOLDERS,
)

# Confidence thresholds (£ / month).
HIGH_GBP_FLOOR = 100.0
MED_GBP_FLOOR  = 25.0

# Resource Graph batch size.
GRAPH_BATCH = 100

# ---------------------------------------------------------------------------
# Shell helpers (mirrors phases 1–3)
# ---------------------------------------------------------------------------

def _is_win() -> bool:
    return sys.platform.startswith("win")


def _q(s: str) -> str:
    if not s or any(c in s for c in ' \t"^&|<>()'):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def az(args: list[str]) -> dict | list:
    cmd = ["az", *args, "-o", "json"]
    if _is_win():
        flat = [(" ".join(a.split()) if "\n" in a else a) for a in cmd]
        p = subprocess.run(" ".join(_q(a) for a in flat),
                           capture_output=True, text=True, shell=True)
    else:
        p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"az failed: {' '.join(cmd[:6])}...\n{p.stderr[:600]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def az_rest(method: str, url: str, body: dict, max_retries: int = 6) -> dict:
    import time, random
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(body, f)
        body_path = f.name
    try:
        last = None
        for attempt in range(max_retries):
            try:
                return az(["rest", "--method", method, "--url", url,
                           "--body", f"@{body_path}",
                           "--headers", "Content-Type=application/json"])
            except RuntimeError as e:
                msg = str(e); last = e
                if "429" in msg or "Too Many Requests" in msg or "503" in msg:
                    delay = min(60, (2 ** attempt) + random.uniform(0, 1.5))
                    print(f"    ... rate-limited, sleeping {delay:.1f}s "
                          f"(attempt {attempt + 1}/{max_retries})", flush=True)
                    time.sleep(delay)
                    continue
                raise
        raise last if last else RuntimeError("az rest failed")
    finally:
        try: os.unlink(body_path)
        except OSError: pass


# ---------------------------------------------------------------------------
# Resource Graph: tags lookup, batched
# ---------------------------------------------------------------------------

GRAPH_URL = ("https://management.azure.com/providers/Microsoft.ResourceGraph"
             "/resources?api-version=2022-10-01")


def fetch_tags_for_ids(ids: list[str]) -> dict[str, dict[str, str]]:
    """Return {resource_id (lower) -> {tag_key_lower: value}} for the given ids."""
    out: dict[str, dict[str, str]] = {}
    for i in range(0, len(ids), GRAPH_BATCH):
        batch = ids[i:i + GRAPH_BATCH]
        in_clause = ",".join("'" + b.lower().replace("'", "''") + "'" for b in batch)
        kql = (f"Resources | where tolower(id) in ({in_clause}) "
               f"| project id=tolower(id), tags")
        body = {"query": kql, "options": {"$top": GRAPH_BATCH}}
        try:
            resp = az_rest("POST", GRAPH_URL, body)
        except RuntimeError as e:
            print(f"  ! graph batch failed ({len(batch)} ids): {e}", flush=True)
            continue
        for row in resp.get("data", []):
            rid = (row.get("id") or "").lower()
            tags = row.get("tags") or {}
            out[rid] = {str(k).lower(): str(v) for k, v in tags.items()}
        print(f"  [ok] tag lookup: {min(i + GRAPH_BATCH, len(ids))}/{len(ids)}",
              flush=True)
    return out


def fetch_vm_id_by_name(sub_id: str, vm_name: str) -> str | None:
    """Resolve a VM's full resource id from its short name (rightsizing CSV)."""
    kql = (f"Resources | where type =~ 'microsoft.compute/virtualmachines' "
           f"and subscriptionId =~ '{sub_id}' and name =~ '{vm_name}' "
           f"| project id | take 1")
    body = {"query": kql}
    try:
        resp = az_rest("POST", GRAPH_URL, body)
    except RuntimeError:
        return None
    rows = resp.get("data", [])
    return (rows[0].get("id") if rows else None)


# ---------------------------------------------------------------------------
# CSV loaders
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    source: str                 # 'hidden_waste' / 'rightsizing'
    category: str
    sub_id: str
    sub_name: str
    resource_group: str
    name: str
    resource_id: str
    monthly_gbp: float
    annual_savings_gbp: float = 0.0
    extra: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    owner: str = ""
    criticality: str = ""
    environment: str = ""
    cost_centre: str = ""
    application: str = ""
    confidence: str = "LOW"
    rationale: str = ""
    owner_source: str = "unrouted"  # 'yaml' | 'tag' | 'codeowners' | 'unrouted'


def load_hidden_waste(path: Path) -> list[Finding]:
    out: list[Finding] = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(Finding(
                source="hidden_waste",
                category=r.get("category", ""),
                sub_id=r.get("sub_id", ""),
                sub_name=r.get("sub_name", ""),
                resource_group=r.get("resource_group", ""),
                name=r.get("name", ""),
                resource_id=r.get("resource_id", ""),
                monthly_gbp=float(r.get("monthly_gbp") or 0.0),
                extra=r.get("extra", ""),
            ))
    return out


def load_rightsizing(path: Path) -> list[Finding]:
    """Load the tenant rightsizing-savings CSV."""
    out: list[Finding] = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            save = float(r.get("SaveGBP") or 0.0)
            out.append(Finding(
                source="rightsizing",
                category=f"Rightsize {r.get('Cur', '')} → {r.get('Next', '')}",
                sub_id="",  # filled in via lookup below
                sub_name=r.get("Sub", ""),
                resource_group="",
                name=r.get("VM", ""),
                resource_id="",
                monthly_gbp=save / 12.0,
                annual_savings_gbp=save,
                extra=f"{r.get('OS','')} / {r.get('Loc','')}",
            ))
    return out


# ---------------------------------------------------------------------------
# Owner resolution: YAML override → Azure Tag → CODEOWNERS → unrouted
# ---------------------------------------------------------------------------
#
# Why three sources?
#   - YAML override file: maintained by the FinOps team for orgs that want
#     a single hand-curated source of truth (highest priority — first hit wins).
#   - Azure tags: the default, and the only source pre-existing in this engine.
#   - CODEOWNERS: a path-glob fallback for orgs that already maintain one in
#     their FinOps repo and don't want to duplicate it as a YAML override.
#
# The chosen source is recorded on each Finding and emitted in the per-row
# ``owner_source`` column of the enriched CSV so downstream reviewers can see
# *why* a row was routed to the team it was.


def _parse_simple_yaml_overrides(text: str) -> list[dict[str, str]]:
    """Parse a small YAML subset used by ``--owner-yaml``.

    Accepted schema (2-space indent, no quotes required, # comments allowed):

        overrides:
          - resource_id: /subscriptions/.../foo
            owner: team-x
          - resource_group: rg-data
            sub_name: sub-prod-a
            owner: data-team

    Each list item becomes a ``{key: value, ...}`` dict. All keys other
    than ``owner`` are treated as match criteria; ``owner`` is the routing
    target. Stdlib-only by design — install ``PyYAML`` and pass JSON if
    you need fancier YAML features (JSON is a subset of YAML, so a
    ``.json`` override file Just Works via ``json.load``).
    """
    rules: list[dict[str, str]] = []
    in_overrides = False
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        # Strip comments & trailing whitespace.
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if indent == 0:
            in_overrides = stripped.rstrip(":").strip() == "overrides"
            if current is not None:
                rules.append(current)
                current = None
            continue
        if not in_overrides:
            continue
        if stripped.startswith("- "):
            if current is not None:
                rules.append(current)
            current = {}
            stripped = stripped[2:].lstrip()
            if not stripped:
                continue
        if current is None or ":" not in stripped:
            continue
        k, _, v = stripped.partition(":")
        current[k.strip().lower()] = v.strip().strip('"').strip("'")
    if current is not None:
        rules.append(current)
    return rules


def load_owner_yaml(path: Path) -> list[dict[str, str]]:
    """Load ``--owner-yaml`` overrides as an ordered list of rules.

    Supports a small YAML subset (see :func:`_parse_simple_yaml_overrides`)
    and JSON files with the same schema (``{"overrides": [...]}``).
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        rules = data.get("overrides") if isinstance(data, dict) else data
        if not isinstance(rules, list):
            return []
        out: list[dict[str, str]] = []
        for r in rules:
            if isinstance(r, dict):
                out.append({str(k).lower(): str(v) for k, v in r.items()})
        return out
    return _parse_simple_yaml_overrides(text)


def _yaml_rule_matches(rule: dict[str, str], f: Finding) -> bool:
    """Return True if every match-criterion in ``rule`` matches ``f``.

    Recognised criteria (all case-insensitive, glob-aware via fnmatch):
    ``resource_id``, ``resource_group``, ``sub_name``, ``sub_id``, ``name``.
    A rule with no recognised criteria never matches (defensive — avoids
    a stray ``owner: foo`` line accidentally claiming every finding).
    """
    fields = {
        "resource_id": f.resource_id,
        "resource_group": f.resource_group,
        "sub_name": f.sub_name,
        "sub_id": f.sub_id,
        "name": f.name,
    }
    matched_any = False
    for key, want in rule.items():
        if key == "owner" or not want:
            continue
        have = fields.get(key)
        if have is None:
            # Unknown key — treat as a non-match to be safe.
            return False
        matched_any = True
        if not fnmatch.fnmatchcase((have or "").lower(), want.lower()):
            return False
    return matched_any


def resolve_owner_from_yaml(f: Finding,
                            rules: list[dict[str, str]]) -> str:
    """First-rule-wins lookup against the loaded YAML overrides."""
    for r in rules:
        if _yaml_rule_matches(r, f) and r.get("owner"):
            return r["owner"]
    return ""


def load_codeowners(path: Path) -> list[tuple[str, str]]:
    """Parse a CODEOWNERS-style file into ``[(pattern, owner), ...]`` rules.

    Each non-comment line is ``PATTERN  @owner1 @owner2 ...``. We keep the
    first ``@owner`` (stripped of the leading ``@``) as the routing target;
    additional reviewers are ignored — context-enricher routes to a single
    owner-slug. Patterns are matched against the lowercased resource id
    via ``fnmatch`` (so ``/subscriptions/*/disks/disk-*`` works).
    """
    rules: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owner = next((p.lstrip("@") for p in parts[1:] if p.startswith("@")), "")
        if owner:
            rules.append((pattern, owner))
    return rules


def resolve_owner_from_codeowners(f: Finding,
                                  rules: list[tuple[str, str]]) -> str:
    """Last-rule-wins matching, the same precedence Git uses for CODEOWNERS."""
    rid = (f.resource_id or "").lower()
    if not rid or not rules:
        return ""
    match = ""
    for pattern, owner in rules:
        if fnmatch.fnmatchcase(rid, pattern.lower()):
            match = owner
    return match




# ---------------------------------------------------------------------------
# Enrichment & confidence
# ---------------------------------------------------------------------------


def first_match(tags: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = tags.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in TAG_PLACEHOLDERS:
            return s
    return ""


def classify(f: Finding,
             *,
             owner_tag_keys: tuple[str, ...] = OWNER_KEYS,
             yaml_rules: list[dict[str, str]] | None = None,
             codeowners_rules: list[tuple[str, str]] | None = None) -> None:
    """Populate context fields and resolve owner via the routing chain.

    Resolution chain (first hit wins, recorded on ``f.owner_source``):

      1. ``yaml``        — ``--owner-yaml`` override file (FinOps-curated)
      2. ``tag``         — Azure resource tags (``owner_tag_keys`` order)
      3. ``codeowners``  — ``--codeowners`` path-glob fallback
      4. ``unrouted``    — no source produced an owner

    The non-owner context fields (criticality, environment, cost-centre,
    application) always come from tags — only the owner has the override
    chain because that's what the issue asked for and what's used for
    routing.
    """
    f.criticality = first_match(f.tags, CRITICALITY_KEYS)
    f.environment = first_match(f.tags, ENVIRONMENT_KEYS)
    f.cost_centre = first_match(f.tags, COSTCENTRE_KEYS)
    f.application = first_match(f.tags, APP_KEYS)

    # 1. YAML override.
    owner = resolve_owner_from_yaml(f, yaml_rules) if yaml_rules else ""
    source = "yaml" if owner else ""
    # 2. Azure tag.
    if not owner:
        owner = first_match(f.tags, owner_tag_keys)
        if owner:
            source = "tag"
    # 3. CODEOWNERS.
    if not owner and codeowners_rules:
        owner = resolve_owner_from_codeowners(f, codeowners_rules)
        if owner:
            source = "codeowners"
    # 4. Unrouted.
    f.owner = owner
    f.owner_source = source or "unrouted"

    has_owner = bool(f.owner)
    has_crit  = bool(f.criticality)
    has_env   = bool(f.environment)

    if has_owner and (has_crit or has_env) and f.monthly_gbp >= HIGH_GBP_FLOOR:
        f.confidence = "HIGH"
        f.rationale = ("Owner + criticality/environment context present and "
                       f"value ({CURRENCY}{f.monthly_gbp:.0f}/mo) clears the "
                       "auto-issue floor.")
    elif (has_owner or has_crit) and f.monthly_gbp >= MED_GBP_FLOOR:
        f.confidence = "MED"
        bits = []
        if not has_owner: bits.append("missing owner tag")
        if not has_crit:  bits.append("missing criticality tag")
        f.rationale = ("Partial context (" + ", ".join(bits) + ") — review "
                       "before auto-issuing.")
    else:
        f.confidence = "LOW"
        if not has_owner and not has_crit:
            f.rationale = ("Untagged — belongs in the tagging-debt backlog, "
                           "not the remediation queue.")
        else:
            f.rationale = (f"Below {CURRENCY}{MED_GBP_FLOOR:.0f}/mo threshold "
                           "— bulk-handle in next quarterly clean-up.")


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "unowned").lower()).strip("-") or "unowned"


def write_enriched_csv(path: Path, findings: list[Finding]) -> None:
    fields = ["source", "category", "sub_name", "resource_group", "name",
              "resource_id", "monthly_gbp", "annual_savings_gbp",
              "owner", "owner_source", "criticality", "environment",
              "cost_centre", "application", "confidence", "rationale", "extra"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for v in findings:
            w.writerow([v.source, v.category, v.sub_name, v.resource_group,
                        v.name, v.resource_id, f"{v.monthly_gbp:.2f}",
                        f"{v.annual_savings_gbp:.2f}", v.owner, v.owner_source,
                        v.criticality, v.environment, v.cost_centre,
                        v.application, v.confidence, v.rationale, v.extra])


def write_summary_md(path: Path, findings: list[Finding]) -> None:
    by_conf = defaultdict(list)
    for f in findings:
        by_conf[f.confidence].append(f)
    by_owner: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.confidence in ("HIGH", "MED"):
            by_owner[f.owner or "(unowned)"].append(f)

    total_monthly = sum(f.monthly_gbp for f in findings)
    auto_monthly  = sum(f.monthly_gbp for f in by_conf["HIGH"])
    review_monthly = sum(f.monthly_gbp for f in by_conf["MED"])
    debt_monthly   = sum(f.monthly_gbp for f in by_conf["LOW"])

    L = []
    L.append("# Context-enriched findings — FinOps Engine")
    L.append("")
    L.append(f"**Generated**: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}  ")
    L.append(f"**Findings ingested**: {len(findings)}")
    L.append("")
    L.append("## Confidence breakdown")
    L.append("")
    L.append("| Band | Count | Monthly cost | Routing |")
    L.append("|---|---:|---:|---|")
    L.append(f"| HIGH (auto-issue ready) | {len(by_conf['HIGH'])} | "
             f"{CURRENCY}{auto_monthly:,.0f} | Domain owner GitHub Issue |")
    L.append(f"| MED  (review first) | {len(by_conf['MED'])} | "
             f"{CURRENCY}{review_monthly:,.0f} | FinOps weekly triage |")
    L.append(f"| LOW  (tagging debt / sub-{CURRENCY}25) | {len(by_conf['LOW'])} | "
             f"{CURRENCY}{debt_monthly:,.0f} | Platform-team backlog |")
    L.append(f"| **Total** | **{len(findings)}** | **{CURRENCY}{total_monthly:,.0f}** | |")
    L.append("")
    L.append("## Auto-issue queue by owner")
    L.append("")
    if not by_owner:
        L.append("_No HIGH/MED findings._")
    else:
        L.append("| Owner | Count | Monthly cost | Bundle |")
        L.append("|---|---:|---:|---|")
        for owner in sorted(by_owner.keys(),
                            key=lambda k: -sum(f.monthly_gbp for f in by_owner[k])):
            items = by_owner[owner]
            mo = sum(f.monthly_gbp for f in items)
            L.append(f"| {owner} | {len(items)} | {CURRENCY}{mo:,.0f} | "
                     f"`issues/{slug(owner)}.md` |")
    L.append("")
    L.append("## Top 25 individual findings")
    L.append("")
    L.append("| Owner | Sub | Resource | Category | Confidence | Cost/mo |")
    L.append("|---|---|---|---|---|---:|")
    for f in sorted(findings, key=lambda x: -x.monthly_gbp)[:25]:
        L.append(f"| {f.owner or '_(untagged)_'} | {f.sub_name} | {f.name} | "
                 f"{f.category} | {f.confidence} | {CURRENCY}{f.monthly_gbp:,.0f} |")
    L.append("")
    L.append("## Tagging debt (top 10 LOW by spend)")
    L.append("")
    debt_top = sorted(by_conf["LOW"], key=lambda x: -x.monthly_gbp)[:10]
    if not debt_top:
        L.append("_None — every finding has at least owner or criticality._")
    else:
        L.append("| Sub | Resource | Category | Cost/mo |")
        L.append("|---|---|---|---:|")
        for f in debt_top:
            L.append(f"| {f.sub_name} | {f.name} | {f.category} | "
                     f"{CURRENCY}{f.monthly_gbp:,.0f} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("_Generated by `context-enricher`. Source: phase 1 / 2 / 3 "
             "engine CSVs. No deletes performed; this is a review aid._")
    path.write_text("\n".join(L), encoding="utf-8")


def write_owner_issue(path: Path, owner: str, items: list[Finding],
                      *, plan_only: bool = False) -> None:
    items = sorted(items, key=lambda x: -x.monthly_gbp)
    monthly = sum(f.monthly_gbp for f in items)
    annual  = monthly * 12

    L = []
    if plan_only:
        L.append("> ⚠️ **DRY-RUN — `--plan-only` mode.** No GitHub Issue was "
                 "opened for this body. To actually open Issues, re-run "
                 "without `--plan-only` (engine) or with the workflow input "
                 "`plan_only=false` (CI).")
        L.append("")
    L.append(f"## FinOps remediation queue — {owner or 'Unowned'}")
    L.append("")
    L.append(f"**{len(items)} findings · ~{CURRENCY}{monthly:,.0f} / month "
             f"({CURRENCY}{annual:,.0f} / yr) recoverable.**")
    L.append("")
    L.append("> Generated by the FinOps Engine automation. Each row links back "
             "to the engine that surfaced it; deletion / rightsizing remains "
             "your call. Reply on this issue with `accept`, `defer`, or "
             "`reject` per row to update the tracker.")
    L.append("")
    L.append("| # | Resource | Category | Confidence | Criticality | "
             "Environment | Cost/mo |")
    L.append("|---:|---|---|---|---|---|---:|")
    for i, f in enumerate(items, 1):
        L.append(f"| {i} | `{f.name}` ({f.sub_name}/{f.resource_group}) | "
                 f"{f.category} | {f.confidence} | {f.criticality or '—'} | "
                 f"{f.environment or '—'} | {CURRENCY}{f.monthly_gbp:,.0f} |")
    L.append("")
    L.append("### Source data")
    L.append("")
    seen_sources = sorted({f.source for f in items})
    for s in seen_sources:
        if s == "hidden_waste":
            L.append("- **Hidden waste & lifecycle** — orphan disks/IPs/NICs, "
                     "old snapshots, empty App Service Plans, "
                     "stopped-not-deallocated VMs, idle Standard load balancers. "
                     "Engine: `tools/hidden-waste/`. Pricing: 30-day "
                     "Cost Management actuals with list-price fallback.")
        elif s == "rightsizing":
            L.append("- **Peak-aware rightsizing** — 90-day P95 CPU + memory "
                     "headroom analysis. Engine: `tools/rightsizing-peak/`. "
                     "Only downsizes that preserve 30% headroom on observed peak.")
    L.append("")
    L.append("### Suggested next step")
    L.append("")
    L.append("1. Eyeball the top 3 by spend — these are 80% of the spend.")
    L.append("2. For HIGH-confidence rows, raise a change ticket against the "
             "named resource group.")
    L.append("3. For MED rows, confirm criticality / environment with the "
             "platform team before action.")
    L.append("4. Reply on this issue per row to close the loop.")
    path.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(hidden_waste_csv: Path | None,
        rightsizing_csv: Path | None,
        out_dir: Path,
        *,
        plan_only: bool = False,
        owner_yaml: Path | None = None,
        owner_tag_keys: tuple[str, ...] | None = None,
        codeowners: Path | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # In plan-only (dry-run) mode we write the planned per-owner Issue
    # bodies to ``issues-planned/`` instead of ``issues/`` so the nightly
    # workflow's ``gh issue create`` loop (which globs ``issues/*.md``)
    # finds nothing and is effectively a no-op. Reviewers can grep the
    # ``issues-planned/`` directory before flipping the flag off.
    issues_dirname = "issues-planned" if plan_only else "issues"
    issues_dir = out_dir / issues_dirname
    issues_dir.mkdir(exist_ok=True)

    findings: list[Finding] = []
    if hidden_waste_csv:
        print(f"[enricher] Loading hidden-waste: {hidden_waste_csv.name}",
              flush=True)
        findings += load_hidden_waste(hidden_waste_csv)
    if rightsizing_csv:
        print(f"[enricher] Loading rightsizing: {rightsizing_csv.name}",
              flush=True)
        findings += load_rightsizing(rightsizing_csv)

    if not findings:
        print("[enricher] No findings loaded.")
        return

    # Resolve missing resource ids (rightsizing CSV needs a lookup).
    rs = [f for f in findings if f.source == "rightsizing" and not f.resource_id]
    if rs:
        # We need sub IDs — group by sub_name first.
        names_by_sub = defaultdict(list)
        for f in rs:
            names_by_sub[f.sub_name].append(f)
        # Get a sub_id -> sub_name map from the hidden-waste set if present.
        sub_id_by_name = {}
        for f in findings:
            if f.source == "hidden_waste" and f.sub_id:
                sub_id_by_name[f.sub_name] = f.sub_id
        # Resolve any remaining via az.
        for sname in names_by_sub:
            if sname in sub_id_by_name:
                continue
            try:
                resp = az(["account", "list", "--query",
                           f"[?name=='{sname}'].id", "-o", "tsv"])
                if isinstance(resp, list) and resp:
                    sub_id_by_name[sname] = resp[0]
            except Exception:
                pass
        print(f"[enricher] Resolving resource IDs for "
              f"{len(rs)} rightsizing rows...", flush=True)
        for f in rs:
            sid = sub_id_by_name.get(f.sub_name, "")
            if not sid:
                continue
            f.sub_id = sid
            rid = fetch_vm_id_by_name(sid, f.name)
            if rid:
                f.resource_id = rid

    # Tag enrichment.
    ids = [f.resource_id for f in findings if f.resource_id]
    print(f"[enricher] Looking up tags for {len(ids)} resource IDs "
          f"(batches of {GRAPH_BATCH})...", flush=True)
    tag_map = fetch_tags_for_ids(ids) if ids else {}

    # Load owner-resolution sources (YAML override + CODEOWNERS fallback).
    yaml_rules: list[dict[str, str]] = []
    if owner_yaml:
        try:
            yaml_rules = load_owner_yaml(owner_yaml)
            print(f"[enricher] Loaded {len(yaml_rules)} owner override(s) "
                  f"from {owner_yaml}", flush=True)
        except (OSError, ValueError) as e:
            print(f"[enricher] WARN: could not read --owner-yaml "
                  f"{owner_yaml}: {e}", flush=True)
    codeowners_rules: list[tuple[str, str]] = []
    if codeowners:
        try:
            codeowners_rules = load_codeowners(codeowners)
            print(f"[enricher] Loaded {len(codeowners_rules)} CODEOWNERS "
                  f"rule(s) from {codeowners}", flush=True)
        except OSError as e:
            print(f"[enricher] WARN: could not read --codeowners "
                  f"{codeowners}: {e}", flush=True)
    tag_keys = owner_tag_keys or OWNER_KEYS

    for f in findings:
        f.tags = tag_map.get(f.resource_id.lower(), {})
        classify(f,
                 owner_tag_keys=tag_keys,
                 yaml_rules=yaml_rules,
                 codeowners_rules=codeowners_rules)

    # Output.
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    csv_path  = out_dir / f"enriched-{date}.csv"
    md_path   = out_dir / f"enriched-{date}.md"
    html_path = out_dir / f"enriched-{date}.html"
    write_enriched_csv(csv_path, findings)
    write_summary_md(md_path, findings)
    write_html(md_path, html_path)
    write_index(out_dir, [
        ("Context-enriched Findings — FinOps Engine", html_path.name),
    ])

    # Per-owner issue bundles for HIGH+MED.
    by_owner: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.confidence in ("HIGH", "MED"):
            by_owner[f.owner or "_unowned"].append(f)
    for owner, items in by_owner.items():
        write_owner_issue(issues_dir / f"{slug(owner)}-{date}.md",
                          owner if owner != "_unowned" else "Unowned", items,
                          plan_only=plan_only)

    print(f"[enricher] Done.")
    print(f"  - {csv_path}")
    print(f"  - {md_path}")
    print(f"  - {html_path}")
    label = "planned (dry-run)" if plan_only else "per-owner issue templates"
    print(f"  - {issues_dir}/  ({len(by_owner)} {label})")
    if plan_only:
        print("[enricher] PLAN-ONLY: no GitHub Issues will be opened. "
              "Review the bodies above, then re-run without --plan-only.")
    high = sum(1 for f in findings if f.confidence == "HIGH")
    med  = sum(1 for f in findings if f.confidence == "MED")
    low  = sum(1 for f in findings if f.confidence == "LOW")
    high_gbp = sum(f.monthly_gbp for f in findings if f.confidence == "HIGH")
    print(f"[enricher] Confidence: HIGH={high} ({CURRENCY}{high_gbp:,.0f}/mo), "
          f"MED={med}, LOW={low}.")
    src_counts: dict[str, int] = defaultdict(int)
    for f in findings:
        src_counts[f.owner_source] += 1
    src_summary = ", ".join(f"{k}={src_counts[k]}"
                            for k in ("yaml", "tag", "codeowners", "unrouted")
                            if src_counts[k])
    if src_summary:
        print(f"[enricher] Owner sources: {src_summary}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hidden-waste-csv", type=Path)
    ap.add_argument("--rightsizing-csv", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "Dry-run: write planned per-owner Issue bodies to "
            "out-dir/issues-planned/ instead of issues/, and print a "
            "PLAN-ONLY banner. No GitHub Issues are opened — the nightly "
            "workflow's gh issue create step globs issues/*.md and finds "
            "nothing. Reviewers can grep issues-planned/ before flipping "
            "the flag off."
        ),
    )
    ap.add_argument(
        "--currency-symbol", type=str, default=None,
        help="Override the auto-detected display currency glyph (e.g. "
             "'$', '€', 'kr'). When omitted, the engine calls "
             "`az billing account list` once to read the tenant's "
             "billing currency and falls back to '£' on any failure.",
    )
    ap.add_argument(
        "--owner-yaml", type=Path, default=None,
        help="Path to a YAML (or JSON) override file listing per-resource "
             "/ per-RG / per-subscription owner overrides. Highest priority "
             "in the resolution chain (YAML → tag → CODEOWNERS → unrouted). "
             "See tools/context-enricher/README.md for the schema.",
    )
    ap.add_argument(
        "--owner-tag-keys", type=str, default=None,
        help="Comma-separated, case-insensitive list of Azure tag keys to "
             "consult when resolving owners from tags (e.g. "
             "'owner,costcenter,team'). Overrides the built-in OWNER_KEYS "
             "tuple — useful for orgs with a non-default tag taxonomy.",
    )
    ap.add_argument(
        "--codeowners", type=Path, default=None,
        help="Path to a CODEOWNERS-style file used as a last-resort routing "
             "fallback when neither the YAML override nor a resource tag "
             "produces an owner. Each line is 'PATTERN @owner1 [@owner2 ...]'; "
             "the first @owner is the routing target and the pattern is glob-"
             "matched against the lowercased Azure resource id.",
    )
    args = ap.parse_args()
    if not args.hidden_waste_csv and not args.rightsizing_csv:
        ap.error("Provide at least one of --hidden-waste-csv / --rightsizing-csv.")
    global CURRENCY, CURRENCY_ISO
    sym, iso, source = detect_currency(args.currency_symbol)
    CURRENCY = sym
    CURRENCY_ISO = iso or CURRENCY_ISO
    src_label = {
        "override": "from --currency-symbol",
        "billing-account": "from `az billing account list`",
        "default": "default — set --currency-symbol or grant Billing "
                   "Reader to silence this",
    }.get(source, source)
    print(f"[enricher] Display currency: {CURRENCY} "
          f"({CURRENCY_ISO or '?'}) — {src_label}")
    tag_keys: tuple[str, ...] | None = None
    if args.owner_tag_keys:
        tag_keys = tuple(k.strip().lower() for k in args.owner_tag_keys.split(",")
                         if k.strip())
    run(args.hidden_waste_csv, args.rightsizing_csv, args.out_dir,
        plan_only=args.plan_only,
        owner_yaml=args.owner_yaml,
        owner_tag_keys=tag_keys,
        codeowners=args.codeowners)


if __name__ == "__main__":
    main()

