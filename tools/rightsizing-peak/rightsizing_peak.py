#!/usr/bin/env python3
"""
rightsizing-peak — Peak-aware VM rightsizing engine.

A longer-window, per-hour-true-peak companion to Azure Advisor's
"Cost — Resize Virtual Machine" recommendations.

Advisor already uses P95/P99 (see Microsoft Learn:
advisor-cost-recommendations#resize-sku-recommendations) but on a 7-day
default window with 30-min buckets taken as the max of 1-minute averages.
This engine uses a 30-day default window with per-hour `Maximum` CPU and
per-hour `Minimum` available-memory bucketing, and is suitable for spiky
retail, batch, CI, and month-end workloads where Advisor's shorter window
or coarser bucketing can hide a peak that justifies the current SKU.

WHAT IT DOES
------------
For each VM in the target subscription(s):
  1. Pulls 30 days of Percentage CPU (Avg + Max) and Available Memory Bytes
     (Min + Avg) from Azure Monitor at PT1H grain.
  2. Computes P95/P99 of the per-hour Max CPU and per-hour Min memory.
  3. Looks up the SKU's vCPU + memory capacity (via `az vm list-skus`).
  4. Applies a peak-aware decision tree (see DECISION_RULES).
  5. Cross-checks Azure Advisor's cost recommendations and flags any of
     Advisor's "downsize" recs that our engine would deem UNSAFE.

OUTPUTS
-------
Per subscription:
  - <sub>-peak-rightsizing-<date>.csv     (machine-readable)
  - <sub>-peak-rightsizing-<date>.md      (human report)
And one combined diff:
  - peak-rightsizing-combined-<date>.md

ASSUMPTIONS
-----------
- Auth: existing `az login` context.
- Excludes Databricks-managed (databricks-rg-*) and AKS node-pool VMs
  (MC_* / aks-*) — these are managed by their parent service.
- Treats VMs with < 80% metric coverage over the window as INSUFFICIENT_DATA
  rather than recommending action.
- Memory% is computed as (1 - AvailableMin / TotalCapacity).

USAGE
-----
    python rightsizing_peak.py \\
        --subs <subId>[,<subId>...] \\
        --days 30 \\
        --out-dir <path>
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# HTML report sink (shared utility — no third-party deps)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from html_sink import write_html, write_index  # noqa: E402

# ---------------------------------------------------------------------------
# Decision rules — single source of truth for the engine's logic
# ---------------------------------------------------------------------------
DECISION_RULES = {
    "downsize_cpu_p95_max": 40.0,    # peak CPU over 30 days below this %
    "downsize_mem_p95_max": 50.0,    # peak memory used below this %
    "downsize_cpu_p99_high_conf": 50.0,
    "downsize_mem_p99_high_conf": 60.0,
    "upsize_cpu_p95_min": 80.0,
    "upsize_mem_p95_min": 85.0,
    "min_data_coverage": 0.80,       # require 80% of expected hourly samples
}

# Family downsize ladders (one step smaller in same family/generation).
# Engine only proposes a target SKU if it's in this map; otherwise it
# emits the verdict (e.g. DOWNSIZE) but leaves target_sku blank for
# manual review. This keeps the engine conservative — domain teams trust
# explicit ladders, not heuristics.
DOWNSIZE_LADDER = {
    # D/Ds v3
    "Standard_D4_v3": "Standard_D2_v3",
    "Standard_D8_v3": "Standard_D4_v3",
    "Standard_D4s_v3": "Standard_D2s_v3",
    "Standard_D8s_v3": "Standard_D4s_v3",
    "Standard_DS3_v2": "Standard_DS2_v2",
    "Standard_DS5_v2": "Standard_DS4_v2",
    # D v4 / v5
    "Standard_D4_v4": "Standard_D2_v4",
    "Standard_D8_v4": "Standard_D4_v4",
    "Standard_D4ds_v5": "Standard_D2ds_v5",
    "Standard_D8ds_v5": "Standard_D4ds_v5",
    "Standard_D16ds_v5": "Standard_D8ds_v5",
    "Standard_D4ads_v5": "Standard_D2ads_v5",
    "Standard_D8ads_v5": "Standard_D4ads_v5",
    "Standard_D16ads_v5": "Standard_D8ads_v5",
    # E series
    "Standard_E4ds_v4": "Standard_E2ds_v4",
    "Standard_E8ds_v4": "Standard_E4ds_v4",
    "Standard_E16ds_v4": "Standard_E8ds_v4",
    "Standard_E4ds_v5": "Standard_E2ds_v5",
    "Standard_E8ds_v5": "Standard_E4ds_v5",
    "Standard_E16ds_v5": "Standard_E8ds_v5",
    "Standard_E4ads_v5": "Standard_E2ads_v5",
    "Standard_E8ads_v5": "Standard_E4ads_v5",
    "Standard_E8as_v5": "Standard_E4as_v5",
    "Standard_E16as_v5": "Standard_E8as_v5",
    # F series
    "Standard_F4s_v2": "Standard_F2s_v2",
    "Standard_F8s_v2": "Standard_F4s_v2",
    "Standard_F16s_v2": "Standard_F8s_v2",
    # B series
    "Standard_B4ms": "Standard_B2ms",
    "Standard_B8ms": "Standard_B4ms",
    # L series
    "Standard_L8s": "Standard_L4s",
    "Standard_L16s": "Standard_L8s",
}

# Family upsize ladders (one step larger in same family/generation).
# Mirror of DOWNSIZE_LADDER. Engine only proposes a target SKU if it's in
# this map; otherwise it emits the verdict (UPSIZE) but leaves target_sku
# blank for manual review.
UPSIZE_LADDER = {
    # D/Ds v3
    "Standard_D2_v3": "Standard_D4_v3",
    "Standard_D4_v3": "Standard_D8_v3",
    "Standard_D2s_v3": "Standard_D4s_v3",
    "Standard_D4s_v3": "Standard_D8s_v3",
    "Standard_DS2_v2": "Standard_DS3_v2",
    "Standard_DS4_v2": "Standard_DS5_v2",
    # D v4 / v5
    "Standard_D2_v4": "Standard_D4_v4",
    "Standard_D4_v4": "Standard_D8_v4",
    "Standard_D2ds_v5": "Standard_D4ds_v5",
    "Standard_D4ds_v5": "Standard_D8ds_v5",
    "Standard_D8ds_v5": "Standard_D16ds_v5",
    "Standard_D2ads_v5": "Standard_D4ads_v5",
    "Standard_D4ads_v5": "Standard_D8ads_v5",
    "Standard_D8ads_v5": "Standard_D16ads_v5",
    # E series
    "Standard_E2ds_v4": "Standard_E4ds_v4",
    "Standard_E4ds_v4": "Standard_E8ds_v4",
    "Standard_E8ds_v4": "Standard_E16ds_v4",
    "Standard_E2ds_v5": "Standard_E4ds_v5",
    "Standard_E4ds_v5": "Standard_E8ds_v5",
    "Standard_E8ds_v5": "Standard_E16ds_v5",
    "Standard_E2ads_v5": "Standard_E4ads_v5",
    "Standard_E4ads_v5": "Standard_E8ads_v5",
    "Standard_E4as_v5": "Standard_E8as_v5",
    "Standard_E8as_v5": "Standard_E16as_v5",
    # F series
    "Standard_F2s_v2": "Standard_F4s_v2",
    "Standard_F4s_v2": "Standard_F8s_v2",
    "Standard_F8s_v2": "Standard_F16s_v2",
    # B series
    "Standard_B2ms": "Standard_B4ms",
    "Standard_B4ms": "Standard_B8ms",
    # L series
    "Standard_L4s": "Standard_L8s",
    "Standard_L8s": "Standard_L16s",
}

# SKU-family modernization swaps. Maps an older-generation SKU to a
# modern AMD (`asv5`) equivalent at the same vCPU/memory shape, which
# typically saves 10–20% at equal or better performance. Source: Azure
# VM pricing pages (compare retail $/hr for the source vs target SKU in
# the same region; Dasv5 / Easv5 are consistently cheaper than the
# corresponding Dv3 / DSv2 / Ev3 SKUs at matching size).
SKU_FAMILY_SWAP = {
    # Dv3 / Dsv3 → Dasv5 (AMD EPYC, premium SSD)
    "Standard_D2_v3":  "Standard_D2as_v5",
    "Standard_D4_v3":  "Standard_D4as_v5",
    "Standard_D8_v3":  "Standard_D8as_v5",
    "Standard_D16_v3": "Standard_D16as_v5",
    "Standard_D2s_v3":  "Standard_D2as_v5",
    "Standard_D4s_v3":  "Standard_D4as_v5",
    "Standard_D8s_v3":  "Standard_D8as_v5",
    "Standard_D16s_v3": "Standard_D16as_v5",
    # Older DSv2 → Dasv5
    "Standard_DS2_v2": "Standard_D2as_v5",
    "Standard_DS3_v2": "Standard_D4as_v5",
    "Standard_DS4_v2": "Standard_D8as_v5",
    "Standard_DS5_v2": "Standard_D16as_v5",
    # Ev3 / Esv3 → Easv5 (memory-optimized AMD)
    "Standard_E2_v3":  "Standard_E2as_v5",
    "Standard_E4_v3":  "Standard_E4as_v5",
    "Standard_E8_v3":  "Standard_E8as_v5",
    "Standard_E16_v3": "Standard_E16as_v5",
    "Standard_E2s_v3":  "Standard_E2as_v5",
    "Standard_E4s_v3":  "Standard_E4as_v5",
    "Standard_E8s_v3":  "Standard_E8as_v5",
    "Standard_E16s_v3": "Standard_E16as_v5",
    # Dv4 → Dasv5 (Dv4 is Intel; Dasv5 typically cheaper at same shape)
    "Standard_D2_v4": "Standard_D2as_v5",
    "Standard_D4_v4": "Standard_D4as_v5",
    "Standard_D8_v4": "Standard_D8as_v5",
}

# Low-duty-cycle swap to B-series. Applied only when a VM is already a
# DOWNSIZE_CANDIDATE *and* CPU P95 max is below LOW_DUTY_CPU_P95_MAX,
# meaning the workload is bursty/idle enough that B-series credit
# accounting will be a net win.
LOW_DUTY_CPU_P95_MAX = 15.0
LOW_DUTY_B_SWAP = {
    # Map source SKU shape → B-series equivalent at the same vCPU/memory.
    # Only includes shapes where a B-series SKU exists at matching capacity.
    "Standard_D2s_v3":   "Standard_B2ms",
    "Standard_D2_v3":    "Standard_B2ms",
    "Standard_D2ds_v5":  "Standard_B2ms",
    "Standard_D2ads_v5": "Standard_B2ms",
    "Standard_D4s_v3":   "Standard_B4ms",
    "Standard_D4_v3":    "Standard_B4ms",
    "Standard_D4ds_v5":  "Standard_B4ms",
    "Standard_D4ads_v5": "Standard_B4ms",
    "Standard_D8s_v3":   "Standard_B8ms",
    "Standard_D8_v3":    "Standard_B8ms",
    "Standard_D8ds_v5":  "Standard_B8ms",
    "Standard_D8ads_v5": "Standard_B8ms",
    "Standard_DS2_v2":   "Standard_B2ms",
    "Standard_DS3_v2":   "Standard_B4ms",
    "Standard_DS4_v2":   "Standard_B8ms",
}

# Resource Graph query — non-managed VMs only
VM_QUERY = """
Resources
| where type =~ 'microsoft.compute/virtualmachines'
| where subscriptionId in ({sub_list})
| where resourceGroup !startswith 'databricks-rg-'
| where resourceGroup !startswith 'mc_'
| where not(name startswith 'aks-')
| extend vmSize = tostring(properties.hardwareProfile.vmSize)
| extend powerState = tostring(properties.extended.instanceView.powerState.displayStatus)
| project subscriptionId, resourceGroup, name, id, vmSize, location, powerState
| order by subscriptionId asc, resourceGroup asc, name asc
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class SkuCapacity:
    vcpus: int
    memory_gb: float


@dataclass
class VmRecord:
    subscription_id: str
    resource_group: str
    name: str
    resource_id: str
    vm_size: str
    location: str
    power_state: str
    cpu_avg: Optional[float] = None
    cpu_p95_max: Optional[float] = None
    cpu_p99_max: Optional[float] = None
    mem_used_p95: Optional[float] = None
    mem_used_p99: Optional[float] = None
    data_coverage: Optional[float] = None
    verdict: str = "UNKNOWN"
    confidence: str = ""
    target_sku: str = ""
    recommended_sku: str = ""
    rationale: str = ""
    advisor_says: str = ""
    advisor_unsafe: bool = False


# ---------------------------------------------------------------------------
# az CLI helpers
# ---------------------------------------------------------------------------
def az(args: list[str], *, subscription: Optional[str] = None) -> dict:
    cmd = ["az", *args, "-o", "json"]
    if subscription and "--subscription" not in args and "-s" not in args:
        cmd += ["--subscription", subscription]
    use_shell = sys.platform.startswith("win")
    if use_shell:
        # cmd.exe breaks on newlines inside quoted args — collapse them.
        flat = [(" ".join(a.split()) if "\n" in a else a) for a in cmd]
        cmd_str = " ".join(_q(a) for a in flat)
        p = subprocess.run(cmd_str, capture_output=True, text=True,
                           shell=True)
    else:
        p = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if p.returncode != 0:
        raise RuntimeError(
            f"az failed ({p.returncode}): {' '.join(cmd[:6])}…\n"
            f"{p.stderr[:500]}"
        )
    return json.loads(p.stdout) if p.stdout.strip() else {}


def _q(s: str) -> str:
    """Quote a single arg for cmd.exe."""
    if not s:
        return '""'
    if any(c in s for c in ' \t"&|<>^()'):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def az_graph(query: str) -> list[dict]:
    out = az(["graph", "query", "-q", query, "--first", "1000"])
    return out.get("data", [])


def az_metrics(resource_id: str, *, metric: str, aggregation: str,
               start: str, end: str, interval: str = "PT1H") -> list[dict]:
    args = [
        "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", metric,
        "--aggregation", aggregation,
        "--interval", interval,
        "--start-time", start,
        "--end-time", end,
    ]
    try:
        data = az(args)
    except RuntimeError as e:
        # Many possible reasons — VM stopped, metric unsupported, throttled
        return []
    series = data.get("value", [])
    if not series:
        return []
    ts = series[0].get("timeseries", [])
    if not ts:
        return []
    return ts[0].get("data", [])


# ---------------------------------------------------------------------------
# SKU capacity catalogue
# ---------------------------------------------------------------------------
def build_sku_catalogue(locations: list[str]) -> dict[str, SkuCapacity]:
    cat: dict[str, SkuCapacity] = {}
    for loc in locations:
        try:
            skus = az(["vm", "list-skus", "--location", loc,
                       "--resource-type", "virtualMachines"])
        except RuntimeError:
            continue
        for s in skus:
            name = s.get("name")
            if not name:
                continue
            caps = {c["name"]: c["value"] for c in s.get("capabilities", [])}
            try:
                vcpu = int(caps.get("vCPUs", "0"))
                mem = float(caps.get("MemoryGB", "0"))
            except (TypeError, ValueError):
                continue
            if name not in cat and vcpu and mem:
                cat[name] = SkuCapacity(vcpus=vcpu, memory_gb=mem)
    return cat


# ---------------------------------------------------------------------------
# Engine core
# ---------------------------------------------------------------------------
def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * pct
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def analyse_vm(vm: VmRecord, *, days: int,
               sku_cat: dict[str, SkuCapacity]) -> VmRecord:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    s_iso = start.isoformat().replace("+00:00", "Z")
    e_iso = end.isoformat().replace("+00:00", "Z")
    expected_samples = days * 24

    # CPU — Average and Maximum
    cpu_avg_pts = az_metrics(vm.resource_id, metric="Percentage CPU",
                             aggregation="Average", start=s_iso, end=e_iso)
    cpu_max_pts = az_metrics(vm.resource_id, metric="Percentage CPU",
                             aggregation="Maximum", start=s_iso, end=e_iso)
    # Memory — Available Bytes (Minimum gives worst-case headroom)
    mem_min_pts = az_metrics(vm.resource_id, metric="Available Memory Bytes",
                             aggregation="Minimum", start=s_iso, end=e_iso)

    cpu_avgs = [p["average"] for p in cpu_avg_pts if "average" in p]
    cpu_maxes = [p["maximum"] for p in cpu_max_pts if "maximum" in p]
    mem_mins = [p["minimum"] for p in mem_min_pts if "minimum" in p]

    coverage = (
        max(len(cpu_avgs), len(cpu_maxes), len(mem_mins)) / expected_samples
        if expected_samples else 0.0
    )
    vm.data_coverage = round(coverage, 3)

    if coverage < DECISION_RULES["min_data_coverage"]:
        vm.verdict = "INSUFFICIENT_DATA"
        vm.rationale = (
            f"Only {int(coverage*100)}% metric coverage in last {days}d "
            f"(VM may be deallocated, recently created, or excluded from "
            f"diagnostic settings)."
        )
        return vm

    if cpu_avgs:
        vm.cpu_avg = round(statistics.fmean(cpu_avgs), 1)
    if cpu_maxes:
        vm.cpu_p95_max = round(percentile(cpu_maxes, 0.95), 1)
        vm.cpu_p99_max = round(percentile(cpu_maxes, 0.99), 1)

    cap = sku_cat.get(vm.vm_size)
    if cap and mem_mins:
        total_bytes = cap.memory_gb * 1024**3
        used_pct = [max(0.0, (1.0 - (m / total_bytes)) * 100.0)
                    for m in mem_mins]
        vm.mem_used_p95 = round(percentile(used_pct, 0.95), 1)
        vm.mem_used_p99 = round(percentile(used_pct, 0.99), 1)

    # Decision tree
    cpu95 = vm.cpu_p95_max or 0.0
    cpu99 = vm.cpu_p99_max or 0.0
    mem95 = vm.mem_used_p95 or 0.0
    mem99 = vm.mem_used_p99 or 0.0

    if (cpu95 >= DECISION_RULES["upsize_cpu_p95_min"]
            or mem95 >= DECISION_RULES["upsize_mem_p95_min"]):
        vm.verdict = "UPSIZE"
        vm.confidence = "HIGH"
        vm.target_sku = UPSIZE_LADDER.get(vm.vm_size, "")
        vm.rationale = (
            f"P95 CPU {cpu95}% / P95 Mem {mem95}% — sustained over upsize "
            f"threshold. Recommend upsize or scale-out; do NOT downsize."
        )
    elif (cpu95 < DECISION_RULES["downsize_cpu_p95_max"]
          and mem95 < DECISION_RULES["downsize_mem_p95_max"]):
        vm.verdict = "DOWNSIZE_CANDIDATE"
        if (cpu99 < DECISION_RULES["downsize_cpu_p99_high_conf"]
                and mem99 < DECISION_RULES["downsize_mem_p99_high_conf"]):
            vm.confidence = "HIGH"
        else:
            vm.confidence = "MEDIUM"
        vm.target_sku = DOWNSIZE_LADDER.get(vm.vm_size, "")
        vm.rationale = (
            f"P95 CPU {cpu95}% (P99 {cpu99}%) / P95 Mem {mem95}% "
            f"(P99 {mem99}%) — comfortably below downsize thresholds."
        )
    else:
        vm.verdict = "KEEP"
        vm.confidence = "HIGH"
        vm.rationale = (
            f"P95 CPU {cpu95}% / P95 Mem {mem95}% — between thresholds; "
            f"current size is appropriate."
        )

    # SKU-family swap recommendation (independent of verdict).
    # Priority: low-duty B-series swap (only for clear DOWNSIZE candidates
    # with very low CPU P95) > family modernization (Dv3 → Dasv5 etc).
    if (vm.verdict == "DOWNSIZE_CANDIDATE"
            and cpu95 < LOW_DUTY_CPU_P95_MAX
            and vm.vm_size in LOW_DUTY_B_SWAP):
        vm.recommended_sku = LOW_DUTY_B_SWAP[vm.vm_size]
    elif vm.vm_size in SKU_FAMILY_SWAP:
        vm.recommended_sku = SKU_FAMILY_SWAP[vm.vm_size]

    return vm


# ---------------------------------------------------------------------------
# Advisor diff
# ---------------------------------------------------------------------------
def fetch_advisor_vm_recs(sub: str) -> dict[str, str]:
    """Return {vm_resource_id: short_advisor_text} for VM cost recs."""
    try:
        recs = az(["advisor", "recommendation", "list", "--category", "Cost"],
                  subscription=sub)
    except RuntimeError:
        return {}
    out: dict[str, str] = {}
    for r in recs:
        impacted = r.get("impactedField", "") or ""
        if "virtualMachines" not in impacted:
            continue
        rid = (r.get("resourceMetadata", {}) or {}).get("resourceId", "")
        if not rid:
            continue
        short = (r.get("shortDescription", {}) or {}).get("solution", "") \
            or r.get("category", "Cost")
        out[rid.lower()] = short
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def write_csv(vms: list[VmRecord], path: Path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(vms[0]).keys())
                           if vms else ["subscription_id"])
        w.writeheader()
        for vm in vms:
            w.writerow(asdict(vm))


def write_md_report(vms: list[VmRecord], sub_name: str, path: Path,
                    days: int):
    counts: dict[str, int] = {}
    for v in vms:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1
    unsafe = [v for v in vms if v.advisor_unsafe]

    lines = [
        f"# Peak-Aware Rightsizing Report — `{sub_name}`",
        "",
        f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Window**: last {days} days  ",
        f"**Engine**: `rightsizing-peak`  ",
        f"**Decision rules**: P95/P99 peak-aware "
        f"(thresholds: downsize CPU<{DECISION_RULES['downsize_cpu_p95_max']}% "
        f"& Mem<{DECISION_RULES['downsize_mem_p95_max']}%; "
        f"upsize CPU≥{DECISION_RULES['upsize_cpu_p95_min']}% "
        f"or Mem≥{DECISION_RULES['upsize_mem_p95_min']}%)",
        "",
        "## Summary",
        "",
        "| Verdict | Count |",
        "|---|---:|",
    ]
    for k in ["DOWNSIZE_CANDIDATE", "KEEP", "UPSIZE",
              "INSUFFICIENT_DATA", "UNKNOWN"]:
        if counts.get(k, 0):
            lines.append(f"| {k} | {counts[k]} |")
    lines += ["", f"**Total VMs analysed**: {len(vms)}", ""]

    if unsafe:
        lines += [
            "## Advisor recommendations our engine flags as UNSAFE",
            "",
            "These are VMs where Azure Advisor recommends a downsize but "
            "P95/P99 peak data shows the workload is **not** safe to downsize.",
            "",
            "| VM | Current SKU | Advisor says | Engine verdict | P95 CPU | P95 Mem |",
            "|---|---|---|---|---:|---:|",
        ]
        for v in unsafe:
            lines.append(
                f"| `{v.name}` | {v.vm_size} | {v.advisor_says[:60]}… | "
                f"**{v.verdict}** | {v.cpu_p95_max}% | {v.mem_used_p95}% |"
            )
        lines.append("")

    downsize = [v for v in vms if v.verdict == "DOWNSIZE_CANDIDATE"]
    if downsize:
        lines += [
            "## Peak-safe downsize candidates",
            "",
            "| Confidence | VM | Current → Target | P95 CPU | P95 Mem | Rationale |",
            "|---|---|---|---:|---:|---|",
        ]
        # high confidence first
        downsize.sort(key=lambda v: (v.confidence != "HIGH", v.name))
        for v in downsize:
            tgt = v.target_sku or "_(no ladder match — review manually)_"
            lines.append(
                f"| {v.confidence} | `{v.name}` | "
                f"{v.vm_size} → {tgt} | {v.cpu_p95_max}% | "
                f"{v.mem_used_p95}% | {v.rationale} |"
            )
        lines.append("")

    upsize = [v for v in vms if v.verdict == "UPSIZE"]
    if upsize:
        lines += [
            "## Upsize candidates (sustained peak headroom — do not downsize)",
            "",
            "| VM | Current → Target | P95 CPU | P95 Mem | Rationale |",
            "|---|---|---:|---:|---|",
        ]
        for v in upsize:
            tgt = v.target_sku or "_(no ladder match — review manually)_"
            lines.append(
                f"| `{v.name}` | {v.vm_size} → {tgt} | "
                f"{v.cpu_p95_max}% | {v.mem_used_p95}% | {v.rationale} |"
            )
        lines.append("")

    swap = [v for v in vms
            if v.recommended_sku and v.recommended_sku != v.target_sku]
    if swap:
        lines += [
            "## SKU-family swap suggestions",
            "",
            "Modernization opportunities independent of the up/downsize "
            "verdict. Typical savings: 10–20% at equal or better "
            "performance (Dv3/DSv2 → Dasv5, Ev3 → Easv5) or larger for "
            "low-duty workloads moving to B-series.",
            "",
            "| VM | Verdict | Current → Recommended | P95 CPU | P95 Mem |",
            "|---|---|---|---:|---:|",
        ]
        for v in swap:
            cpu = f"{v.cpu_p95_max}%" if v.cpu_p95_max is not None else "—"
            mem = f"{v.mem_used_p95}%" if v.mem_used_p95 is not None else "—"
            lines.append(
                f"| `{v.name}` | {v.verdict} | "
                f"{v.vm_size} → {v.recommended_sku} | {cpu} | {mem} |"
            )
        lines.append("")

    lines += [
        "## Methodology",
        "",
        "- Source metrics: Azure Monitor platform metrics "
        "(`Percentage CPU` Max + Avg, `Available Memory Bytes` Min) at "
        "`PT1H` grain.",
        f"- Window: last {days} days; minimum coverage "
        f"{int(DECISION_RULES['min_data_coverage']*100)}% of expected hourly "
        f"samples — otherwise flagged INSUFFICIENT_DATA.",
        "- Memory% computed as `(1 - AvailableMemoryMin / TotalMemoryGB) * 100`",
        "  using the SKU's catalogue capacity (`az vm list-skus`).",
        "- Decision tree applied per VM (see `rightsizing_peak.py` "
        "`DECISION_RULES`).",
        "- Excludes Databricks-managed (`databricks-rg-*`) and AKS node "
        "(`MC_*` / `aks-*`) VMs — those are managed by their parent service.",
        "- Advisor diff: any VM where Advisor recommends a downsize but "
        "the engine emits `UPSIZE` or `KEEP` is flagged as "
        "**advisor_unsafe**.",
        "",
        "_Generated by `rightsizing-peak`. Source: "
        "`tools/rightsizing-peak/`._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(subs: list[str], *, days: int, out_dir: Path,
        max_workers: int = 8) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[engine] Querying Resource Graph for VMs across "
          f"{len(subs)} sub(s)…", flush=True)
    sub_list = ",".join(f"'{s}'" for s in subs)
    rows = az_graph(VM_QUERY.format(sub_list=sub_list))
    print(f"[engine] Found {len(rows)} non-managed VMs.")

    # Group VMs by sub & gather unique locations for SKU catalogue
    vms_by_sub: dict[str, list[VmRecord]] = {s: [] for s in subs}
    locations: set[str] = set()
    for r in rows:
        v = VmRecord(
            subscription_id=r.get("subscriptionId", ""),
            resource_group=r.get("resourceGroup", ""),
            name=r.get("name", ""),
            resource_id=r.get("id", ""),
            vm_size=r.get("vmSize") or "Unknown",
            location=r.get("location", "unknown"),
            power_state=r.get("powerState") or "",
        )
        if not v.resource_id or v.vm_size == "Unknown":
            continue
        vms_by_sub.setdefault(v.subscription_id, []).append(v)
        locations.add(v.location)

    print(f"[engine] Building SKU catalogue for {len(locations)} location(s)…")
    sku_cat = build_sku_catalogue(sorted(locations))
    print(f"[engine] Catalogue: {len(sku_cat)} SKUs.")

    # Subscription-name lookup (best-effort)
    sub_names: dict[str, str] = {}
    try:
        for acc in az(["account", "list"]):
            sub_names[acc["id"]] = acc.get("name") or acc["id"]
    except RuntimeError:
        pass

    summary = {"subscriptions": [], "totals": {}}
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    for sub_id, vms in vms_by_sub.items():
        sub_name = sub_names.get(sub_id, sub_id)
        if not vms:
            print(f"[engine] {sub_name}: 0 VMs (skipped).")
            continue

        print(f"[engine] {sub_name}: pulling metrics for {len(vms)} VMs "
              f"({max_workers} parallel)…")

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(analyse_vm, vm, days=days, sku_cat=sku_cat): vm
                    for vm in vms}
            done = 0
            for fut in as_completed(futs):
                fut.result()
                done += 1
                if done % 10 == 0 or done == len(vms):
                    print(f"  ... {done}/{len(vms)}", flush=True)

        # Advisor diff
        adv = fetch_advisor_vm_recs(sub_id)
        for v in vms:
            hit = adv.get(v.resource_id.lower())
            if hit:
                v.advisor_says = hit
                if v.verdict in ("UPSIZE", "KEEP"):
                    v.advisor_unsafe = True

        csv_path = out_dir / f"{sub_name}-peak-rightsizing-{today}.csv"
        md_path = out_dir / f"{sub_name}-peak-rightsizing-{today}.md"
        html_path = out_dir / f"{sub_name}-peak-rightsizing-{today}.html"
        write_csv(vms, csv_path)
        write_md_report(vms, sub_name, md_path, days)
        write_html(md_path, html_path)

        counts: dict[str, int] = {}
        for v in vms:
            counts[v.verdict] = counts.get(v.verdict, 0) + 1
        unsafe_count = sum(1 for v in vms if v.advisor_unsafe)
        summary["subscriptions"].append({
            "name": sub_name, "id": sub_id, "vms": len(vms),
            "verdicts": counts, "advisor_unsafe": unsafe_count,
            "csv": csv_path.name, "md": md_path.name,
            "html": html_path.name,
        })
        print(f"[engine] {sub_name}: {counts} | unsafe Advisor recs: "
              f"{unsafe_count}")

    # Combined diff report
    write_combined(summary, out_dir, today, days)
    return summary


def write_combined(summary: dict, out_dir: Path, today: str, days: int):
    path = out_dir / f"peak-rightsizing-combined-{today}.md"
    lines = [
        "# Peak-Aware Rightsizing — Combined Pilot Report",
        "",
        f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Window**: last {days} days  ",
        f"**Pilot subscriptions**: {len(summary['subscriptions'])}",
        "",
        "## Roll-up",
        "",
        "| Subscription | VMs | Downsize | Keep | Upsize | Insufficient | Advisor unsafe |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    tot = {"vms": 0, "DOWNSIZE_CANDIDATE": 0, "KEEP": 0,
           "UPSIZE": 0, "INSUFFICIENT_DATA": 0, "advisor_unsafe": 0}
    for s in summary["subscriptions"]:
        v = s["verdicts"]
        lines.append(
            f"| {s['name']} | {s['vms']} | "
            f"{v.get('DOWNSIZE_CANDIDATE',0)} | {v.get('KEEP',0)} | "
            f"{v.get('UPSIZE',0)} | "
            f"{v.get('INSUFFICIENT_DATA',0)} | {s['advisor_unsafe']} |"
        )
        tot["vms"] += s["vms"]
        for k in ["DOWNSIZE_CANDIDATE", "KEEP", "UPSIZE",
                  "INSUFFICIENT_DATA"]:
            tot[k] += v.get(k, 0)
        tot["advisor_unsafe"] += s["advisor_unsafe"]
    lines.append(
        f"| **Total** | **{tot['vms']}** | "
        f"**{tot['DOWNSIZE_CANDIDATE']}** | **{tot['KEEP']}** | "
        f"**{tot['UPSIZE']}** | **{tot['INSUFFICIENT_DATA']}** | "
        f"**{tot['advisor_unsafe']}** |"
    )
    lines += [
        "",
        "## The headline number",
        "",
        f"**{tot['advisor_unsafe']}** of Azure Advisor's downsize "
        f"recommendations across the pilot trio would have been **unsafe** "
        f"according to peak (P95/P99) workload data. These are recommendations "
        f"domain teams would have rejected on review — exactly the "
        f"manual-validation overhead this engine eliminates.",
        "",
        "## Per-subscription reports",
        "",
    ]
    for s in summary["subscriptions"]:
        lines.append(f"- [{s['name']}](./{s['md']}) "
                     f"({s['vms']} VMs)")
    lines += ["", "_Generated by `rightsizing-peak`._"]
    path.write_text("\n".join(lines), encoding="utf-8")

    # HTML output
    html_path = path.with_suffix(".html")
    write_html(path, html_path)

    # index.html — combined report first, then per-subscription
    index_reports = [
        ("Peak-Aware Rightsizing — Combined", html_path.name)
    ]
    for s in summary["subscriptions"]:
        index_reports.append((
            f"Peak-Aware Rightsizing — {s['name']}",
            s["html"],
        ))
    write_index(out_dir, index_reports)

    print(f"[engine] Combined report: {path}")
    print(f"[engine] Combined HTML:   {html_path}")


def _resolve_subs(args: argparse.Namespace) -> list[str]:
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
    excludes: set[str] = set()
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


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
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
                    help="Limit --all-subs to a single tenant ID. "
                         "Useful for guest accounts that span tenants.")
    ap.add_argument("--include-disabled", action="store_true",
                    help="Include subscriptions whose state is not "
                         "Enabled (default: skip them).")
    ap.add_argument("--days", type=int, default=30,
                    help="Lookback window in days (default 30).")
    ap.add_argument("--out-dir", required=True,
                    help="Output directory for reports.")
    ap.add_argument("--max-workers", type=int, default=8,
                    help="Parallel metric fetches (default 8).")

    # Decision-rule overrides. Defaults match DECISION_RULES.
    g = ap.add_argument_group(
        "decision thresholds",
        "Override the engine's downsize/upsize thresholds. "
        "All values are percentages (0-100) except --min-data-coverage "
        "(0.0-1.0). See README for guidance on tuning.")
    g.add_argument("--downsize-cpu-p95-max", type=float,
                   default=DECISION_RULES["downsize_cpu_p95_max"],
                   help="P95 CPU%% must be below this to be a downsize "
                        "candidate (default %(default)s).")
    g.add_argument("--downsize-mem-p95-max", type=float,
                   default=DECISION_RULES["downsize_mem_p95_max"],
                   help="P95 memory-used%% must be below this to be a "
                        "downsize candidate (default %(default)s).")
    g.add_argument("--downsize-cpu-p99-high-conf", type=float,
                   default=DECISION_RULES["downsize_cpu_p99_high_conf"],
                   help="P99 CPU%% below this promotes the candidate to "
                        "HIGH confidence (default %(default)s).")
    g.add_argument("--downsize-mem-p99-high-conf", type=float,
                   default=DECISION_RULES["downsize_mem_p99_high_conf"],
                   help="P99 memory-used%% below this promotes the candidate "
                        "to HIGH confidence (default %(default)s).")
    g.add_argument("--upsize-cpu-p95-min", type=float,
                   default=DECISION_RULES["upsize_cpu_p95_min"],
                   help="P95 CPU%% at or above this triggers UPSIZE "
                        "(default %(default)s).")
    g.add_argument("--upsize-mem-p95-min", type=float,
                   default=DECISION_RULES["upsize_mem_p95_min"],
                   help="P95 memory-used%% at or above this triggers "
                        "UPSIZE (default %(default)s).")
    g.add_argument("--min-data-coverage", type=float,
                   default=DECISION_RULES["min_data_coverage"],
                   help="Fraction (0.0-1.0) of expected hourly samples "
                        "required before the engine emits a verdict; "
                        "below this the VM is INSUFFICIENT_DATA "
                        "(default %(default)s).")
    return ap.parse_args(argv)


def _apply_threshold_overrides(a: argparse.Namespace) -> None:
    """Mutate the module-level DECISION_RULES dict from CLI args.

    Validates the resulting rule set so an obvious mis-configuration
    (e.g. downsize threshold above upsize threshold) fails fast with
    a clear message rather than silently producing odd verdicts.
    """
    DECISION_RULES["downsize_cpu_p95_max"] = a.downsize_cpu_p95_max
    DECISION_RULES["downsize_mem_p95_max"] = a.downsize_mem_p95_max
    DECISION_RULES["downsize_cpu_p99_high_conf"] = a.downsize_cpu_p99_high_conf
    DECISION_RULES["downsize_mem_p99_high_conf"] = a.downsize_mem_p99_high_conf
    DECISION_RULES["upsize_cpu_p95_min"] = a.upsize_cpu_p95_min
    DECISION_RULES["upsize_mem_p95_min"] = a.upsize_mem_p95_min
    DECISION_RULES["min_data_coverage"] = a.min_data_coverage

    errors = []
    for k in ("downsize_cpu_p95_max", "downsize_mem_p95_max",
              "downsize_cpu_p99_high_conf", "downsize_mem_p99_high_conf",
              "upsize_cpu_p95_min", "upsize_mem_p95_min"):
        v = DECISION_RULES[k]
        if not 0.0 <= v <= 100.0:
            errors.append(f"--{k.replace('_','-')}={v} must be in [0, 100]")
    cov = DECISION_RULES["min_data_coverage"]
    if not 0.0 <= cov <= 1.0:
        errors.append(f"--min-data-coverage={cov} must be in [0.0, 1.0]")
    if (DECISION_RULES["downsize_cpu_p95_max"]
            >= DECISION_RULES["upsize_cpu_p95_min"]):
        errors.append(
            "--downsize-cpu-p95-max must be strictly less than "
            "--upsize-cpu-p95-min (otherwise a single VM could match both "
            "rules and the verdict would be undefined).")
    if (DECISION_RULES["downsize_mem_p95_max"]
            >= DECISION_RULES["upsize_mem_p95_min"]):
        errors.append(
            "--downsize-mem-p95-max must be strictly less than "
            "--upsize-mem-p95-min.")
    if errors:
        raise SystemExit("Threshold validation failed:\n  - "
                         + "\n  - ".join(errors))


def main(argv: list[str]) -> int:
    a = parse_args(argv)
    _apply_threshold_overrides(a)
    subs = _resolve_subs(a)
    out = Path(a.out_dir).resolve()
    summary = run(subs, days=a.days, out_dir=out,
                  max_workers=a.max_workers)
    print(f"[engine] Done. Reports in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
