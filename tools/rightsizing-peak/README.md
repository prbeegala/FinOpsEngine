# rightsizing-peak

Peak-aware (P95 / P99) VM rightsizing engine. A safer companion to Azure
Advisor's "Cost — Resize Virtual Machine" recommendations for spiky /
batch / retail / month-end workloads where Advisor's default 7-day
lookback and 30-min bucketed aggregation hide the peak that justifies
the current SKU.

> **Common misconception:** Advisor does **not** use simple averages. Per
> [Microsoft Learn][advisor-resize] its resize algorithm uses **P95 of
> CPU and Outbound Network** and **P99 of Memory** on a 7-day default
> window, with samples bucketed into 30-minute intervals taken as the
> *max of 1-minute averages*. The headline value of `rightsizing-peak`
> is therefore **not** "P95 vs averages" — it is **(a)** a 30-day
> default window that catches weekly / month-end peaks Advisor's 7-day
> window misses, **(b)** per-hour true `Maximum` CPU and per-hour
> `Minimum` `Available Memory Bytes` (a stricter bucketing than
> Advisor's max-of-1-min-averages), **(c)** explicit exclusion of
> AKS / Databricks-managed VMs, and **(d)** a deterministic Advisor-diff
> that flags any of Advisor's downsizes this engine would deem unsafe.

[advisor-resize]: https://learn.microsoft.com/azure/advisor/advisor-cost-recommendations#resize-sku-recommendations

## What it does

For every non-managed VM in the target subscription(s):

1. Pulls 30+ days of `Percentage CPU` (avg + max), `Available Memory Bytes`
   (min + avg), and `Disk Read Operations/sec` and `Network In Total` from
   **Azure Monitor** at PT1H grain.
2. Looks up the SKU's vCPU + memory capacity via `az vm list-skus`, so memory
   bytes can be converted to memory %.
3. Applies a deterministic decision tree:
   - `DOWNSIZE_CANDIDATE` if P95 CPU < 40% AND P95 mem-used < 50%.
     `HIGH` confidence when P99 is also low; `MEDIUM` otherwise.
   - `UPSIZE_WARNING` if P95 CPU ≥ 80% OR P95 mem-used ≥ 85%.
   - `INSUFFICIENT_DATA` if metric coverage < 80% of the window.
   - `KEEP` otherwise.
4. **Diffs against Azure Advisor**: every Advisor "Cost — Resize VM"
   recommendation where this engine emits `UPSIZE_WARNING` or `KEEP` is
   flagged `advisor_unsafe = true`. *This is the headline metric.*
5. Emits per-subscription Markdown + CSV reports plus a combined roll-up.

## Excluded by design

- `databricks-rg-*` and `MC_*` resource groups (managed by parent service).
- VMs whose name starts `aks-` (AKS-managed).
- VMs without a `vmSize` property (corrupt / unusual rows).

## Usage

Pick a scope. The engine requires **either** `--subs` (explicit list) **or**
`--all-subs` (tenant-wide), not both.

```pwsh
# Explicit subscription list
python rightsizing_peak.py `
  --subs "<subId1>,<subId2>,<subId3>" `
  --days 30 `
  --out-dir ./out/peak-rightsizing `
  --max-workers 8

# Every enabled subscription in the current tenant
python rightsizing_peak.py `
  --all-subs `
  --days 30 `
  --out-dir ./out/peak-rightsizing

# Tenant-wide, but skip sandboxes / archive
python rightsizing_peak.py `
  --all-subs `
  --exclude-subs "sandbox-1,sandbox-2,archive-prod" `
  --tenant "<tenant-guid>" `
  --days 30 `
  --out-dir ./out/peak-rightsizing
```

`az login` required. Uses Azure CLI under the hood. Expect ~1–2 min per 100
VMs (rate-limited by Azure Monitor's `metrics list` throttle, not by this
engine).

### Scope flags

| Flag | Purpose |
|---|---|
| `--subs <a,b,c>` | Run against this exact list. Accepts IDs or display names. |
| `--all-subs` | Enumerate `az account list` and run against every **Enabled** subscription. |
| `--exclude-subs <a,b>` | When using `--all-subs`, skip these IDs/names (typical: sandboxes, frozen archives). |
| `--tenant <guid>` | Limit `--all-subs` to a single tenant — useful for guest accounts. |
| `--include-disabled` | Include subs whose state is not Enabled (default: skip). |

## Outputs

- `<sub>-peak-rightsizing-<date>.csv` — one row per VM with all metrics +
  decision + confidence + proposed_size + advisor_unsafe.
- `<sub>-peak-rightsizing-<date>.md` — human-readable per-sub summary.
- `peak-rightsizing-combined-<date>.md` — roll-up table + headline number.

## Workbook

`workbook-peak-rightsizing.json` is an Azure Workbook template that reads
the same data live from Resource Graph + Azure Monitor metrics. Import it
via **Azure Portal → Monitor → Workbooks → New → Advanced editor → Gallery
Template**.

For a richer view, ingest the engine's CSV into a Log Analytics custom table
(`PeakRightsizing_CL`) and extend the workbook with a join.

## Decision rules — single source of truth

The thresholds live in `DECISION_RULES` at the top of `rightsizing_peak.py`.
They are deterministic by design — no machine learning, no auto-tuning. The
defaults are conservative; teams typically loosen them only after several
nightly cycles validate the output.

```python
DECISION_RULES = {
    "downsize_cpu_p95_max":    40.0,   # peak CPU under 40% over the window
    "downsize_mem_p95_max":    50.0,   # peak mem-used under 50%
    "downsize_cpu_p99_high_conf": 50.0,
    "downsize_mem_p99_high_conf": 60.0,
    "upsize_cpu_p95_min":      80.0,
    "upsize_mem_p95_min":      85.0,
    "min_data_coverage":        0.80,
}
```

### Tuning thresholds at the command line

Every value above can be overridden per-run without touching the source.
Pass any of:

```text
--downsize-cpu-p95-max     (default 40.0)
--downsize-mem-p95-max     (default 50.0)
--downsize-cpu-p99-high-conf  (default 50.0)
--downsize-mem-p99-high-conf  (default 60.0)
--upsize-cpu-p95-min       (default 80.0)
--upsize-mem-p95-min       (default 85.0)
--min-data-coverage        (default 0.80)
```

Example — be more aggressive about downsizing (treat anything below 80%
P95 CPU as a candidate) while keeping the upsize bar where it is:

```pwsh
python tools/rightsizing-peak/rightsizing_peak.py `
  --subs "<sub1>,<sub2>" `
  --days 30 `
  --out-dir ./out/peak-rightsizing `
  --downsize-cpu-p95-max 80 `
  --downsize-mem-p95-max 80 `
  --upsize-cpu-p95-min   90 `
  --upsize-mem-p95-min   90
```

The engine validates the resulting rule set on startup and refuses to run
if a downsize threshold is greater than or equal to the matching upsize
threshold (which would make verdicts ambiguous), or if any value is out of
range. You'll get a clear `Threshold validation failed:` message instead of
a silent miscalculation.

**Recommended starting points:**

| Profile | Downsize CPU P95 | Downsize Mem P95 | Upsize CPU P95 | Notes |
|---|---|---|---|---|
| **Conservative (default)** | 40 | 50 | 80 | Safe for spiky/batch workloads. Use this until you've run for several nightly cycles. |
| **Balanced** | 60 | 65 | 85 | Once Advisor-unsafe count is consistently low and reviewers are catching obvious cases. |
| **Aggressive** | 80 | 80 | 90 | Mature FinOps function with rollback plans and good observability. Expect more candidates and more reviewer load. |

Whichever profile you pick, **lock it for at least one full month** before
re-tuning — you need the trend data to tell whether a change in candidate
count is the threshold or the workload.

### Adding new SKU families

The `DOWNSIZE_LADDER` map (also in the source file) is the *only* place an
explicit target SKU is proposed. If the current size has no entry there, the
engine still emits `DOWNSIZE_CANDIDATE` — but leaves `target_sku` blank and
defers the choice to the human reviewer. Adding ladder entries is the
recommended way to expand the engine's coverage; heuristic-derived targets
are deliberately not generated.

## Why peak-aware matters — and where Advisor falls short

Advisor's resize algorithm already uses percentiles, **not** averages. The
documented thresholds (per [Microsoft Learn][advisor-resize]) are:

| Workload class | CPU & Outbound Network | Memory |
|---|---|---|
| User-facing | **P95 ≤ 40 %** on the new SKU | **P99 ≤ 60 %** on the new SKU |
| Non-user-facing | P95 ≤ 80 % | P99 ≤ 80 % |

`rightsizing-peak` uses similar P95 / P99 thresholds. The genuine, defensible
deltas are **window length** and **bucket aggregation**:

| Dimension | Azure Advisor | `rightsizing-peak` |
|---|---|---|
| Default lookback | **7 days** (configurable 7 / 14 / 21 / 30 / 60 / 90) | **30 days** (configurable via `--days`) |
| CPU sampling | every 30 s → 1-min avg → 30-min bucket = **max of 1-min averages** | every 30 s → 1-min avg → 1-hour bucket = **true `Maximum`** |
| Memory sampling | same 30-min max-of-1-min-averages bucketing | 1-hour `Minimum` of `Available Memory Bytes` (worst-case headroom) |
| Excludes managed VMs (AKS / Databricks) | No | Yes |
| Cross-checks Advisor recs as a sanity layer | n/a | Yes — emits `advisor_unsafe = true` |

The portal *VM/VMSS right-sizing* configuration page lets operators filter
recommendations by an *average CPU utilization* threshold, but that is a
**display filter**, not the generation algorithm — a common source of the
"Advisor uses averages" misconception.

Where this matters in practice:

- **Retail batch jobs** that idle 90 % of the day and saturate at 02:00.
  A 7-day window that happens to omit two of those nights still produces a
  P95 below 40 %.
- **Month-end finance / reporting / ETL** workloads that peak once every
  28–31 days. By definition, a 7-day window will see the peak in at most
  one run out of four.
- **CI runners and weekly batch jobs** whose peak weekday isn't always in
  the 7-day window.

The 30-day default catches these. The per-hour true-peak aggregation
catches sub-30-minute spikes that Advisor's 30-min max-of-averages
smooths over.

The engine's headline number — *Advisor recs that would have been unsafe* —
is therefore the metric that matters most when introducing this tool to a
new team. Expect 1–5 % of Advisor's downsize recommendations to be unsafe
on a longer window in any given tenant; the cost of one unsafe change
typically dwarfs years of savings from the safe ones.

## Limitations & assumptions

- **Memory %** is computed as `(1 − AvailableMin / TotalCapacity)`. For VMs
  without the Diagnostic Extension installed, `Available Memory Bytes` is
  not emitted by the platform; those VMs land in `INSUFFICIENT_DATA`.
- **Disk and network metrics are pulled but not currently part of the
  decision.** They are written to the CSV for human review of borderline
  cases.
- **The engine does not change anything.** It is read-only. Acting on the
  output is a human decision.

## Tooling provenance

- Resource Graph via `az graph query` for VM enumeration.
- Azure Monitor via `az monitor metrics list` (PT1H grain).
- Advisor via `az advisor recommendation list --category Cost`.
- All quoting is via `subprocess` arg lists — no `shell=True`, so Windows
  cmd.exe quoting bugs are sidestepped at the source.
