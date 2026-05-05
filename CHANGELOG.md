# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
as detailed in [`VERSIONING.md`](./VERSIONING.md).

## [Unreleased]

### Added

- **`hidden-waste`: dev/test auto-shutdown gap detector (issue
  [#5](https://github.com/prbeegala/FinOpsEngine/issues/5)).** Three
  new categories — `devtest_no_shutdown_vm`, `devtest_no_shutdown_sql`,
  and `devtest_no_shutdown_aks` — flag non-production workloads
  (`environment` / `env` tag in the canonical dev/test value list)
  that bill 24×7 against a 12h × 5d target cadence. The VM detector
  left-anti-joins against `microsoft.devtestlab/schedules`
  (`ComputeVmShutdownTask`, `Enabled`) so VMs that already have a
  shutdown schedule are excluded; it then verifies ≥95% hourly
  Percentage CPU coverage over the last 14 days via Azure Monitor,
  matching `rightsizing-peak`'s coverage methodology. SQL flags every
  Azure SQL DB whose service tier isn't Serverless (`GP_S_*`) — the
  only DTU/vCore tier that auto-pauses. AKS flags clusters in
  `PowerState/Running` that could be paused via `az aks stop`. Savings
  are computed as `monthly_bill × 108/168 ≈ 0.6429` (the wasted slice
  vs the issue's explicit 12h × 5d target). New CLI flags
  `--devtest-uptime-days` and `--devtest-uptime-threshold` tune the
  evidence window. Tag-key tuples (`OWNER_KEYS`, `CRITICALITY_KEYS`,
  `ENVIRONMENT_KEYS`, `COSTCENTRE_KEYS`, `APP_KEYS`,
  `TAG_PLACEHOLDERS`) and the new `DEVTEST_ENV_VALUES` set are now
  centralised in `tools/tag_keys.py`; `context-enricher` re-imports
  them so the two engines can never disagree on what counts as
  "dev/test".

- **`rightsizing-peak`: upsize targets and SKU-family swap suggestions
  (issue [#9](https://github.com/prbeegala/FinOpsEngine/issues/9)).**
  The engine now emits an `UPSIZE` verdict (replacing the old
  `UPSIZE_WARNING`) with a recommended target SKU drawn from a new
  `UPSIZE_LADDER` (mirror of the existing `DOWNSIZE_LADDER`). It also
  populates a new CSV column, `recommended_sku`, with modernization
  suggestions independent of the verdict — Dv3/DSv2 → Dasv5 and Ev3 →
  Easv5 (typically 10–20% cheaper at matching shape), and a B-series
  swap for `DOWNSIZE_CANDIDATE` VMs whose P95 CPU < 15% (low duty
  cycle). The `--upsize-cpu-p95-min` / `--upsize-mem-p95-min` flags
  drive the `UPSIZE` threshold (unchanged behaviour). README updated
  with the swap tables. Existing `DOWNSIZE_CANDIDATE` / `KEEP` /
  `INSUFFICIENT_DATA` verdicts are unchanged.

- **Currency auto-detect across all engines (issue [#14](https://github.com/prbeegala/FinOpsEngine/issues/14)).**
  `hidden-waste`, `ri-coverage`, and `context-enricher` now call
  `az billing account list` once at startup, read the tenant's billing
  currency, and render headlines / tables / per-owner Issue bodies in
  the matching glyph (USD `$`, EUR `€`, SEK `kr`, JPY `¥`, etc. — 18
  ISO-4217 codes mapped, unknown codes fall back to the raw ISO).
  A new `--currency-symbol` flag overrides detection (accepts any
  string, including multi-character glyphs like `kr` / `A$`). When
  detection fails (no Billing Reader, offline run, malformed
  response), engines silently fall back to the historical `£` default
  and print a one-line provenance message at startup. Numeric values
  are unchanged — Cost Management already returns amounts in the
  tenant billing currency. Shared helper lives at
  `tools/finops_currency.py` (covered by 18 unit tests).
  `rightsizing-peak` was unaffected (it doesn't print money).

### Changed

- **`ri-coverage`: `--refund-buffer-gbp` renamed to `--refund-buffer`.**
  The old GBP-suffixed flag remains as a deprecated alias for one
  release so existing automation (`automation/finops-nightly.yml`,
  custom CI) keeps working — invoking it prints a deprecation warning
  to stderr but otherwise behaves identically. Update your wrappers
  before the v0.3.0 release, when the alias will be removed.

### Added

- **`context-enricher`: `--plan-only` dry-run mode (issue [#21](https://github.com/prbeegala/FinOpsEngine/issues/21)).**
  When the flag is set, per-owner Issue bodies are written to
  `<out-dir>/issues-planned/` instead of `<out-dir>/issues/` and each
  body carries a `> ⚠️ DRY-RUN — --plan-only mode.` banner. The
  nightly workflow's `gh issue create` loop globs `issues/*.md`, so a
  plan-only run is a no-op for CI — reviewers can grep
  `issues-planned/` before flipping the flag off. The
  `finops-nightly.yml` workflow gains a `plan_only` `workflow_dispatch`
  input (default `true`) that wires through to the engine and
  short-circuits the issue-create step. Recommended posture for the
  first run on a new tenant.

- **`hidden-waste`: PaaS rightsizing — under-utilised App Service Plans
  and idle Container Apps (issue [#4](https://github.com/prbeegala/FinOpsEngine/issues/4)).**
  Adds `idle_app_service_plan` (paid SKUs only — `Free` / `Shared` /
  `Dynamic` / `ElasticPremium` excluded — refined by P95 of hourly
  `CpuPercentage` Maximum over a configurable window) and
  `idle_container_app` (`minReplicas >= 1` candidates whose total
  `Requests` and average `Replicas` indicate warm but quiet workloads).
  Both detectors **drop candidates when Azure Monitor metrics can't be
  retrieved** (a deliberately different posture from storage detectors —
  the ARG predicate alone is not waste-suspect on its own). Five new CLI
  flags: `--asp-idle-cpu-p95-max` (default 5 %), `--asp-idle-days`
  (default 14), `--ca-idle-requests-max` (default 0), `--ca-idle-days`
  (default 14), `--skip-metrics` (skip the Monitor pass entirely; PaaS
  candidates are dropped). Audit-mode Azure Policy templates ship for
  both categories; README, ROADMAP, CHANGELOG, and the by-category
  sample tables are updated. CSV schema unchanged.

- **`hidden-waste`: three new storage detectors (issue [#1](https://github.com/prbeegala/FinOpsEngine/issues/1)).**
  Adds `storage_cold_tier` (Hot-tier accounts with low transactions and
  ≥100 GiB stored, refined via Azure Monitor `Transactions` /
  `UsedCapacity` metrics), `storage_untouched_container` (blob containers
  with `lastModifiedTime` ≥ 90 days — hygiene-only, no per-container CM
  attribution), and `storage_oversize_premium` (premium file shares with
  `shareQuota` ≥ 1 TiB and ≤ 50% utilisation, refined via per-share
  `FileCapacity`). Each ships an audit-mode Azure Policy template; the
  README, ROADMAP, and the by-category sample table are updated. Cost
  source tagging follows the existing `cost_mgmt` / `estimate` /
  `unknown` contract; no CSV column changes.

- **Fixture-driven test infrastructure** under `tests/`:
  - `pyproject.toml` adds an optional `[test]` extra (pytest only; engines
    remain stdlib-only at runtime).
  - `tests/conftest.py` imports each engine by file path and exposes a
    column-aware `assert_csv_matches` helper so snapshot diffs point at
    the first differing cell, not a 500-line text blob.
  - `tests/test_rightsizing_peak.py` — 5 fixture JSONs cover every verdict
    (DOWNSIZE_CANDIDATE high/medium confidence, KEEP, UPSIZE_WARNING,
    INSUFFICIENT_DATA) plus a guard test on `DECISION_RULES`.
  - `tests/test_context_enricher.py` — end-to-end test that runs the
    engine against a synthetic `hidden-waste.csv` with a mocked tag
    lookup, then snapshots `enriched-*.csv`. Hits all three confidence
    bands (HIGH/MED/LOW).
  - `tests/README.md` documents the "drop a `.json` + `.expected.csv`"
    workflow for adding new fixtures.
- New FAQ entry — **"Where do the £ figures come from?"** — clarifying
  that headline numbers are sourced from Cost Management `ActualCost`
  (your real bill, post-discount), not retail list price. Per-engine
  breakdown shows where list price is used as a fallback (never-attached
  disks in `hidden-waste`) and how `ri-coverage` applies public RI / SP
  discount %s on top of actual PAYG run-rate.
- Top-level README "Currency" section renamed to "Currency and pricing
  source" with a short summary that links to the FAQ entry.

### Notes

- `hidden-waste` and `ri-coverage` test coverage is tracked separately —
  both require an `az_rest` mocking pattern that this PR doesn't
  introduce. See [#18](https://github.com/prbeegala/FinOpsEngine/issues/18)
  follow-ups.

## [0.1.2] - 2026-04-29

### Added

- All three subscription-scoped engines (`rightsizing-peak`,
  `hidden-waste`, `ri-coverage`) now accept `--all-subs` to enumerate
  every Enabled subscription via `az account list` and run tenant-wide,
  removing the need to pass an explicit `--subs` list.
- `--exclude-subs <a,b>` skips named subscriptions when `--all-subs` is
  used (typical: sandboxes, frozen archives). Accepts IDs or display
  names.
- `--tenant <guid>` limits `--all-subs` to a single tenant — useful for
  guest accounts that span tenants.
- `--include-disabled` flag for the rare case where Disabled
  subscriptions should also be scanned (default: skip).
- The engines log `--all-subs resolved N subscription(s).` on startup
  so the resolved scope is visible in CI output.
- `docs/finops-engine-overview.pptx` — 12-slide overview deck for
  introducing the engine to customers and internal teams. Covers the
  problem, the four engines, how it runs, sample output, differentiators
  vs Advisor / FinOps SaaS, the roadmap, and a five-minute get-started.
- `docs/build_overview_pptx.py` — script that generates the deck. Run
  with `python docs/build_overview_pptx.py`. Requires `python-pptx`
  (docs-only dependency; engines remain stdlib-only).

### Changed

- `--subs` is no longer marked `required=True` on its own. Every engine
  now requires **exactly one of** `--subs` or `--all-subs` (enforced by
  an argparse mutually-exclusive group). Supplying neither, or both,
  fails fast with a clear argparse error before any Azure call is made.
- Per-tool READMEs and the top-level Quick start now show both the
  explicit-list and `--all-subs` paths, with a recommended safe default
  (`--all-subs --exclude-subs <sandbox>`).

### Migration notes

Existing automations that pass `--subs "<a>,<b>,<c>"` are unaffected —
the flag still works exactly as before. To switch to tenant-wide,
replace `--subs "<list>"` with `--all-subs` and (optionally)
`--exclude-subs "<list>"`.

## [0.1.1] - 2026-04-29

### Added

- `rightsizing-peak`: seven CLI flags to override the engine's decision
  thresholds at runtime —
  `--downsize-cpu-p95-max`, `--downsize-mem-p95-max`,
  `--downsize-cpu-p99-high-conf`, `--downsize-mem-p99-high-conf`,
  `--upsize-cpu-p95-min`, `--upsize-mem-p95-min`,
  `--min-data-coverage`. Defaults are unchanged from `v0.1.0`.
- `rightsizing-peak`: startup validation of the resulting threshold set.
  Each value is range-checked, and every downsize threshold must be
  strictly less than its matching upsize threshold (otherwise verdicts
  could be ambiguous). Mis-configurations exit with a clear
  `Threshold validation failed:` message.
- `ROADMAP.md` — public backlog of new detectors, engine improvements,
  trust/safety items, productisation, integrations, and multi-cloud
  adapters. Items are size-badged 🟢 / 🟡 / 🔴 and 🛡 for trust/safety.
  Includes an explicit "Out of scope" section (no auto-remediation, no
  savings-% pricing models, no web UI).
- `VERSIONING.md` — explicit SemVer policy covering the four public
  contracts (CLI flags, output file shape, Workbook/Policy IDs, Issue
  body), the deprecation policy, branch model, and `v1.0.0` shipping
  criteria.
- `CHANGELOG.md` (this file).
- `ROADMAP.md` "Issue lifecycle & dedupe" section documenting the three
  known gaps in today's per-owner Issue automation: title-string dedupe
  fragility, no per-finding stable IDs, and undefined closed-issue
  semantics.

### Changed

- `README.md`: replaced the customer-specific anecdote with a
  vendor-neutral "What to expect on a typical run" section. Pointed
  readers at `samples/` for concrete output shapes.
- `tools/rightsizing-peak/README.md`: added a "Tuning thresholds at the
  command line" section with three recommended starting profiles
  (conservative / balanced / aggressive) and guidance on locking
  thresholds for at least a month before re-tuning.

## [0.1.0] - 2026-04-29

Initial public release.

### Added

- **`tools/rightsizing-peak/`** — peak-aware VM rightsizing engine.
  Replaces Azure Advisor's average-based recommendations with P95/P99
  decisions; flags Advisor's `Cost — Resize` recommendations that would
  have been unsafe under peak data.
- **`tools/hidden-waste/`** — seven-category orphan / lifecycle waste
  detector with monthly £ pricing from Cost Management actuals.
  Categories: empty App Service Plans, idle load balancers, old
  snapshots, orphan NICs, stopped-not-deallocated VMs, unattached disks,
  unused public IPs. Includes a starter Azure Policy pack under
  `tools/hidden-waste/policy/`.
- **`tools/ri-coverage/`** — workload-aware Reservations / Compute
  Savings Plan coverage map, risk-scored against a configurable
  cancellation-exposure buffer (default £5,000).
- **`tools/context-enricher/`** — joins the other engines' CSVs with
  per-resource criticality, owner, environment, and confidence; emits
  per-`CODEOWNERS`-team GitHub Issue body templates for HIGH+MED
  findings.
- **`automation/finops-nightly.yml`** — GitHub Actions nightly workflow
  with OIDC auth, artifact retention, and per-owner Issue
  open-or-update.
- **Azure Monitor workbook JSONs** for each engine.
- **`samples/`** — synthetic-but-realistic sample reports for every
  engine, every file flagged "synthetic data" at the top.
- **`docs/`** — `methodology.md`, `troubleshooting.md`, `faq.md`.
- `LICENSE` (MIT), `.gitignore`, `requirements.txt` (stdlib-only — file
  documents the contract).

[Unreleased]: https://github.com/prbeegala/FinOpsEngine/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/prbeegala/FinOpsEngine/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/prbeegala/FinOpsEngine/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/prbeegala/FinOpsEngine/releases/tag/v0.1.0
