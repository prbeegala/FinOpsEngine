# FinOps Engine

> **An open, opinionated, four-engine FinOps toolkit for Azure.**
> Replaces Advisor's average-based recommendations with peak-aware decisions,
> finds the hidden waste Advisor doesn't price, sizes Reservations / Savings
> Plans against a configurable cancellation-exposure buffer, and turns every
> finding into an approve-ready, per-owner remediation queue — all from
> `az login`, no agents, no SaaS.

---

## Why this exists

Most FinOps tools fall into one of two camps:

1. **Reactive dashboards** that re-state what Azure already shows you (Cost
   Management, Advisor) without changing the decision quality.
2. **Black-box SaaS platforms** that ingest your billing data, charge a
   percentage of savings, and still hand-off to your engineers to actually
   act on the findings.

The FinOps Engine is neither. It is four small, deterministic Python engines
that reproduce — and significantly improve on — the analysis a senior FinOps
practitioner would do by hand:

| Engine | Replaces / improves on | Headline output |
|---|---|---|
| **`rightsizing-peak`** | Azure Advisor's *average*-based VM rightsizing | A list of Advisor recommendations that would have been **unsafe** under P95/P99 peak data |
| **`hidden-waste`** | Manual orphan / lifecycle hunts in Cost Management | Seven categories of waste, **priced** against actual £, plus a starter Azure Policy pack |
| **`ri-coverage`** | Portal *Reservations → Recommendations* (single-SKU view) | A risk-scored shortlist that fits inside **your cancellation-exposure buffer** |
| **`context-enricher`** | Spreadsheet round-trips between FinOps and domain teams | Per-owner GitHub Issue bodies — auto-routed via `CODEOWNERS` |

Every engine is **read-only**. None of them delete or modify resources. They
emit Markdown, CSV, and (where appropriate) Azure Policy / Azure Workbook JSON.
Remediation is always a human decision.

## How it landed at one customer

The first deployment of these engines (a UK retailer, ~20 production
subscriptions) produced, in a single afternoon:

- **1 of Advisor's downsize recommendations flagged as unsafe** — would have
  caused a peak-hour outage on a batch workload Advisor had averaged-over.
- **£9.7k / month (£117k / year) of recoverable hidden waste** that Advisor
  did not price (mostly old snapshots, empty App Service Plans, and one
  £2.7k/month unattached ASR seed disk no human had spotted).
- **A risk-scored RI / Savings-Plan shortlist** that fit within a £5k
  cancellation-exposure buffer — and quantified the £1.5M of savings that
  *would* be unlocked by raising the buffer (the binding constraint was
  procurement, not the data).
- **One GitHub Issue per domain owner** the next morning, with `accept` /
  `defer` / `reject` checkboxes — replacing a recurring 90-minute FinOps
  weekly walk-through of a spreadsheet.

You should expect different absolute numbers; the *shape* of the findings is
remarkably consistent across tenants.

---

## Quick start

```pwsh
git clone https://github.com/prbeegala/FinOpsEngine.git
cd FinOpsEngine

# 1. Authenticate to Azure (Reader + Cost Management Reader is enough)
az login
az account set --subscription <default-sub-id>

# 2. Run any engine standalone
python tools/rightsizing-peak/rightsizing_peak.py `
  --subs "<sub1>,<sub2>,<sub3>" `
  --days 30 `
  --out-dir ./out/peak-rightsizing

python tools/hidden-waste/hidden_waste.py `
  --subs "<sub1>,<sub2>,<sub3>" `
  --out-dir ./out/hidden-waste

python tools/ri-coverage/ri_coverage.py `
  --subs "<sub1>,<sub2>,<sub3>" `
  --months 3 `
  --refund-buffer-gbp 5000 `
  --out-dir ./out/ri-coverage

# 3. Join everything into per-owner remediation queues
python tools/context-enricher/context_enricher.py `
  --hidden-waste-csv ./out/hidden-waste/hidden-waste-<date>.csv `
  --rightsizing-csv  ./out/peak-rightsizing/tenant-peak-rightsizing-savings-<date>.csv `
  --out-dir ./out/enriched
```

Linux / macOS users: substitute `\` for the PowerShell line-continuation
backtick. The Python is portable; only the shell quoting differs.

## Prerequisites

- **Python 3.10+** (standard library only — no `pip install` step needed).
- **Azure CLI 2.55+** (`az --version` to check).
- **Permissions on the in-scope subscriptions:**
  - `Reader` (Resource Graph, Monitor metrics, Advisor)
  - `Cost Management Reader` (`/query` REST API)
  - *Optional:* `Microsoft.Capacity/reservationOrders/read` to subtract
    existing RI cover from the coverage gap (the engine works without it; the
    gap is then relative to *measured PAYG* rather than *PAYG net of RI*).

The engines never write to Azure. They issue `GET` / `POST /query` calls only.

---

## Architecture at a glance

```
                         ┌───────────────────────────────────┐
                         │  Azure (your tenant)              │
                         │  • Resource Graph                 │
                         │  • Azure Monitor metrics          │
                         │  • Cost Management /query         │
                         │  • Advisor                        │
                         └───────────────┬───────────────────┘
                                         │ az CLI (read-only)
            ┌────────────────────────────┼────────────────────────────┐
            ▼                            ▼                            ▼
   ┌──────────────────┐         ┌──────────────────┐         ┌──────────────────┐
   │ rightsizing-peak │         │ hidden-waste     │         │ ri-coverage      │
   │  P95/P99 vs      │         │  7 waste classes │         │  Family×region   │
   │  Advisor diff    │         │  + Policy pack   │         │  shortlist + buf │
   └────────┬─────────┘         └────────┬─────────┘         └────────┬─────────┘
            │ CSV + MD                   │ CSV + MD                   │ CSV + MD
            └─────────────┬──────────────┴──────────────┬─────────────┘
                          ▼                             ▼
                ┌──────────────────────────────────────────┐
                │ context-enricher                         │
                │  • joins findings ↔ tags via Resource     │
                │    Graph                                 │
                │  • HIGH / MED / LOW confidence scoring    │
                │  • per-owner GitHub Issue bodies          │
                └────────────────────┬─────────────────────┘
                                     │
                                     ▼
                        ┌──────────────────────────┐
                        │ automation/finops-       │
                        │ nightly.yml (GitHub      │
                        │ Actions: 05:00 UTC)      │
                        │  → opens / updates       │
                        │    per-owner Issues      │
                        └──────────────────────────┘
```

## Repository layout

```
FinOpsEngine/
├── tools/
│   ├── rightsizing-peak/   Peak-aware VM rightsizing engine + Workbook
│   ├── hidden-waste/       Orphan & lifecycle waste finder + Policy pack
│   ├── ri-coverage/        Reservation / Savings Plan shortlist
│   └── context-enricher/   Tag join + per-owner Issue generator
├── automation/
│   └── finops-nightly.yml  GitHub Actions: nightly refresh + Issues
├── samples/                Synthetic example outputs (runnable without Azure)
│   ├── peak-rightsizing/
│   ├── hidden-waste/
│   ├── ri-coverage/
│   └── enriched/
├── docs/                   Methodology, FAQ, troubleshooting
├── LICENSE
├── requirements.txt
└── README.md               (this file)
```

---

## Sample outputs

> The samples in `samples/` are **synthetic** — generated to illustrate the
> output shape. None of the resource IDs, subscriptions, or numbers represent
> a real environment.

### 1. Peak-aware rightsizing — combined report ([full file](samples/peak-rightsizing/peak-rightsizing-combined-20260101.md))

```markdown
# Peak-Aware Rightsizing — Combined Pilot Report

| Subscription   | VMs | Downsize | Keep | Upsize warn | Insufficient | Advisor unsafe |
|----------------|----:|---------:|-----:|------------:|-------------:|---------------:|
| ContosoApp.Prod|   7 |        1 |    6 |           0 |            0 |              0 |
| ContosoBatch   |  22 |        7 |    5 |           9 |            1 |              1 |
| **Total**      |**29**|     **8**|**11**|       **9** |        **1** |          **1** |

## The headline number

**1** of Azure Advisor's downsize recommendations across the pilot trio
would have been **unsafe** according to peak (P95/P99) workload data — the
manual-validation overhead this engine eliminates.
```

### 2. Hidden waste — by category ([full file](samples/hidden-waste/hidden-waste-20260101.md))

```markdown
# Hidden Waste & Lifecycle

- Subscriptions scanned: 20
- Findings: **1,135**
- Estimated monthly £ recoverable: **£9,744** (~£116,923 / yr)

| Category                       | Count | Monthly £ | Annualised £ |
|--------------------------------|------:|----------:|-------------:|
| Unattached managed disks       |   592 |    £4,845 |      £58,143 |
| Old snapshots (>90d)           |    54 |    £3,053 |      £36,635 |
| Empty App Service Plans        |    20 |    £1,717 |      £20,607 |
| Unused public IPs              |    46 |       £91 |       £1,098 |
| Idle Standard load balancers   |     2 |       £30 |         £360 |
| Stopped-not-deallocated VMs    |     1 |     £6.67 |          £80 |
| Orphan NICs                    |   420 |     £0.00 |        £0.00 |
```

### 3. RI / SP shortlist ([full file](samples/ri-coverage/ri-shortlist-20260101.md))

```markdown
# RI / Savings-Plan Risk-Scored Shortlist

Buffer: **£5,000** cancellation exposure (configurable).

| # | Family×Region    | Product       | Annual £ committed | Annual savings | Exposure |
|--:|------------------|---------------|-------------------:|---------------:|---------:|
| 1 | Dsv5  / uksouth  | Compute SP 1Y |             £18,200|         £3,094 |   £2,184 |
| 2 | Esv5  / uksouth  | RI 1Y         |             £14,800|         £4,440 |   £1,776 |
| 3 | Bs    / uksouth  | RI 1Y         |              £6,400|         £1,920 |     £768 |
| 4 | Fsv2  / westeu   | Compute SP 1Y |              £2,200|           £374 |     £264 |
| | **Buffer used**  |               |                    |  **£9,828**    | **£4,992** |
```

### 4. Per-owner remediation queue ([full file](samples/enriched/contoso-app-team-20260101.md))

```markdown
# FinOps remediation queue — contoso-app-team — 2026-01-01

**14 findings · ~£1,230 / month (£14,760 / yr) recoverable.**

| Resource              | Category                       | Env  | £/mo |
|-----------------------|--------------------------------|------|-----:|
| pip-fe-prod-uks-08    | Unused public IPs              | Prod |    £3|
| disk-bkp-2024-q4      | Old snapshots (>90d)           | Prod |  £312|
| asp-legacy-portal-001 | Empty App Service Plans        | Prod |  £180|
| vm-batch-night-04     | Rightsize Standard_E16ds_v5 →  | Prod |  £420|
|                       | Standard_E8ds_v5               |      |      |
| ...                   | ...                            | ...  |  ... |

> Reply `accept`, `defer`, or `reject` per row.
> Generated by the FinOps Engine. Each row links back to the source engine.
```

---

## Engine deep-dives

Each engine has its own README with full documentation:

- [`tools/rightsizing-peak/README.md`](tools/rightsizing-peak/README.md) —
  P95/P99 decision tree, Advisor diff, downsize ladders.
- [`tools/hidden-waste/README.md`](tools/hidden-waste/README.md) — the seven
  waste classes, pricing fallbacks, Policy pack.
- [`tools/ri-coverage/README.md`](tools/ri-coverage/README.md) — risk model,
  buffer guardrail, and limitations.
- [`tools/context-enricher/README.md`](tools/context-enricher/README.md) —
  tag conventions, confidence scoring, GitHub Issue routing.
- [`automation/README.md`](automation/README.md) — GitHub Actions deployment
  guide.

## Methodology and design notes

For the *why* behind the engines (rather than the *how*), see:

- [`docs/methodology.md`](docs/methodology.md) — peak-vs-average,
  the £-buffer mental model, deterministic-first / LLM-later.
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — common
  permission errors, throttle behaviour, Windows quoting.
- [`docs/faq.md`](docs/faq.md) — frequently-asked questions.

---

## Currency

All `£` symbols in the code and reports are **labels only** — the underlying
numbers come from your Azure billing currency. If your tenant bills in USD /
EUR / etc., the values are still your billed numbers; they are merely
*displayed* with a £ glyph. To change the display, search-replace `£` in the
four engine source files (the engines treat it as a constant string).

A future release will read `az billing account list` to autodetect.

## Extending

The engines are intentionally small and dependency-free. Common extensions:

- **Different cloud / billing system** — replace the Cost Management /query
  call with whichever billing API your platform exposes; the rest of the
  pipeline (CSV → enrichment → Issues) is generic.
- **Different ticketing system** — `context-enricher` writes Markdown bodies
  per owner under `issues/`. Swap the `gh issue create` step in
  `finops-nightly.yml` for `jira`, `azure boards`, ServiceNow, etc.
- **Different tag taxonomy** — edit the `*_KEYS` tuples at the top of
  `context_enricher.py`. Case-insensitive, first match wins.
- **LLM-ranked rationale** — the README of `context-enricher` walks through
  layering a small model on top of the deterministic rationale once
  HIGH/MED accept rates are trusted.

## Contributing

Issues and PRs welcome. The engines deliberately avoid third-party Python
dependencies (the standard library plus the Azure CLI is the contract); please
keep that property if you submit a PR.

## License

[MIT](LICENSE).
