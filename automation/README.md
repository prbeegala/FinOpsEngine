# automation

GitHub Actions templates for running the four FinOps engines nightly.

## Files

- `finops-nightly.yml` — runs `rightsizing-peak` + `ri-coverage` +
  `hidden-waste` + `context-enricher` on a 05:00 UTC schedule, uploads the
  artefact bundle, and opens / updates per-owner remediation Issues.

## Deployment

```bash
# In the repo where you want issues raised:
mkdir -p .github/workflows
cp automation/finops-nightly.yml .github/workflows/finops-nightly.yml
git add .github/workflows/finops-nightly.yml
git commit -m "ci: enable FinOps Engine nightly refresh"
```

## Required configuration

| Type     | Name                    | Description |
|----------|-------------------------|-------------|
| Secret   | `AZURE_CLIENT_ID`       | App registration with `Reader` + `Cost Management Reader` on the in-scope management group |
| Secret   | `AZURE_TENANT_ID`       | Your Entra tenant ID |
| Secret   | `AZURE_SUBSCRIPTION_ID` | Default subscription for `az login` |
| Secret   | `GH_PAT` *(optional)*   | PAT with `issues: write` if cross-repo issue raising is wanted; otherwise `GITHUB_TOKEN` is used |
| Variable | `FINOPS_SUBS`           | Comma-separated list of in-scope subscription IDs |
| Variable | `FINOPS_REFUND_BUFFER` *(optional)* | Override the default £5k cancellation-exposure cap |

The federated identity must be configured for the
`repo:<owner>/<repo>:ref:refs/heads/main` subject (or whichever branch the
workflow runs on). See:
[Microsoft Learn — Connect from Azure to GitHub Actions with OIDC](https://learn.microsoft.com/azure/developer/github/connect-from-azure).

## Idempotency

The "Open / update per-owner issues" step looks for an existing open Issue
matching `<owner> — nightly remediation queue`. If found, the body is
overwritten in place; if not, a new Issue is created. This means:

- A finding that disappears from the engine output will silently disappear
  from the Issue body on the next run (no graveyard noise).
- A finding that comes back will be re-listed automatically.
- The `accept` / `defer` / `reject` replies are preserved as Issue comments
  even when the body is overwritten.

## Suggested CODEOWNERS pattern

In the repo where Issues are raised, add a `CODEOWNERS` file that maps
each `Owned By` slug to a GitHub team so the workflow's `@`-mentions route
correctly:

```text
# .github/CODEOWNERS
/issues/contoso-app-team-*    @your-org/contoso-app-team
/issues/data-platform-*        @your-org/data-platform
/issues/platform-team-*        @your-org/cloud-platforms
```

(Slugs come from the enricher's `slug()` function — lowercased, non-alnum
collapsed to `-`.)

## Running the engines locally first

It is strongly recommended to run all four engines manually for at least one
cycle before turning on the nightly workflow. Reasons:

1. **Permissions discovery.** You'll find out very quickly whether your
   identity has Cost Management Reader on every subscription you expect.
2. **Throttle profiling.** The `hidden-waste` engine is the most likely to
   hit Cost Management throttles on a large tenant; running it once
   interactively lets you tune `--max-workers` if needed.
3. **Tag-coverage sanity check.** The `context-enricher` output tells you
   how much tagging debt your tenant has before it lands in front of an
   audience as a per-owner Issue stream.
