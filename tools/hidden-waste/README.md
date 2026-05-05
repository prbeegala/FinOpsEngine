# hidden-waste

Detects the seven recurring **hidden-waste** classes that Azure Advisor
under-reports or misses, prices them with actual Cost Management spend, and
emits an *audit-mode* Azure Policy starter pack so platform teams can codify
the guardrails.

## What it does

1. Runs ten **Resource Graph** queries in a single pass across the supplied
   subscriptions:
   - Unattached managed disks (`diskState =~ 'Unattached'` AND no `managedBy`).
   - Unused public IPs (no `ipConfiguration` AND no `natGateway`).
   - Orphan NICs (no `virtualMachine` AND no `privateEndpoint`).
   - Stopped-not-deallocated VMs (`powerState/stopped` — distinct from
     `deallocated`; still billed for compute).
   - Old snapshots (`timeCreated > 90 days` ago).
   - Empty App Service Plans (`numberOfSites == 0`).
   - Idle Standard load balancers (no rules AND no backend pools).
   - Hot-tier storage accounts (`accessTier =~ 'Hot'`, GPv2 / blob kinds,
     creation > 30 days). Refined by Azure Monitor metrics —
     `Transactions` (Total, 30d) < 30,000 AND `UsedCapacity` (Average, 30d)
     >= 100 GiB. If metrics are unavailable the candidate is kept and
     tagged `metrics unavailable; verify manually`.
   - Untouched blob containers (`lastModifiedTime` >= 90 days). Hygiene
     finding only — Cost Management has no per-container dimension.
   - Oversized premium file shares (parent `kind == 'FileStorage'`,
     `shareQuota >= 1024 GiB`). Refined by `FileCapacity` per-share
     (Average, 30d) — flagged when used <= 50% of quota.
2. Pulls 30 days of **actual** £ from the Cost Management `/query` REST API
   (via `az rest`, body sent as a temp file — Windows-quoting-safe). Per-sub
   paging short-circuits as soon as every flagged resource is priced; capped
   at 5 pages × 5 000 rows for safety.
3. Falls back to **list-price estimates** when Cost Management has no row for
   a flagged resource (e.g. never-attached ASR replica disks, day-old
   snapshots). Disk pricing is hand-coded for P/E tiers in West Europe GBP;
   Standard public IPs at £3 / mo; idle Standard LBs at £15 / mo; premium
   files at £0.16 / GiB-month applied to the *recoverable* slice
   (`shareQuota - actual_used`).
4. Writes per-category **audit-mode Azure Policy** JSON into `policy/` so
   platform teams can promote the top-3 by £ to deny-mode after a 30-day
   audit cycle.

## Inputs

Pick a scope. The engine requires **either** `--subs` (explicit list) **or**
`--all-subs` (tenant-wide), not both.

```pwsh
# Explicit subscription list
python hidden_waste.py `
  --subs "<id1>,<id2>,..." `
  --out-dir ./out/hidden-waste

# Every enabled subscription in the current tenant
python hidden_waste.py `
  --all-subs `
  --out-dir ./out/hidden-waste

# Tenant-wide, but skip sandboxes
python hidden_waste.py `
  --all-subs `
  --exclude-subs "sandbox-1,sandbox-2" `
  --out-dir ./out/hidden-waste
```

`az login` required. Tool retries 429 / 503 with exponential backoff. Expect
~3–5 min per 20 subs, dominated by Cost Management throttling.

### Scope flags

| Flag | Purpose |
|---|---|
| `--subs <a,b,c>` | Run against this exact list. Accepts IDs or display names. |
| `--all-subs` | Enumerate `az account list` and run against every **Enabled** subscription. |
| `--exclude-subs <a,b>` | When using `--all-subs`, skip these IDs/names. |
| `--tenant <guid>` | Limit `--all-subs` to a single tenant. |
| `--include-disabled` | Include subs whose state is not Enabled. |

## Outputs

- `hidden-waste-<date>.md` — headline, by-category roll-up, top-25
  individual offenders, recommended Policy guardrails.
- `hidden-waste-<date>.csv` — full row dump for sheet pivots.
- `policy/<category>.audit.json` — one audit-mode policy per category, with
  inline `_note` annotations on the two with caveats:
  - `stopped_not_deallocated._note` — `powerState` isn't directly auditable
    via Policy. Treat as a Workbook query + Function App that re-tags VMs.
  - `old_snapshots._note` — `timeCreated` isn't filterable in Policy. Use the
    blanket snapshot rule plus a Workbook KQL filter on `properties.timeCreated`.

## Limitations & assumptions

- **Cost attribution is 30-day actuals**, monthlied. A finding flagged 28 days
  after creation will under-state monthly burn; one flagged on day 1 will
  over-state.
- **Idle-LB scope is Standard SKU only.** Basic Load Balancer is being
  retired; we deliberately ignore it rather than recommend deletion of an
  already-deprecated SKU.
- **Old-snapshots threshold is 90 days, hard-coded.** Your data-protection
  team should ratify the value before promoting the policy to deny.
- **Orphan NICs almost never have a £ cost** — they're flagged for hygiene
  (privileged-NIC sprawl is an exfil risk) not for direct savings.
- **Storage cold-tier signal is metric-based.** Without Azure Monitor
  permissions the engine keeps Hot-tier candidates with an explicit
  `metrics unavailable` tag; the operator must verify before action.
  `Transactions` and `UsedCapacity` thresholds are conservative
  (1,000 tx / day average, ≥ 100 GiB stored).
- **Untouched blob containers carry no £ attribution.** Cost Management
  doesn't break out cost by container — the parent storage account is the
  smallest billable resource ID. Treat as a hygiene signal.
- **Premium files £ savings are an estimate.** £0.16 / GiB-month is West
  Europe LRS list price; multi-region or zone-redundant shares bill more.
  The estimate uses the *recoverable* slice (`quota - actual_used`); when
  `FileCapacity` metric is unavailable we fall back to the full
  provisioned-line ceiling and tag the row clearly.
- **No deletes performed.** Engine is read-only. Remediation is a separate
  decision owned by domain teams, gated by the audit-first / 30-day pattern.

## Workbook

`workbook-hidden-waste.json` is an Azure Workbook that reads the engine's CSV
once ingested as a `HiddenWaste_CL` Log Analytics custom table. Filters by
category and by subscription; live cumulative recoverable-£ tile.

## Audit-mode policy pack

The `policy/` directory contains one Azure Policy definition per waste
category. They are all in **audit mode** — they emit non-compliant events
to Activity Log and Defender for Cloud, but do not block creation. The
recommended adoption pattern is:

1. Run the engine.
2. Pick the top 3 categories by £.
3. Assign the corresponding policies (still audit) to a pilot management
   group for 30 days.
4. Review compliance state — false-positive rate should approach zero.
5. Promote to `deny` mode (change `effect` from `audit` to `deny` in the
   `parameters.allowedValues` block of the assignment).

The two categories with caveats (`stopped_not_deallocated`, `old_snapshots`)
ship with `_note` fields explaining why the Policy alone isn't sufficient
and what to pair it with.

## Tooling provenance

- Resource Graph via `az graph query` with `$skipToken` paging (default page
  size 1 000; capped at 100 pages — none of the seven categories typically
  have more than ~5 000 rows in even very large tenants).
- Cost Management `/query` REST API (api-version `2023-11-01`), POST body
  via temp-file to dodge cmd.exe quoting bugs.
- Azure Policy templates target the GA `Microsoft.Authorization/policy`
  resource provider; tested via `az policy definition create --rules @file`
  on a sandbox tenant before being committed here.
