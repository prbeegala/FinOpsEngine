# context-enricher

Joins the raw findings from `rightsizing-peak`, `hidden-waste`, and
`ri-coverage` with **resource-tag context** (owner, criticality, environment,
cost-centre, application) and turns each finding into an *approve-ready
review item* — so domain teams can act without a spreadsheet round-trip.

Pairs with `automation/finops-nightly.yml`, the GitHub Actions workflow that
runs all engines nightly and posts per-owner Issues.

## What it does

1. Loads any combination of:
   - `hidden-waste-<date>.csv` — orphan / lifecycle finds.
   - `tenant-peak-rightsizing-savings-<date>.csv` — downsize candidates.
2. Resolves rightsizing rows to full Azure resource IDs via Resource Graph
   (the rightsizing engine emits VM short-names; tag lookup needs the full id).
3. Batches **Resource Graph** queries (100 IDs per call) to fetch tags;
   429/503 backed off with exponential delay.
4. Maps common tag conventions (case-insensitive, first match wins):

   | Field        | Tag keys checked                                                                                |
   |--------------|--------------------------------------------------------------------------------------------------|
   | Owner        | `Owned By`, `Managed By`, `Owner`, `Team`, `Domain`, `Approval Group`, `Support Group`, `Department` |
   | Criticality  | `Business Criticality`, `Service Tier`, `Criticality`, `Tier`, `BusinessCriticality`              |
   | Environment  | `Environment`, `Env`                                                                             |
   | Cost centre  | `Cost Centre`, `Cost Center`                                                                     |
   | Application  | `Service`, `Product`, `Application`, `App`, `AppName`, `Project`                                  |

   The literal values `UNTAGGED`, `N/A`, `TBD`, `TBC`, `None`, `-`, and blanks
   are treated as "no value".
5. Scores each finding deterministically — **no LLM in the path** (the
   design choice is: deterministic baseline first, LLM ranking layered on
   later, only after trust is earned):

   | Band | Rule                                                              | Routing                  |
   |------|-------------------------------------------------------------------|--------------------------|
   | HIGH | owner present AND (criticality OR environment) AND ≥ £100/mo      | Auto-issue to domain owner |
   | MED  | (owner OR criticality) AND ≥ £25/mo                               | FinOps weekly triage     |
   | LOW  | no useful tags OR < £25/mo                                        | Platform-team backlog (tagging-debt) |

6. Writes an enriched CSV, a summary MD, and **one GitHub-Issue body
   markdown per owner** (HIGH+MED only) under `issues/`. The nightly
   workflow `gh issue create -F`s those bodies directly.

## Usage

```pwsh
python context_enricher.py `
  --hidden-waste-csv ./out/hidden-waste/hidden-waste-<date>.csv `
  --rightsizing-csv  ./out/peak-rightsizing/tenant-peak-rightsizing-savings-<date>.csv `
  --out-dir ./out/enriched
```

`az login` required. ~3–8 minutes per 1 500 findings (rate-limited by
Resource Graph, not Cost Management — much faster than `hidden-waste`).

### Dry-run / plan-only mode

Add `--plan-only` to write the planned per-owner Issue bodies to
`<out-dir>/issues-planned/` instead of `<out-dir>/issues/` and skip
the actual Issue creation in CI:

```pwsh
python context_enricher.py `
  --hidden-waste-csv ./out/hidden-waste/hidden-waste-<date>.csv `
  --out-dir ./out/enriched `
  --plan-only
```

This is the **recommended posture for the first run on a new tenant** —
the nightly workflow's `gh issue create` step globs `issues/*.md`, so
writing into `issues-planned/` is a no-op for CI. Reviewers can grep
the directory and confirm the bodies look sane before re-running
without the flag (or with `plan_only=false` from `workflow_dispatch`).
Each body carries a `> ⚠️ DRY-RUN — --plan-only mode.` banner so
operators can't accidentally treat a planned body as a published one.

## Outputs

- `enriched-<date>.csv` — every finding with owner / criticality / env /
  cost-centre / application / confidence / rationale.
- `enriched-<date>.md` — confidence breakdown, top-25, tagging-debt tail.
- `issues/<owner-slug>-<date>.md` — per-owner Issue body, ready for
  `gh issue create -F`. Each body links back to the source engine and
  asks for `accept` / `defer` / `reject` replies per row.
- `issues-planned/<owner-slug>-<date>.md` — same content, dry-run
  banner, written only when `--plan-only` is set.

## Customising the tag taxonomy

Edit the `*_KEYS` tuples at the top of `context_enricher.py`. Lookups are
case-insensitive and first-match-wins, so order matters: list your most
authoritative key first.

```python
OWNER_KEYS       = ("owned by", "managed by", "owner", "team", "domain", ...)
CRITICALITY_KEYS = ("business criticality", "criticality", "service tier", ...)
ENVIRONMENT_KEYS = ("environment", "env")
COSTCENTRE_KEYS  = ("cost centre", "cost center", ...)
APP_KEYS         = ("service", "product", "application", ...)
TAG_PLACEHOLDERS = {"untagged", "n/a", "na", "tbc", "tbd", "none", "-", ""}
```

To wire the per-owner issues to GitHub teams, add a `CODEOWNERS` entry that
maps the slug (lower-cased, non-alphanumeric collapsed to `-`) to the team:

```text
# .github/CODEOWNERS
/issues/contoso-app-team-*    @your-org/contoso-app-team
/issues/data-platform-*        @your-org/data-platform
/issues/platform-team-*        @your-org/cloud-platforms
```

## Limitations

- **Read-only.** No tag writes, no resource modifications.
- **Tag sync is single-shot per run.** A resource re-tagged after the
  nightly snapshot won't reclassify until the next cycle.
- **No reservation utilisation factored in.** The enricher trusts the
  upstream engines; if the `ri-coverage` tool ever gains per-resource
  reservation cover (currently blocked by RBAC in many tenants), this
  enricher won't need changes — just add `--ri-coverage-csv` to inputs.

## Roadmap

- **LLM-ranking layer** — once HIGH/MED accept-rates are trusted, layer a
  small model on top of the rationale field to rewrite it in the owner's
  tone and flag near-duplicates across runs.
- **Per-finding state machine** — store accept/defer/reject replies as
  Issue labels and surface the rejection rate per owner in the Workbook.
- **Snapshot-chain grouping** — aggregate `_yyyy_mm_dd_hhmm` siblings into
  one parent finding so a backup retention chain shows up as a single £
  number, not 12 rows.
