# FinOps Engine — Roadmap

This is the public backlog for the FinOps Engine. It captures the gaps and
extensions we know about today, grouped by theme. **Nothing here is
committed to a release** — items move into a milestone only when scoped and
prioritised.

If you'd like an item bumped, are willing to pick one up, or think we're
missing something obvious, please open an Issue (or a PR — see
[CONTRIBUTING.md](./CONTRIBUTING.md) once it lands).

The four engines themselves are **deliberately small and stdlib-only**.
Most of the items below should preserve that property: zero third-party
Python deps and Azure CLI as the only runtime requirement. PRs that pull
in a heavyweight SDK will be asked to justify the dependency.

---

## Legend

| Badge | Meaning |
|---|---|
| 🟢 | Quick win — small, high-leverage, low risk |
| 🟡 | Medium — non-trivial change but well-scoped |
| 🔴 | Large — needs design discussion before code |
| 🛡 | Trust / safety — should land before scaling adoption |

---

## Coverage gaps (new detectors)

These are resource types or waste patterns the current engines don't see.
Most are direct extensions of `hidden-waste` or `rightsizing-peak`.

- 🟢 **Storage account waste** — cold blobs sitting in Hot tier, untouched
  containers, oversized premium files. Often the #2 line item after
  compute.
- 🟡 **Log Analytics + App Insights waste** — over-retained tables,
  expensive custom-log ingestion (£2/GB), Sentinel double-charging.
  Routinely 10–25% of an Azure bill, completely invisible to Advisor.
- 🟡 **AKS node-pool peak rightsizing** — node pools sized to peak pod
  requests rather than P95 actuals; under-utilised system pools.
  `rightsizing-peak` only handles VMs/VMSS today.
- 🟢 **App Service / Container Apps rightsizing** — P1v3 plans at <5% CPU,
  Container Apps with `min-replicas > 0` and no traffic. Today only the
  *empty* App Service Plan case is detected.
- 🟢 **Dev/test auto-shutdown gap** — non-prod-tagged VMs / SQL / AKS
  running 24×7. One of the highest-confidence quick wins in any tenant.
- 🟡 **Cosmos DB autoscale waste** — provisioned RU/s at <10% utilisation,
  dedicated-vs-shared throughput candidates.
- 🟡 **Network waste** — idle NAT Gateways (~£200/mo each), unused
  ExpressRoute circuits, unused VPN gateways.
- 🔴 **Bandwidth / egress analyser** — cross-region traffic, public-IP
  egress, VNet peering charges. Currently invisible.

## Engine improvements

Refinements to the existing four engines.

- 🟢 **rightsizing-peak: upsize + SKU-family swap** — today only emits
  *downsize* candidates. Should also emit upsize candidates and SKU-family
  swaps (Dv3 → Dasv5 typically saves 10–20% at the same performance,
  B-series for low-duty-cycle).
- 🟡 **hidden-waste: persistent state.db** — a SQLite store for first-seen
  / dedupe / staleness across nightly runs. Today every run is a new
  snapshot; trend ("is this getting better?") needs external glue.
- 🟡 **ri-coverage: expiry calendar + scope-opt + exchange maths** —
  reservations lapsing in the next 90/180 days; subscription-scope RIs
  that could go shared; 1-year vs 3-year term-exchange comparisons.
- 🟡 **RI vs Savings Plan trade-off calculator** — today both treated as
  one bucket. Customers want the trade-off priced explicitly.
- 🟢 **context-enricher: tag + YAML routing fallback** — today owners are
  resolved from `CODEOWNERS`. Add support for `owner=` / `costcenter=`
  Azure Tags and a YAML override file for orgs without `CODEOWNERS`.

## Trust & safety 🛡

These should land before the engine is rolled out widely.

- 🛡 🟢 **Unit tests against fixture CSV/JSON** — pure-stdlib deterministic
  outputs are easy to test. Land `pytest` fixtures per engine.
- 🛡 🟢 **CI on PRs** — `python -m py_compile` plus the test suite once it
  exists.
- 🛡 🟡 **Output-schema versioning** — version the CSV/MD output formats so
  Workbooks and downstream Issue templates don't break silently when a
  column is added or renamed.
- 🛡 🟢 **context-enricher `--plan-only` dry-run** — write planned Issue
  bodies to disk for review *before* opening real GitHub Issues. The
  current path will happily open hundreds of issues in one run.

## Productisation

Lower the friction of running the engine for a new team.

- 🟢 **Dockerfile + ghcr.io publish** — a `ghcr.io/prbeegala/finops-engine`
  image removes Python/`az` install friction.
- 🟡 **Single `finops` CLI entrypoint** — `finops run rightsizing-peak …`,
  `finops run all …` instead of four separate invocations. A thin wrapper
  over the existing modules.
- 🟡 **`finops.yaml` config file** — replace long PowerShell argument lists
  with a config file (subscriptions, refund buffer, regions, tag
  conventions, threshold overrides).
- 🟢 **HTML report sink** alongside the existing Markdown — most execs
  won't open a `.md` file.
- 🔴 **Cost Management export ingest mode** — use the daily Cost
  Management exports already enabled in most tenants, instead of
  on-demand `az` calls. Faster, deterministic, cheaper.

## Integrations

Get the findings into the systems FinOps and engineering teams already
live in.

- 🟡 **ServiceNow / Jira issue sinks** — alongside the existing GitHub
  Issues sink in `context-enricher`.
- 🟢 **Slack / Teams weekly digest webhook** — top 10 findings + a
  week-over-week delta posted to a channel.
- 🟡 **Power BI dataset export** — Workbooks for engineers; Power BI for
  finance.
- 🔴 **Azure Policy auto-PR mode** — opt-in mode that PRs the starter
  policy pack into a customer IaC repo to close the loop on prevention.

## Multi-cloud / scope expansion

The analytical patterns (peak vs average, orphan detection, commitment
fit) translate; the data sources don't.

- 🔴 **AWS adapter** — Compute Optimizer + Cost Explorer feed
  `rightsizing-peak` and `ri-coverage` via a thin adapter.
- 🔴 **GCP adapter** — Recommender API + Billing export.
- 🟡 **M365 / Power Platform licensing waste** — adjacent ask from the same
  FinOps lead in most engagements.

## Operational / repo hygiene

- 🟢 **`make` / `Invoke-Build` target** for `lint + compile + test +
  sample-run` so a new contributor runs one command.
- 🟢 **CONTRIBUTING.md + CODE_OF_CONDUCT.md** — standard public-repo
  hygiene.
- 🟢 **Issue / PR templates** — "new waste detector" and "new report sink"
  are the two repeating PR shapes.

---

## Recently shipped

- `v0.1.0` — initial public release: four engines, nightly automation
  workflow, Azure Monitor workbooks, starter Azure Policy pack, full
  docs, synthetic samples.
- Customisable downsize / upsize / coverage thresholds via CLI flags
  on `rightsizing-peak`, with startup validation.

## Out of scope (deliberately)

- **Auto-remediation.** The engine is read-only by design; every
  remediation is a human decision. We will not add a "delete the orphan"
  flag.
- **Saving-percentage pricing models.** The engine is open source and
  free. We will not gate findings behind a SaaS tier.
- **A web UI.** Markdown + CSV + Workbooks cover the audiences we have.
  PRs to add a web app will be politely declined; an HTML report sink
  (above) is the supported alternative.
