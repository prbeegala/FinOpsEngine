# rightsizing-peak

Peak-aware (P95 / P99) VM rightsizing engine. A safe replacement for Azure
Advisor's average-based "Cost — Resize Virtual Machine" recommendations,
appropriate for spiky / batch / retail workloads where Advisor's averages
hide the peak that justifies the current SKU.

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

```pwsh
python rightsizing_peak.py `
  --subs "<subId1>,<subId2>,<subId3>" `
  --days 30 `
  --out-dir ./out/peak-rightsizing `
  --max-workers 8
```

`az login` required. Uses Azure CLI under the hood. Expect ~1–2 min per 100
VMs (rate-limited by Azure Monitor's `metrics list` throttle, not by this
engine).

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

The `DOWNSIZE_LADDER` map (also in the source file) is the *only* place an
explicit target SKU is proposed. If the current size has no entry there, the
engine still emits `DOWNSIZE_CANDIDATE` — but leaves `target_sku` blank and
defers the choice to the human reviewer. Adding ladder entries is the
recommended way to expand the engine's coverage; heuristic-derived targets
are deliberately not generated.

## Why peak-aware matters

Advisor's "Cost — Resize" recommendation is derived from the *average* CPU
over its observation window. For a steady-state workload that is fine. For:

- **Retail batch jobs** that idle 90% of the day and saturate at 02:00,
- **CI runners** that burst during the working day,
- **Reporting / ETL** that hit one peak per month,

…the *average* is structurally below 40% but the *peak* is at 100%. Following
Advisor's recommendation in those cases causes a missed peak — a pager event
that has nothing to do with the change reviewer realising they bought 12
months of pain to save £40 / month.

The engine's headline number — *Advisor recs that would have been unsafe* —
is therefore the metric that matters most when introducing this tool to a
new team. Expect 1–5% of Advisor's "downsize" recommendations to be unsafe
in any given tenant; the cost of one unsafe change typically dwarfs years
of savings from the safe ones.

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
