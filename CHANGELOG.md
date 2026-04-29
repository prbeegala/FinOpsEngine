# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
as detailed in [`VERSIONING.md`](./VERSIONING.md).

## [Unreleased]

_Nothing yet._

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

[Unreleased]: https://github.com/prbeegala/FinOpsEngine/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/prbeegala/FinOpsEngine/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/prbeegala/FinOpsEngine/releases/tag/v0.1.0
