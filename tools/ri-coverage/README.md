# ri-coverage

Builds a **coverage gap map** plus a **risk-scored Reservation / Savings-Plan
shortlist** bounded by your configured cancellation-exposure buffer.

## What it does

1. Pulls last N months of *PAYG (OnDemand)* virtual-machine consumption from
   the **Cost Management `/query` REST API** (via `az rest`, body sent as a
   temp file to dodge the cmd.exe quoting trap).
2. Aggregates by `(MeterSubCategory, ResourceLocation)` — i.e. the natural
   commitment unit for an RI or Compute Savings Plan.
3. Computes month-over-month coefficient of variation per family × region
   group.
4. Picks a product per group:
   - `STABLE` (CV < 15%) → VM RI 1Y, commit 80% of PAYG.
   - `VARIABLE` (CV 15–30%) → Compute SP 1Y, commit 65%.
   - `UNSTABLE` (CV ≥ 30%) → Compute SP 1Y, commit 30%.
5. Estimates annual savings using blended published rates (RI 1Y 30%,
   RI 3Y 50%, SP 1Y 17%, SP 3Y 28%).
6. Models cancellation exposure at 12% of annual commit, then **greedy-packs
   the highest-savings LOW-risk picks into the configured buffer**.

## Usage

Pick a scope. The engine requires **either** `--subs` (explicit list) **or**
`--all-subs` (tenant-wide), not both.

```pwsh
# Explicit subscription list
python ri_coverage.py `
  --subs "<id1>,<id2>,..." `
  --months 3 `
  --refund-buffer 5000 `
  --out-dir ./out/ri-coverage

# Every enabled subscription in the current tenant
python ri_coverage.py `
  --all-subs `
  --refund-buffer 5000 `
  --out-dir ./out/ri-coverage
```

`az login` required. Tool retries 429s with exponential backoff; expect
~30–90s per 20 subs.

### Scope flags

| Flag | Purpose |
|---|---|
| `--subs <a,b,c>` | Run against this exact list. Accepts IDs or display names. |
| `--all-subs` | Enumerate `az account list` and run against every **Enabled** subscription. |
| `--exclude-subs <a,b>` | When using `--all-subs`, skip these IDs/names. |
| `--tenant <guid>` | Limit `--all-subs` to a single tenant. |
| `--include-disabled` | Include subs whose state is not Enabled. |
| `--currency-symbol <glyph>` | Override the auto-detected display currency (e.g. `$`, `€`, `kr`). Defaults to whatever `az billing account list` reports for the tenant, falling back to `£`. Affects display only — the underlying numbers come straight from Cost Management. |

## Outputs

- `ri-coverage-<date>.md` — coverage gap map, top-20 commitable groups.
- `ri-coverage-<date>.csv` — same data, full rows.
- `ri-shortlist-<date>.md` — risk-scored picks within the buffer +
  rejected-by-guardrail list.

## The cancellation-exposure buffer

The most important argument is `--refund-buffer`. It is the maximum
amount of cancellation fee you are willing to incur if a commitment turns
out badly, expressed in your tenant's billing currency (auto-detected
via `az billing account list` — see `--currency-symbol` to override).
Microsoft currently charges a 12% cancellation fee on Reservations and
Savings Plans (subject to change), so:

```
Maximum committable annual spend ≈ buffer / 0.12
e.g. 5,000 buffer ≈ 41,666 of safely-cancellable annual commit.
```

This is intentionally a **business choice**, not a technical one. Setting
the buffer too low caps your savings; setting it too high turns a forecast
miss into a real refund cost. Common starting points:

- **5k** for a first-time customer with no commit history.
- **15k–25k** for a tenant with substantial VM spend and a
  capacity-planning function.
- **`--refund-buffer 0`** to suppress the buffer guardrail entirely
  (the shortlist becomes "everything stable enough to commit").

> **Deprecated**: `--refund-buffer-gbp` is still accepted as an alias
> for `--refund-buffer` for one release so existing CI workflows keep
> working; using it prints a deprecation warning. Update before v0.3.0.

## Limitations & assumptions

- **No reservation-utilisation pull**: requires
  `Microsoft.Capacity/reservationOrders/read` which the operating identity
  often doesn't have. Coverage gap is therefore relative to *measured PAYG*,
  not *PAYG net of partial existing RI cover*. To fix: request the role on
  the billing scope, then extend the engine to pull
  `/providers/Microsoft.Capacity/reservationOrders` and subtract.
- **Savings rates are blended estimates** — confirm per-SKU at commit time
  in the Azure portal *Reservations → Recommendations* view.
- **Cancellation exposure modelled at 12%**; this is Microsoft's current
  fee. The buffer is your cap, not Microsoft's policy.
- **Coupling with rightsizing**: a family×region group flagged for downsize
  by `rightsizing-peak` should *not* be reserved at its current size. The
  two tools are intentionally decoupled but their output should be reviewed
  together at FinOps weekly.

## Workbook

`workbook-ri-coverage.json` is an Azure Workbook that reads the engine's CSV
once it's ingested as a `RICoverage_CL` Log Analytics custom table. Filters
by risk band; tracks cumulative buffer exposure live.

## Tooling provenance

- Cost Management `/query` REST API (api-version `2023-11-01`).
- `az rest --body @file.json` pattern to bypass shell-quoting bugs on
  Windows.
- Throttle: serial per-sub, in-process exponential backoff for 429/503.
