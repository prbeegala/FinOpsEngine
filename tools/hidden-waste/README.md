# hidden-waste

Detects the twelve recurring **hidden-waste** classes that Azure Advisor
under-reports or misses, prices them with actual Cost Management spend, and
emits an *audit-mode* Azure Policy starter pack so platform teams can codify
the guardrails.

## What it does

1. Runs fifteen **Resource Graph** queries in a single pass across the supplied
   subscriptions:
   - Unattached managed disks (`diskState =~ 'Unattached'` AND no `managedBy`).
   - Unused public IPs (no `ipConfiguration` AND no `natGateway`).
   - Orphan NICs (no `virtualMachine` AND no `privateEndpoint`).
   - Stopped-not-deallocated VMs (`powerState/stopped` — distinct from
     `deallocated`; still billed for compute).
   - Old snapshots (`timeCreated > 90 days` ago).
   - Empty App Service Plans (`numberOfSites == 0`).
   - **Under-utilised App Service Plans** (`numberOfSites >= 1`, paid
     SKU, P95 of hourly `CpuPercentage` Maximum below
     `--asp-idle-cpu-p95-max` (default 5 %) over `--asp-idle-days`
     (default 14). Excludes `Free` / `Shared` / `Dynamic` /
     `ElasticPremium` — Functions Premium needs its own heuristic
     and is intentionally out of scope here.
   - **Idle Container Apps** (`template.scale.minReplicas >= 1`, total
     `Requests <= --ca-idle-requests-max` (default 0) AND average
     `Replicas` held within 0.1 of `minReplicas` over `--ca-idle-days`
     (default 14)).
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
   - **Dev/test VMs without auto-shutdown.** `environment` / `env` tag
     in the canonical dev/test value list (`dev`, `test`, `qa`, `uat`,
     `staging`, `preprod`, `sandbox`, `nonprod`, …), `PowerState/running`,
     and **no** `microsoft.devtestlab/schedules` `ComputeVmShutdownTask`
     (Enabled) targeting the VM. AKS-managed (`mc_*`) and Databricks
     (`databricks-rg-*`) RGs and AKS-spawned (`aks-*`) names are
     excluded. Refined by ≥ `--devtest-uptime-threshold` (default 0.95)
     hourly Percentage CPU coverage over `--devtest-uptime-days`
     (default 14) — same posture as `rightsizing-peak`. Missing metrics
     keep the candidate, tagged `uptime=unknown`.
   - **Dev/test SQL DBs not on Serverless.** `microsoft.sql/servers/databases`
     with env-tag in the dev/test list and service tier not starting with
     `GP_S_` — provisioned DTU/vCore tiers bill regardless of activity;
     Serverless General-Purpose is the only auto-pause-capable option.
   - **Dev/test AKS clusters always-on.** `microsoft.containerservice/managedclusters`
     with env-tag in the dev/test list and `properties.powerState.code =~ 'Running'`.
     Pause via `az aks stop`; control-plane and node-pool charges drop to
     near-zero while stopped.
2. Pulls 30 days of **actual** £ from the Cost Management `/query` REST API
   (via `az rest`, body sent as a temp file — Windows-quoting-safe). Per-sub
   paging short-circuits as soon as every flagged resource is priced; capped
   at 5 pages × 5 000 rows for safety. Dev/test categories scale the bill by
   `108/168 ≈ 0.6429` (the wasted slice vs a 12h × 5d target cadence).
3. Falls back to **list-price estimates** when Cost Management has no row for
   a flagged resource (e.g. never-attached ASR replica disks, day-old
   snapshots). Disk pricing is hand-coded for P/E tiers in West Europe GBP;
   Standard public IPs at £3 / mo; idle Standard LBs at £15 / mo; premium
   files at £0.16 / GiB-month applied to the *recoverable* slice
   (`shareQuota - actual_used`). Dev/test rows without a CM hit are reported
   as `unknown` rather than fabricated.
4. Writes per-category **audit-mode Azure Policy** JSON into `policy/` so
   platform teams can promote the top-3 by £ to deny-mode after a 30-day
   audit cycle. Dev/test detectors are tag/configuration-based rather than
   resource-property based, so they don't ship a Policy template — the
   top-3 selection skips them.

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
| `--asp-idle-cpu-p95-max <pct>` | App Service Plan: P95 hourly Maximum `CpuPercentage` below this threshold flags the plan as under-utilised. Default `5.0`. |
| `--asp-idle-days <n>` | App Service Plan observation window in days. Default `14`. |
| `--ca-idle-requests-max <n>` | Container Apps: total `Requests` ≤ this counts as idle. Default `0`. |
| `--ca-idle-days <n>` | Container Apps observation window in days. Default `14`. |
| `--skip-metrics` | Skip Azure Monitor calls. PaaS rightsizing candidates are dropped (they can't be qualified without metrics); storage detectors fall back to their own posture. |
| `--currency-symbol <glyph>` | Override the auto-detected display currency (e.g. `$`, `€`, `kr`). Defaults to whatever `az billing account list` reports for the tenant, falling back to `£`. Numeric values are unchanged — Cost Management already returns amounts in the tenant's billing currency. |

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
- **PaaS rightsizing requires metric access.** Both the under-utilised
  ASP and idle Container Apps detectors need `Microsoft.Insights/
  metrics/read` (built-in `Reader` is usually sufficient; custom
  Reader-like roles may not be). Candidates whose metrics fail to
  return are *dropped*, not surfaced — the ARG predicate alone
  ("ASP with apps deployed", "CA with min-replicas ≥ 1") is not
  waste-suspect on its own. `--skip-metrics` short-circuits both
  categories entirely. Each candidate triggers up to two `az monitor
  metrics list` calls, so very large tenants should set realistic
  windows (`--asp-idle-days`, `--ca-idle-days`) for run-time control.
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
  size 1 000; capped at 100 pages — none of the twelve categories typically
  have more than ~5 000 rows in even very large tenants).
- Cost Management `/query` REST API (api-version `2023-11-01`), POST body
  via temp-file to dodge cmd.exe quoting bugs.
- Azure Policy templates target the GA `Microsoft.Authorization/policy`
  resource provider; tested via `az policy definition create --rules @file`
  on a sandbox tenant before being committed here.
