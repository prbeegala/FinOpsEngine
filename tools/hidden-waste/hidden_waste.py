"""
hidden-waste — Continuous orphan / lifecycle waste detection.

Part of the FinOps Engine — orphan/lifecycle waste detection.

What it does
============
Pulls ten categories of "hidden waste" via the Azure Resource Graph REST
API, then estimates monthly £ cost from Cost Management. Always Resource-Graph
first (cheap, fast) then Cost-Management for actual £ on the IDs we found.

Categories
----------
1. Unattached managed disks            (microsoft.compute/disks)
2. Unused public IPs                    (microsoft.network/publicipaddresses)
3. Orphan network interfaces            (microsoft.network/networkinterfaces)
4. Stopped-not-deallocated VMs          (microsoft.compute/virtualmachines)
5. Old snapshots > 90 days              (microsoft.compute/snapshots)
6. Empty App Service Plans              (microsoft.web/serverfarms)
7. Idle load balancers (no backends)    (microsoft.network/loadbalancers)
8. Hot-tier storage accounts with very low transactional activity
                                        (microsoft.storage/storageaccounts)
9. Untouched blob containers > 90 days
                                        (microsoft.storage/.../containers)
10. Premium file shares with materially oversized provisioned quota
                                        (microsoft.storage/.../shares)

Outputs
-------
- hidden-waste-<date>.md  — exec summary, per-category headline, top-N
  resources by £.
- hidden-waste-<date>.csv — every flagged resource with sub, RG, name,
  category, monthly_gbp.
- policy/ — JSON Azure Policy starter pack for the top 3 waste classes.

Usage
-----
    python hidden_waste.py \
        --subs <subId>[,<subId>...] \
        --out-dir ./out/hidden-waste

Auth: relies on `az login`. Uses `az rest` for both Resource Graph and Cost
Management (avoids cmd.exe quoting).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# HTML report sink (shared utility — no third-party deps)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from html_sink import write_html, write_index  # noqa: E402

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _is_win() -> bool:
    return sys.platform.startswith("win")


def _q(s: str) -> str:
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


def az_rest(method: str, url: str, body: dict, max_retries: int = 6) -> dict:
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
# Resource Graph queries
# ---------------------------------------------------------------------------

GRAPH_URL = ("https://management.azure.com/providers/Microsoft.ResourceGraph/"
             "resources?api-version=2022-10-01")

QUERIES: dict[str, str] = {
    "unattached_disks": (
        "Resources "
        "| where type =~ 'microsoft.compute/disks' "
        "| where properties.diskState =~ 'Unattached' "
        "| where managedBy == '' or isnull(managedBy) "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=tostring(sku.name), "
        "  sizeGb=tolong(properties.diskSizeGB)"
    ),
    "unused_public_ips": (
        "Resources "
        "| where type =~ 'microsoft.network/publicipaddresses' "
        "| where isnull(properties.ipConfiguration) and "
        "        isnull(properties.natGateway) "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=tostring(sku.name), "
        "  tier=tostring(sku.tier)"
    ),
    "orphan_nics": (
        "Resources "
        "| where type =~ 'microsoft.network/networkinterfaces' "
        "| where isnull(properties.virtualMachine) and "
        "        isnull(properties.privateEndpoint) "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id)"
    ),
    "stopped_not_deallocated": (
        "Resources "
        "| where type =~ 'microsoft.compute/virtualmachines' "
        "| extend power = tostring(properties.extended.instanceView.powerState.code) "
        "| where power == 'PowerState/stopped' "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=tostring(properties.hardwareProfile.vmSize), "
        "  power"
    ),
    "old_snapshots": (
        "Resources "
        "| where type =~ 'microsoft.compute/snapshots' "
        "| extend ageDays=datetime_diff('day', now(), todatetime(properties.timeCreated)) "
        "| where ageDays >= 90 "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sizeGb=tolong(properties.diskSizeGB), "
        "  ageDays"
    ),
    "empty_asp": (
        "Resources "
        "| where type =~ 'microsoft.web/serverfarms' "
        "| where tolong(properties.numberOfSites) == 0 "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=tostring(sku.name), "
        "  tier=tostring(sku.tier)"
    ),
    "idle_load_balancers": (
        "Resources "
        "| where type =~ 'microsoft.network/loadbalancers' "
        "| where tostring(sku.name) =~ 'Standard' "
        "| extend ruleCount = array_length(properties.loadBalancingRules), "
        "         poolCount = array_length(properties.backendAddressPools) "
        "| where (ruleCount == 0 or isnull(ruleCount)) and "
        "        (poolCount == 0 or isnull(poolCount)) "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=tostring(sku.name), "
        "  ruleCount, poolCount"
    ),
    # Hot-tier storage accounts; a separate metric pass narrows to
    # genuinely cold workloads (low transactions, non-trivial size).
    "storage_cold_tier": (
        "Resources "
        "| where type =~ 'microsoft.storage/storageaccounts' "
        "| where tostring(properties.accessTier) =~ 'Hot' "
        "| where tostring(kind) in~ "
        "        ('StorageV2', 'BlobStorage', 'BlockBlobStorage') "
        "| extend ageDays = datetime_diff('day', now(), "
        "        todatetime(properties.creationTime)) "
        "| where ageDays >= 30 "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=tostring(sku.name), "
        "  tier=tostring(properties.accessTier), "
        "  ageDays"
    ),
    "storage_untouched_container": (
        "Resources "
        "| where type =~ "
        "        'microsoft.storage/storageaccounts/blobservices/containers' "
        "| extend ageDays = datetime_diff('day', now(), "
        "        todatetime(properties.lastModifiedTime)) "
        "| where ageDays >= 90 "
        "| extend account = tostring(split(name, '/')[0]) "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  ageDays, account"
    ),
    # Joins shares to parent account so we can filter on Premium files SKU.
    "storage_oversize_premium": (
        "Resources "
        "| where type =~ "
        "        'microsoft.storage/storageaccounts/fileservices/shares' "
        "| extend acctId = tolower(tostring(split(tolower(id), "
        "        '/fileservices/')[0])) "
        "| join kind=inner ( "
        "    Resources "
        "    | where type =~ 'microsoft.storage/storageaccounts' "
        "    | where tostring(kind) =~ 'FileStorage' "
        "    | project acctId = tolower(id), "
        "              accountSku=tostring(sku.name) "
        "  ) on acctId "
        "| extend quotaGb = tolong(properties.shareQuota) "
        "| where quotaGb >= 1024 "
        "| project subscriptionId, resourceGroup, name, location, "
        "  id=tolower(id), "
        "  sku=accountSku, "
        "  sizeGb=quotaGb"
    ),
}


def graph_query(query: str, subs: list[str]) -> list[dict]:
    """Page through Resource Graph for the given KQL across subs."""
    rows: list[dict] = []
    skip_token = None
    while True:
        body = {
            "subscriptions": subs,
            "query": query,
            "options": {"resultFormat": "objectArray", "$top": 1000},
        }
        if skip_token:
            body["options"]["$skipToken"] = skip_token
        resp = az_rest("POST", GRAPH_URL, body)
        rows.extend(resp.get("data", []))
        skip_token = resp.get("$skipToken")
        if not skip_token:
            break
    return rows


# ---------------------------------------------------------------------------
# Azure Monitor metric helpers (used by storage detectors)
# ---------------------------------------------------------------------------

def az_metrics_summary(resource_id: str, *, metric: str, aggregation: str,
                       days: int = 30) -> tuple[float | None, int]:
    """Return (aggregate, sample_count) for ``metric`` on ``resource_id``.

    ``aggregation`` is the API aggregation passed to ``az monitor metrics
    list`` (``Total``, ``Average`` etc.). The returned aggregate is the
    sum-of-totals or mean-of-averages over the window — coarse on purpose;
    the storage detectors only care about order-of-magnitude.

    Returns ``(None, 0)`` on any failure (permission denied, metric
    unsupported, throttled). Callers must treat ``None`` as "metric
    unavailable" rather than "no activity".
    """
    end = datetime.now(timezone.utc) - timedelta(seconds=1)
    start = end - timedelta(days=days)
    args = [
        "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", metric,
        "--aggregation", aggregation,
        "--interval", "P1D",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    ]
    try:
        data = az(args)
    except RuntimeError:
        return (None, 0)
    series = data.get("value") or []
    if not series:
        return (None, 0)
    ts = series[0].get("timeseries") or []
    if not ts:
        return (None, 0)
    pts = ts[0].get("data") or []
    key = aggregation.lower()
    vals = [float(p.get(key, 0.0) or 0.0) for p in pts if key in p]
    if not vals:
        return (0.0, 0)
    if aggregation.lower() == "total":
        return (sum(vals), len(vals))
    return (sum(vals) / len(vals), len(vals))


def az_metric_per_dimension(resource_id: str, *, metric: str, aggregation: str,
                            dimension: str, days: int = 30
                            ) -> dict[str, float] | None:
    """Pull a metric grouped by a dimension; return ``{dim_value -> aggregate}``.

    Returns ``None`` on failure so callers can distinguish "metric
    unavailable" from "all values are zero".
    """
    end = datetime.now(timezone.utc) - timedelta(seconds=1)
    start = end - timedelta(days=days)
    args = [
        "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", metric,
        "--aggregation", aggregation,
        "--interval", "P1D",
        "--filter", f"{dimension} eq '*'",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end-time", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    ]
    try:
        data = az(args)
    except RuntimeError:
        return None
    series = data.get("value") or []
    if not series:
        return None
    out: dict[str, float] = {}
    key = aggregation.lower()
    for ts in series[0].get("timeseries", []) or []:
        meta = {m["name"]["value"]: m["value"]
                for m in ts.get("metadatavalues", []) or []
                if isinstance(m.get("name"), dict)}
        dim_val = meta.get(dimension, "")
        if not dim_val:
            continue
        pts = ts.get("data") or []
        vals = [float(p.get(key, 0.0) or 0.0) for p in pts if key in p]
        if not vals:
            continue
        agg = (sum(vals) if aggregation.lower() == "total"
               else sum(vals) / len(vals))
        out[dim_val.lower()] = agg
    return out


# ---------------------------------------------------------------------------
# Storage detector refinement
# ---------------------------------------------------------------------------

# Cold-tier thresholds. A Hot-tier account is "actually cold" if it stores
# meaningful data AND sees very few transactions. Both are coarse — we lean
# conservative to keep the false-positive rate down.
STORAGE_COLD_TIER_MIN_USED_GIB = 100.0
STORAGE_COLD_TIER_MAX_TX_30D = 30_000  # ~1,000 / day average

# Premium files: West-Europe LRS list price. Hand-coded constant — the
# engines stay stdlib-only so we cannot pull the retail-price API.
PREMIUM_FILES_GBP_PER_GIB_MO = 0.16
# Only flag if quota is non-trivial AND used << provisioned.
STORAGE_OVERSIZE_PREMIUM_MIN_QUOTA_GIB = 1024  # filtered in KQL too
STORAGE_OVERSIZE_PREMIUM_USED_FRACTION = 0.5


def refine_storage_findings(raw: list["Finding"]) -> list["Finding"]:
    """Best-effort metric-based refinement for the storage detectors.

    - ``storage_cold_tier``: keeps only Hot accounts with <30k transactions
      over 30d AND >=100 GiB of stored data. If metrics are unavailable
      (permissions / throttle) the candidate is kept with ``extra`` flagged
      so the output is honest about the uncertainty.
    - ``storage_oversize_premium``: keeps only premium file shares whose
      ``FileCapacity`` is < 50% of the provisioned quota. Uses the parent
      account's ``fileServices/default`` metrics with a per-share filter;
      again, missing metrics keeps the candidate with an ``extra`` note.
    """
    kept: list["Finding"] = []
    # Group oversize-premium candidates by parent account so we make one
    # metrics call per account, not per share.
    oversize_by_account: dict[str, list[Finding]] = {}

    for f in raw:
        if f.category == "storage_cold_tier":
            tx, _ = az_metrics_summary(f.resource_id, metric="Transactions",
                                       aggregation="Total", days=30)
            used_bytes, _ = az_metrics_summary(f.resource_id,
                                               metric="UsedCapacity",
                                               aggregation="Average",
                                               days=30)
            if tx is None or used_bytes is None:
                f.extra = "metrics unavailable; verify manually"
                kept.append(f)
                continue
            tx_val = tx
            used_gib = (used_bytes / (1024 ** 3))
            if (tx_val < STORAGE_COLD_TIER_MAX_TX_30D
                    and used_gib >= STORAGE_COLD_TIER_MIN_USED_GIB):
                f.size_gb = int(used_gib)
                f.extra = (f"~{used_gib:,.0f} GiB stored; "
                           f"~{tx_val:,.0f} tx / 30d")
                kept.append(f)
            # else drop — Hot tier looks justified
        elif f.category == "storage_oversize_premium":
            # Parent account id = strip "/fileServices/default/shares/<n>"
            parent = f.resource_id.split("/fileservices/")[0]
            oversize_by_account.setdefault(parent, []).append(f)
        else:
            kept.append(f)

    # Resolve oversize-premium with one metric call per parent account.
    for parent, shares in oversize_by_account.items():
        file_svc_id = f"{parent}/fileServices/default"
        per_share = az_metric_per_dimension(
            file_svc_id, metric="FileCapacity",
            aggregation="Average", dimension="FileShare", days=30)
        for s in shares:
            quota = float(s.size_gb or 0)
            # ARG `name` is "<account>/default/<share>"; share name is last seg.
            share_name = s.name.split("/")[-1].lower() if s.name else ""
            if per_share is None:
                # Metric unavailable — keep the share but be transparent.
                s.extra = (f"quota {quota:,.0f} GiB; usage unknown "
                           f"(metric unavailable)")
                kept.append(s)
                continue
            used_bytes = per_share.get(share_name)
            if used_bytes is None:
                s.extra = (f"quota {quota:,.0f} GiB; usage unknown "
                           f"(no datapoints)")
                kept.append(s)
                continue
            used_gib = used_bytes / (1024 ** 3)
            if (quota >= STORAGE_OVERSIZE_PREMIUM_MIN_QUOTA_GIB
                    and used_gib <= quota
                                * STORAGE_OVERSIZE_PREMIUM_USED_FRACTION):
                s.recoverable_gb = max(0.0, quota - used_gib)
                s.extra = (f"quota {quota:,.0f} GiB; "
                           f"used ~{used_gib:,.0f} GiB; "
                           f"recoverable ~{s.recoverable_gb:,.0f} GiB")
                kept.append(s)
            # else drop — share is sized appropriately
    return kept




def fetch_resource_costs(sub_id: str, days: int = 30,
                         wanted: set[str] | None = None,
                         max_pages: int = 5) -> dict[str, float]:
    """Return {resourceId(lowercased) -> £/last-N-days} for sub_id (paged).

    If `wanted` is provided, paging stops as soon as every id in `wanted`
    has been seen (typical case: we only need cost for flagged orphans, not
    the entire estate). `max_pages` caps total Cost Management round-trips
    per subscription as a hard safety net.
    """
    end = datetime.now(timezone.utc) - timedelta(seconds=1)
    start = end - timedelta(days=days)
    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start.strftime("%Y-%m-%dT00:00:00Z"),
            "to":   end.strftime("%Y-%m-%dT23:59:59Z"),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }
    base_url = (f"https://management.azure.com/subscriptions/{sub_id}"
                f"/providers/Microsoft.CostManagement/query"
                f"?api-version=2023-11-01")
    out: dict[str, float] = {}
    url = base_url
    page = 0
    while url and page < max_pages:
        page += 1
        try:
            resp = az_rest("POST", url, body)
        except RuntimeError as e:
            print(f"  ! Cost lookup failed for {sub_id} (page {page}): {e}",
                  flush=True)
            break
        cols = [c["name"] for c in resp.get("properties", {}).get("columns", [])]
        for r in resp.get("properties", {}).get("rows", []):
            d = dict(zip(cols, r))
            rid = str(d.get("ResourceId", "")).lower()
            cost = float(d.get("Cost", 0.0) or 0.0)
            if rid:
                out[rid] = out.get(rid, 0.0) + cost
        if wanted and wanted.issubset(out.keys()):
            break
        url = resp.get("properties", {}).get("nextLink") \
            or resp.get("nextLink")
    return out


# ---------------------------------------------------------------------------
# Disk pricing fallback (when CM has no data for an unattached disk)
# ---------------------------------------------------------------------------

# Premium SSD (Pxx) and Standard SSD (Exx) GBP/mo at West-Europe list price,
# rounded conservative averages — used only when Cost Management returned no
# data for the disk (which happens for never-attached disks).
DISK_TIER_PRICES_GBP = {
    "P": {  # Premium SSD
        4: 0.45, 6: 0.79, 10: 1.46, 15: 2.92, 20: 5.85, 30: 11.70, 40: 23.40,
        50: 46.80, 60: 93.60, 70: 187.20, 80: 374.40,
    },
    "E": {  # Standard SSD
        1: 0.30, 4: 0.60, 6: 1.20, 10: 2.40, 15: 4.80, 20: 9.60, 30: 19.20,
        40: 38.40, 50: 76.80, 60: 153.60, 70: 307.20, 80: 614.40,
    },
}


def estimate_disk_monthly_gbp(size_gb: int, sku: str) -> float:
    """Coarse monthly price estimate for an unattached disk."""
    if size_gb <= 0:
        return 0.0
    if sku.startswith("Premium"):
        # Map size to Pxx tier
        tiers = [(4, 32), (6, 64), (10, 128), (15, 256), (20, 512),
                 (30, 1024), (40, 2048), (50, 4096), (60, 8192),
                 (70, 16384), (80, 32767)]
        for label, max_gb in tiers:
            if size_gb <= max_gb:
                return DISK_TIER_PRICES_GBP["P"].get(label, 0.0)
        return DISK_TIER_PRICES_GBP["P"][80]
    if sku.startswith("StandardSSD"):
        tiers = [(1, 4), (4, 32), (6, 64), (10, 128), (15, 256), (20, 512),
                 (30, 1024), (40, 2048), (50, 4096), (60, 8192),
                 (70, 16384), (80, 32767)]
        for label, max_gb in tiers:
            if size_gb <= max_gb:
                return DISK_TIER_PRICES_GBP["E"].get(label, 0.0)
    # Standard HDD — cheap, ~£0.0345/GB
    return size_gb * 0.0345


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    category: str
    sub_id: str
    sub_name: str
    rg: str
    name: str
    location: str
    resource_id: str
    sku: str = ""
    size_gb: int = 0
    age_days: int = 0
    extra: str = ""
    monthly_gbp: float = 0.0
    cost_source: str = ""   # 'cost_mgmt' / 'estimate' / 'unknown'
    recoverable_gb: float = 0.0   # used by storage_oversize_premium


def fetch_sub_name(sub_id: str) -> str:
    try:
        resp = az(["account", "show", "--subscription", sub_id])
        return resp.get("name", sub_id)
    except RuntimeError:
        return sub_id


CATEGORY_LABELS = {
    "unattached_disks":       "Unattached managed disks",
    "unused_public_ips":      "Unused public IPs",
    "orphan_nics":            "Orphan NICs",
    "stopped_not_deallocated": "Stopped-not-deallocated VMs",
    "old_snapshots":          "Old snapshots (>90d)",
    "empty_asp":              "Empty App Service Plans",
    "idle_load_balancers":    "Idle Standard load balancers",
    "storage_cold_tier":          "Hot-tier storage accounts (cold workload)",
    "storage_untouched_container": "Untouched blob containers (>90d)",
    "storage_oversize_premium":   "Oversized premium file shares",
}


def annotate_cost(finding: Finding, cost_map: dict[str, float],
                  days: int) -> None:
    rid = finding.resource_id.lower()
    if rid in cost_map and cost_map[rid] > 0:
        # last-N-days cost → monthly equivalent
        finding.monthly_gbp = cost_map[rid] * 30.0 / max(days, 1)
        finding.cost_source = "cost_mgmt"
        return
    # fallbacks per category
    if finding.category == "unattached_disks" and finding.size_gb:
        finding.monthly_gbp = estimate_disk_monthly_gbp(
            finding.size_gb, finding.sku or "Standard")
        finding.cost_source = "estimate"
    elif finding.category == "unused_public_ips":
        # Standard static = ~£3/mo, Basic = ~£0
        finding.monthly_gbp = 3.0 if finding.sku.lower() == "standard" else 0.0
        finding.cost_source = "estimate"
    elif finding.category == "old_snapshots" and finding.size_gb:
        # Standard HDD pricing for incremental snapshots ~£0.04/GB-month
        finding.monthly_gbp = finding.size_gb * 0.04
        finding.cost_source = "estimate"
    elif finding.category == "idle_load_balancers":
        # Standard LB hourly + ~5 rules — we'll use £15/mo as conservative
        finding.monthly_gbp = 15.0
        finding.cost_source = "estimate"
    elif finding.category == "storage_cold_tier":
        # Account is hot-tier and looks cold but Cost Management has
        # no row — likely a tiny / drain-only account. Don't fabricate
        # savings; flag for manual review.
        finding.monthly_gbp = 0.0
        finding.cost_source = "unknown"
    elif finding.category == "storage_untouched_container":
        # Containers aren't a Cost Management dimension — surface as
        # hygiene-only (same posture as orphan_nics).
        finding.monthly_gbp = 0.0
        finding.cost_source = "unknown"
    elif finding.category == "storage_oversize_premium":
        # Premium files line item is roughly quota * GBP/GiB-mo. We
        # report the recoverable slice as the savings estimate.
        if finding.recoverable_gb > 0:
            finding.monthly_gbp = (finding.recoverable_gb
                                   * PREMIUM_FILES_GBP_PER_GIB_MO)
            finding.cost_source = "estimate"
        elif finding.size_gb:
            # Usage unknown — report the full provisioned-line ceiling
            # so the row sorts proportional to its bill, but tag clearly
            # as estimate (operator must confirm before remediating).
            finding.monthly_gbp = (finding.size_gb
                                   * PREMIUM_FILES_GBP_PER_GIB_MO)
            finding.cost_source = "estimate"
        else:
            finding.monthly_gbp = 0.0
            finding.cost_source = "unknown"
    else:
        finding.monthly_gbp = 0.0
        finding.cost_source = "unknown"


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def gbp(x: float) -> str:
    if abs(x) >= 1000:
        return f"£{x:,.0f}"
    return f"£{x:,.2f}"


def write_csv(path: Path, findings: list[Finding]) -> None:
    fields = ["category", "sub_id", "sub_name", "resource_group", "name",
              "location", "resource_id", "sku", "size_gb", "age_days",
              "extra", "monthly_gbp", "cost_source"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for v in sorted(findings, key=lambda f: -f.monthly_gbp):
            w.writerow([
                v.category, v.sub_id, v.sub_name, v.rg, v.name, v.location,
                v.resource_id, v.sku, v.size_gb, v.age_days, v.extra,
                f"{v.monthly_gbp:.2f}", v.cost_source,
            ])


def write_md(path: Path, findings: list[Finding], sub_count: int) -> None:
    by_cat: dict[str, list[Finding]] = {}
    for f in findings:
        by_cat.setdefault(f.category, []).append(f)

    total_monthly = sum(f.monthly_gbp for f in findings)
    total_count = len(findings)

    lines = [
        "# Hidden Waste & Lifecycle",
        "",
        f"**Generated**: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}  ",
        f"**Subscriptions in scope**: {sub_count}",
        "",
        "## Headline",
        "",
        f"- Total flagged resources: **{total_count:,}**",
        f"- Estimated monthly £ recoverable (deletion or rightsizing): "
        f"**{gbp(total_monthly)}**",
        f"- Annualised: **{gbp(total_monthly * 12)}**",
        "",
        "## By category",
        "",
        "| Category | Count | Monthly £ | Annualised £ |",
        "|---|---:|---:|---:|",
    ]
    for cat in QUERIES.keys():
        items = by_cat.get(cat, [])
        if not items:
            continue
        c_total = sum(f.monthly_gbp for f in items)
        lines.append(
            f"| {CATEGORY_LABELS[cat]} | {len(items):,} | "
            f"{gbp(c_total)} | {gbp(c_total * 12)} |"
        )

    # Top 3 worst classes for Azure Policy starter pack
    cat_costs = sorted(
        ((cat, sum(f.monthly_gbp for f in by_cat.get(cat, [])))
         for cat in QUERIES.keys() if by_cat.get(cat)),
        key=lambda t: -t[1],
    )
    top3 = [c for c, _ in cat_costs[:3]]

    lines += [
        "",
        "## Top 25 individual offenders (across all categories)",
        "",
        "| Sub | RG | Resource | Category | Monthly £ | Source |",
        "|---|---|---|---|---:|---|",
    ]
    for f in sorted(findings, key=lambda f: -f.monthly_gbp)[:25]:
        lines.append(
            f"| {f.sub_name} | {f.rg} | {f.name} | "
            f"{CATEGORY_LABELS[f.category]} | {gbp(f.monthly_gbp)} | "
            f"{f.cost_source} |"
        )

    lines += [
        "",
        "## Recommended Azure Policy guardrails (top 3 by £)",
        "",
        "Starter-pack JSON for the top 3 waste classes is in `policy/`. These "
        "are *audit-mode* policies — they flag new offenders without "
        "blocking. Promote to `deny` for new-resource creation only after a "
        "30-day audit cycle.",
        "",
    ]
    for cat in top3:
        lines.append(f"- **{CATEGORY_LABELS[cat]}** — see "
                     f"`policy/{cat}.audit.json`")

    lines += [
        "",
        "## Cost-source notes",
        "",
        "- `cost_mgmt` — actual £ from last 30 days of Cost Management.",
        "- `estimate` — list-price estimate (used when Cost Management "
        "  has no data for that resource ID, e.g. never-attached disks).",
        "- `unknown` — no cost attribution available; verify manually.",
        "",
        "_Generated by `hidden-waste`._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Azure Policy starter pack
# ---------------------------------------------------------------------------

POLICY_TEMPLATES = {
    "unattached_disks": {
        "displayName": "Audit unattached managed disks",
        "description": "Flags managed disks that have been unattached for "
                       "any length of time. Promote to deny only after "
                       "30-day audit.",
        "mode": "All",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Compute/disks"},
                    {"field": "Microsoft.Compute/disks/diskState",
                     "equals": "Unattached"},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
    },
    "unused_public_ips": {
        "displayName": "Audit unused Public IP addresses",
        "description": "Flags Public IP addresses with no IP configuration "
                       "and no NAT-gateway association — typical orphan "
                       "after a VM/LB delete.",
        "mode": "All",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Network/publicIPAddresses"},
                    {"field": "Microsoft.Network/publicIPAddresses/"
                              "ipConfiguration.id",
                     "exists": "false"},
                    {"field": "Microsoft.Network/publicIPAddresses/"
                              "natGateway.id",
                     "exists": "false"},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
    },
    "empty_asp": {
        "displayName": "Audit empty App Service Plans",
        "description": "Flags App Service Plans with zero hosted apps. "
                       "Common waste class after app deletion.",
        "mode": "All",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Web/serverfarms"},
                    {"field": "Microsoft.Web/serverfarms/numberOfSites",
                     "equals": 0},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
    },
    "stopped_not_deallocated": {
        "displayName": "Audit stopped (not deallocated) VMs",
        "description": "Flags VMs in 'Stopped' (allocated) state which "
                       "still incur compute charges — should be "
                       "deallocated.",
        "mode": "Indexed",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Compute/virtualMachines"},
                    # NOTE: powerState isn't directly auditable via Policy;
                    # this template is a starter — pair with an Azure
                    # Automation runbook that tags allocated-stopped VMs.
                ]
            },
            "then": {"effect": "auditIfNotExists",
                     "details": {"type": "Microsoft.Insights/diagnosticSettings"}}
        },
        "parameters": {},
        "_note": "Power-state isn't directly queryable from Policy. Pair "
                 "with an Automation/Function App that tags VMs with their "
                 "current powerState daily, then audit on the tag.",
    },
    "old_snapshots": {
        "displayName": "Audit snapshots older than 90 days",
        "description": "Flags compute snapshots created more than 90 days "
                       "ago — typical retention waste.",
        "mode": "Indexed",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "field": "type",
                "equals": "Microsoft.Compute/snapshots"
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
        "_note": "Policy can't filter by `timeCreated` directly — use this "
                 "blanket rule combined with a Workbook/KQL filter on age.",
    },
    "idle_load_balancers": {
        "displayName": "Audit idle Standard load balancers",
        "description": "Flags Standard load balancers with no LB rules — "
                       "incurs hourly charge regardless of traffic.",
        "mode": "Indexed",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Network/loadBalancers"},
                    {"field": "Microsoft.Network/loadBalancers/sku.name",
                     "equals": "Standard"},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
    },
    "orphan_nics": {
        "displayName": "Audit orphan network interfaces",
        "description": "Flags NICs with no virtualMachine or "
                       "privateEndpoint association.",
        "mode": "All",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Network/networkInterfaces"},
                    {"field": "Microsoft.Network/networkInterfaces/"
                              "virtualMachine.id",
                     "exists": "false"},
                    {"field": "Microsoft.Network/networkInterfaces/"
                              "privateEndpoint.id",
                     "exists": "false"},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
    },
    "storage_cold_tier": {
        "displayName": "Audit storage accounts in Hot access tier",
        "description": "Flags general-purpose v2 / blob storage accounts "
                       "whose default access tier is Hot. Pair with a "
                       "Workbook query on the Transactions / UsedCapacity "
                       "metrics to narrow to the genuinely cold workloads "
                       "before promoting to deny.",
        "mode": "Indexed",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals": "Microsoft.Storage/storageAccounts"},
                    {"field": "kind",
                     "in": ["StorageV2", "BlobStorage", "BlockBlobStorage"]},
                    {"field": "Microsoft.Storage/storageAccounts/"
                              "accessTier",
                     "equals": "Hot"},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
        "_note": "Policy can't read transaction or used-capacity metrics. "
                 "Treat the audit hits as a candidate list and confirm "
                 "via the engine's metric refinement pass before action.",
    },
    "storage_untouched_container": {
        "displayName": "Audit blob containers (paired-Workbook detector)",
        "description": "Flags blob containers — used as a counting hook "
                       "only. Container `lastModifiedTime` cannot be "
                       "evaluated by Policy.",
        "mode": "Indexed",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "field": "type",
                "equals":
                    "Microsoft.Storage/storageAccounts/blobServices/containers"
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
        "_note": "Policy cannot filter on container lastModifiedTime. Use "
                 "this blanket rule with a Workbook KQL filter on "
                 "`properties.lastModifiedTime` for the actual signal.",
    },
    "storage_oversize_premium": {
        "displayName": "Audit large premium file shares",
        "description": "Flags premium file shares with a provisioned quota "
                       "of >=1 TiB. Premium files bill on provisioned "
                       "capacity, so oversized quotas are pure waste.",
        "mode": "Indexed",
        "policyType": "Custom",
        "policyRule": {
            "if": {
                "allOf": [
                    {"field": "type",
                     "equals":
                         "Microsoft.Storage/storageAccounts/fileServices/shares"},
                    {"field":
                         "Microsoft.Storage/storageAccounts/fileServices/"
                         "shares/shareQuota",
                     "greaterOrEquals": 1024},
                ]
            },
            "then": {"effect": "audit"}
        },
        "parameters": {},
        "_note": "Policy can audit on shareQuota but cannot read the "
                 "FileCapacity metric. Confirm via the engine's metric "
                 "pass (or a Workbook) before taking action.",
    },
}


def write_policy_starter_pack(out_dir: Path,
                              top3_categories: list[str]) -> None:
    pol_dir = out_dir / "policy"
    pol_dir.mkdir(exist_ok=True)
    # Always emit all available templates (engineers might want them) but
    # name top-3 in the README.
    for cat, body in POLICY_TEMPLATES.items():
        path = pol_dir / f"{cat}.audit.json"
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(subs: list[str], out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")

    sub_names = {sid: fetch_sub_name(sid) for sid in subs}

    # Pull all categories via Resource Graph (one query each, paged).
    raw_findings: list[Finding] = []
    for cat, query in QUERIES.items():
        print(f"[engine] Querying: {CATEGORY_LABELS[cat]}…")
        rows = graph_query(query, subs)
        print(f"  ✓ {len(rows)} rows")
        for r in rows:
            f = Finding(
                category=cat,
                sub_id=r.get("subscriptionId", ""),
                sub_name=sub_names.get(r.get("subscriptionId", ""), ""),
                rg=r.get("resourceGroup", ""),
                name=r.get("name", ""),
                location=r.get("location", ""),
                resource_id=r.get("id", ""),
                sku=str(r.get("sku") or r.get("tier") or ""),
                size_gb=int(r.get("sizeGb") or 0),
                age_days=int(r.get("ageDays") or 0),
                extra=str(r.get("power") or r.get("ruleCount") or ""),
            )
            raw_findings.append(f)

    # Storage detectors need a metric-based second pass to filter the
    # noisy "Hot tier" / "premium files quota" candidates down to the
    # ones that are genuinely waste. Best-effort: missing metrics keep
    # the candidate with an explanatory ``extra``.
    storage_cats = {"storage_cold_tier", "storage_oversize_premium"}
    if any(f.category in storage_cats for f in raw_findings):
        print("[engine] Refining storage candidates with Azure Monitor "
              "metrics (best-effort; missing metrics are kept and tagged)…",
              flush=True)
        raw_findings = refine_storage_findings(raw_findings)

    # Enrich with actual £ from Cost Management (one query per sub,
    # short-circuited as soon as every flagged resource for that sub
    # has been priced).
    print(f"[engine] Pulling 30-day Cost Management data for "
          f"{len(subs)} sub(s)…", flush=True)
    cost_map_full: dict[str, float] = {}
    by_sub: dict[str, set[str]] = {}
    for f in raw_findings:
        rid = (f.resource_id or "").lower()
        if not rid:
            continue
        by_sub.setdefault(f.sub_id, set()).add(rid)
    for sid in subs:
        wanted = by_sub.get(sid, set())
        if not wanted:
            print(f"  • {sub_names[sid]}: no findings, skipping cost lookup",
                  flush=True)
            continue
        m = fetch_resource_costs(sid, days=30, wanted=wanted)
        cost_map_full.update(m)
        matched = len(wanted & set(m.keys()))
        print(f"  ✓ {sub_names[sid]}: {len(m)} priced resources "
              f"({matched}/{len(wanted)} flagged matched)", flush=True)

    for f in raw_findings:
        annotate_cost(f, cost_map_full, days=30)

    # Output
    md_path = out_dir / f"hidden-waste-{date}.md"
    csv_path = out_dir / f"hidden-waste-{date}.csv"
    html_path = out_dir / f"hidden-waste-{date}.html"
    write_csv(csv_path, raw_findings)
    write_md(md_path, raw_findings, len(subs))
    write_html(md_path, html_path)
    write_index(out_dir, [("Hidden Waste & Lifecycle", html_path.name)])

    # Top-3 waste classes by £ → policy starter pack.
    cat_costs = sorted(
        ((cat, sum(f.monthly_gbp for f in raw_findings if f.category == cat))
         for cat in QUERIES.keys()),
        key=lambda t: -t[1],
    )
    top3 = [c for c, v in cat_costs[:3] if v > 0]
    tool_dir = Path(__file__).resolve().parent
    write_policy_starter_pack(tool_dir, top3)

    total_monthly = sum(f.monthly_gbp for f in raw_findings)
    print(f"[engine] Done.")
    print(f"  - {md_path}")
    print(f"  - {html_path}")
    print(f"  - {csv_path}")
    print(f"  - {tool_dir / 'policy'}/  (audit-mode policies for all "
          f"7 classes)")
    print(f"[engine] Total flagged: {len(raw_findings):,} resources, "
          f"~£{total_monthly:,.0f}/mo (~£{total_monthly * 12:,.0f}/yr).")


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
    ap.add_argument("--out-dir", required=True, help="Output directory.")
    args = ap.parse_args()
    subs = _resolve_subs(args)
    run(subs, Path(args.out_dir))


if __name__ == "__main__":
    main()
