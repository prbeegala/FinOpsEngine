# FAQ

### Why Python and not PowerShell / Go / Rust?

Python's standard library has the right level of abstraction for ETL-shaped
code (CSV, JSON, subprocess, dataclasses), and Azure's official samples are
overwhelmingly Python. The engines have **zero third-party Python
dependencies** to keep the bar to first-run as low as possible — only the
Azure CLI itself.

### Can I run this against AWS / GCP / on-prem?

Not out of the box. The data shape (PAYG meters, family×region commitments,
tag-driven owner attribution) is portable, but every engine talks to Azure
APIs directly. Porting `hidden-waste` to AWS would mean replacing the seven
Resource Graph queries with seven Boto3 calls; the rest of the pipeline
(CSV → enrichment → Issues) is generic.

### Why not use the Azure SDK for Python instead of shelling to `az`?

Three reasons:

1. **No SDK install.** Stays a one-Python-file deployment.
2. **Token reuse.** `az login` is the single auth surface; SDK auth would
   need parallel `DefaultAzureCredential` configuration.
3. **Operability.** When something goes wrong, a curious user can copy the
   exact `az` command from the engine's verbose output and run it
   themselves to debug. With the SDK, that debug path is much harder.

The trade-off is process-spawn overhead per call. In practice the engines
are I/O-bound on the Azure APIs, not on the local subprocess. The longest
runs are dominated by Cost Management throttling, not `az` startup.

### Where do the £ figures come from — public list price or my actual bill?

**Mostly your actual bill.** Public retail price is only used as a thin
fallback for resources the bill has never seen.

| Engine | Source of £ | Notes |
|---|---|---|
| **`hidden-waste`** | Cost Management `ActualCost`, last 30 days, grouped by `ResourceId`. | Each row is tagged with a **cost source**: `cost_mgmt` (real billed £), `estimate` (list-price fallback for never-attached disks), `unknown` (no attribution — flagged for manual review). |
| **`ri-coverage`** | Cost Management `ActualCost`, last N months, filtered to `MeterCategory = Virtual Machines` and `PricingModel = OnDemand`. | RI / Savings Plan discount %s come from public Microsoft commitment tables (1Y/3Y × RI/SP) and are applied on top of your real PAYG run-rate. Microsoft does not expose your *negotiated* RI/SP rate via API, so this is unavoidable. |
| **`rightsizing-peak`** | Does not compute £ savings itself. | Produces verdicts (DOWNSIZE / KEEP / UPSIZE / INSUFFICIENT_DATA). Where £ savings appear in the diff against Advisor, the figure is **Advisor's own modeled annual savings** (Advisor uses retail list price). Pair with `hidden-waste` via `context-enricher` for a verdict + actual-bill view per VM. |

In practice this means EA / MCA discounts, regional pricing, Hybrid Benefit,
dev/test rates and any private negotiated pricing are **already baked in**
to the headline numbers. The only place retail list price leaks in is the
disk-tier fallback table in `hidden-waste` for never-attached disks (which
genuinely never produced a billing record), and Advisor's annual-savings
column on the rightsizing diff.

The engines do **not** currently call the Azure Retail Prices API. Adding
it as a sanity check (e.g. flag rows where `cost_mgmt` ≫ retail to detect
mis-tagged charges) is on the [roadmap](../ROADMAP.md).

### Why £? Can I make it $?

By default, every engine calls `az billing account list` once at startup
and renders amounts in the tenant's billing currency (USD, EUR, SEK,
etc.). The numeric values themselves come straight from Azure Cost
Management — only the glyph is local. If detection fails (no Billing
Reader, offline run, multi-currency setup), the engine falls back to
`£` and prints a one-line provenance message so you can spot it.

To force a specific glyph (e.g. when you want consistent reports
across multiple tenants, or you're billed in a currency Azure doesn't
expose to the CLI):

```pwsh
python tools\hidden-waste\hidden_waste.py    --currency-symbol '$' ...
python tools\ri-coverage\ri_coverage.py      --currency-symbol '€' ...
python tools\context-enricher\context_enricher.py --currency-symbol 'kr' ...
```

The flag accepts any string, so multi-character glyphs like `kr`,
`A$`, `NT$` work too. `rightsizing-peak` doesn't print money (it
emits verdicts) so it doesn't take the flag.

> **Note**: the engines do not perform FX conversion. Cost Management
> already returns amounts in the tenant billing currency; we only
> change the display glyph. If your tenant is genuinely multi-currency,
> talk to your Microsoft account team about consolidating onto a
> single MCA billing account first.

### Will the engines delete anything?

**No.** Every engine is read-only. The Azure Policy templates ship in
**audit mode**. The GitHub Issues open `accept` / `defer` / `reject`
discussions; they do not auto-resolve.

The closest the toolkit gets to mutation is the optional `gh issue create`
step in the nightly workflow. Even that is reversible (delete the issue;
the engine recreates it on the next nightly run).

### Why GitHub Issues? My team uses Jira / ServiceNow / Azure Boards.

The default is Issues because it's the lowest-friction option for an
engineering team that already has a GitHub repo. The `context-enricher`
writes per-owner Markdown bodies under `issues/`; swap the `gh issue
create` step in `automation/finops-nightly.yml` for the equivalent
`jira issue create`, `az boards work-item create`, etc.

### Can I run only one of the four engines?

Yes. Each engine is independent. Common subsets:

- **Just `rightsizing-peak`** — first-time customer, validate against
  Advisor before adopting more.
- **`rightsizing-peak` + `hidden-waste`** — pre-FinOps-team customer,
  build the savings number before the FinOps function exists.
- **All four + nightly automation** — mature customer with a tagging
  taxonomy and a domain-owner CODEOWNERS file.

### How does this compare to Microsoft FinOps Toolkit / Cost Management
   Power BI templates / etc.?

It is intentionally complementary. The Microsoft FinOps Toolkit is a
*data layer* (Cost Management exports, Power BI templates, KQL queries).
The FinOps Engine is a *decision layer* on top of that data — it produces
specific, ordered, attributable recommendations. You can run both; the
engines' CSV output is easy to ingest into the toolkit's Power BI models.

### How does this compare to Azure Advisor?

Advisor is a great starting point. It misses:

- **Longer-window, per-hour-true-peak rightsizing.** Advisor already
  uses P95 / P99 (not averages — a common misconception driven by the
  portal's *average CPU utilization* display filter), but on a 7-day
  default window with 30-min buckets taken as the max of 1-minute
  averages. `rightsizing-peak` uses 30 days and per-hour `Maximum` CPU
  / `Minimum` `Available Memory Bytes` buckets, which catches weekly,
  month-end, and sub-30-minute peaks the 7-day / 30-min view smooths
  over. See
  [`tools/rightsizing-peak/README.md`](../tools/rightsizing-peak/README.md#why-peak-aware-matters--and-where-advisor-falls-short).
- **Old snapshots, empty App Service Plans, idle Standard LBs,
  stopped-not-deallocated VMs** as priced findings.
- **A coverage-gap-with-buffer-guardrail** approach to RI/SP commits.
- **Per-owner remediation routing** with tag context.

Use them together: Advisor for the broad cleanup, this engine for the
sharper edges Advisor doesn't cut.

### Does this support multi-tenant / MSP scenarios?

Out of the box, no — `az login` authenticates against a single tenant per
session. For MSP usage, run the engine once per tenant (separate `az
login`) and emit the outputs to a tenant-prefixed directory. The
`context-enricher` accepts inputs from any directory, so a final
"all-tenants enriched view" is straightforward to assemble.

### What happens to my data?

It stays in your environment. The engines do **not** call any external
service. The only outbound network traffic is to `*.azure.com`,
`*.microsoft.com`, and (if you run the GitHub Actions workflow)
`api.github.com`. There is no telemetry beacon, no SaaS endpoint, no
licence server.
