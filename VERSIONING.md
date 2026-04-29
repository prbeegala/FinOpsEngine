# Versioning policy

The FinOps Engine follows [Semantic Versioning 2.0.0](https://semver.org/)
applied to four explicit public contracts. This document is the source of
truth — if something here disagrees with a release note, this document wins
and the release note is wrong.

## What's versioned

The repo is versioned as a **single unit**. All four engines, the
automation workflow, the workbook JSONs, and the policy pack share one
version number.

> Why not per-engine versions? They share an output pipeline, an
> automation workflow, and a CSV-schema contract. A breaking change in any
> one of them is a breaking change for users who pin to the repo. Keeping
> a single version is simpler to reason about and simpler to pin.

## Public contracts (what counts as "breaking")

A change is **breaking** if it forces a downstream consumer to change
something. The four contracts are:

| Contract | Breaking change | Examples |
|---|---|---|
| **CLI flags & defaults** | Removing a flag, renaming a flag, changing a default in a way that reverses a verdict on the same input. | Renaming `--downsize-cpu-p95-max` to `--cpu-downsize-p95`. Changing the default `--days` from 30 to 7. |
| **Output file shape** | Removing/renaming a column, changing the meaning of a column, changing the file naming pattern that customer dashboards consume. | Renaming `peak_p95_cpu` to `p95_cpu`. Changing `*-peak-rightsizing-<date>.csv` filename pattern. |
| **Workbook & Policy JSON IDs** | Renaming a query the workbook depends on, renaming a custom-table name (e.g. `PeakRightsizing_CL`), removing a starter policy file. | Renaming `RICoverage_CL` to `Reservations_CL`. |
| **GitHub Issue body shape** | Changing label names that the workflow searches for, changing the finding-fingerprint format once it lands, changing the issue title pattern. | Renaming the `finops` label to `cost`. |

A change that **adds** is not breaking:

- New CLI flags with safe defaults.
- New optional CSV columns appended at the end.
- New Markdown sections.
- New detectors.
- New JSON keys in workbook payloads (consumers must ignore unknown keys).

A **bug fix** that changes a verdict on the same input on a stable release
is not a feature — it is a bug. Test fixtures (planned for `v1.0.0`) are
how we keep ourselves honest about this.

## Version increments

| Bump | When |
|---|---|
| **MAJOR** (`X.0.0`) | Any breaking change to a contract above. |
| **MINOR** (`x.Y.0`) | New flag, new detector, new output column, new optional integration. |
| **PATCH** (`x.y.Z`) | Bug fix that does not change a verdict on stable input; doc-only updates that ship with code; workflow / dependency hygiene. |

### Pre-1.0 carve-out

Until `v1.0.0`, breaking changes **may** ship in MINOR releases — that is
the SemVer rule for `0.y.z`. We will:

1. Always document a breaking change in the release notes.
2. Never make a silent breaking change.
3. Move to strict SemVer the moment `v1.0.0` ships.

If you are pinning to a `0.x` tag, watch the release notes — same as you
would for any other pre-1.0 dependency.

### `v1.0.0` shipping criteria

`v1.0.0` ships only once **all** of the following are true:

- Unit tests cover each engine against fixture CSV/JSON inputs.
- CI runs `python -m py_compile` and the test suite on every PR.
- Every output artifact carries a `schema_version` field that the engines
  refuse to load if greater than expected.
- Label-based Issue dedupe + per-finding stable fingerprints are in.

These items are tracked in [`ROADMAP.md`](./ROADMAP.md). After `v1.0.0`,
any breaking change to a contract above goes in a MAJOR.

## Branch & release model

- **`main` is always shippable.** All work via PR, squash-merge. No
  long-lived feature branches.
- **Tags drive releases.** Cut a release with:

  ```sh
  git tag -a vX.Y.Z -m "vX.Y.Z — <one-line summary>"
  git push origin vX.Y.Z
  gh release create vX.Y.Z --generate-notes \
    --title "vX.Y.Z — <one-line summary>"
  ```

- **No release branches.** If a security fix is needed for an old MAJOR
  line, we'll cherry-pick into a `release/X` branch on demand. Until that
  happens we don't pre-create them.

## Changelog

[`CHANGELOG.md`](./CHANGELOG.md) follows [Keep a Changelog
1.1.0](https://keepachangelog.com/en/1.1.0/) with sections **Added /
Changed / Deprecated / Removed / Fixed / Security**.

Rules:

- Every PR that changes user-facing behaviour updates `CHANGELOG.md` under
  `## [Unreleased]`. Doc-only and CI-only PRs may skip it.
- At release time, `[Unreleased]` is renamed to `[X.Y.Z] - YYYY-MM-DD` and
  a fresh empty `[Unreleased]` is added at the top.
- The release notes on GitHub quote the changelog verbatim.

## Deprecation policy

When a CLI flag, output column, label, or other contract is to be removed:

1. **Announce in MINOR release N.** The flag still works. Its `--help`
   text starts with `[DEPRECATED]`. The CHANGELOG records it under
   `Deprecated`.
2. **Remove in MAJOR release N+1.** At minimum one MINOR release elapses
   between the deprecation announcement and the removal.

Never remove without an announce cycle.

## Pinning guidance for consumers

- **Workbooks and dashboards** — pin to a MAJOR (e.g. `v1.x`). MINOR and
  PATCH releases will not break you.
- **CI pipelines that call the engines** — pin to a MINOR (e.g.
  `v1.4.x`). New flags in a future MINOR won't disturb you.
- **Forks adding custom detectors** — pin to an exact PATCH and rebase
  deliberately. Rebasing across a MINOR is usually trivial; across a
  MAJOR is a real diff.

## Why not CalVer

Considered. Rejected because:

- CalVer hides breaking changes behind a date. The whole point of the
  policy is to tell users *which* upgrades are safe.
- Customers want to pin to "the v1 line" — a CalVer tag like `2026.04`
  carries no such promise.
