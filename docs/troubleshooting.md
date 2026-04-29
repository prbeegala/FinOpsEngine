# Troubleshooting

Common errors when running the FinOps Engine and how to fix them.

## `az login` related

### `AuthenticationRequired` / `AADSTS50079`

You're not logged in, or your token has expired. Run:

```pwsh
az login
az account set --subscription <default-sub-id>
az account show     # confirm tenant + sub are correct
```

If `az login` opens a browser you didn't expect (e.g. you're running on a
build agent), use device code flow:

```pwsh
az login --use-device-code
```

### `403 Forbidden` on Cost Management calls

The caller is missing the **Cost Management Reader** role on the billing
scope or subscription. This is a separate role from Reader.

```pwsh
# Grant on a single sub:
az role assignment create `
  --assignee <user-or-sp-objectId> `
  --role "Cost Management Reader" `
  --scope "/subscriptions/<sub-id>"

# Or, on a management group:
az role assignment create `
  --assignee <user-or-sp-objectId> `
  --role "Cost Management Reader" `
  --scope "/providers/Microsoft.Management/managementGroups/<mg-id>"
```

### `403 Forbidden` on Reservations endpoint

The optional reservation-utilisation pull in `ri-coverage` requires
`Microsoft.Capacity/reservationOrders/read`. The engine **handles this
gracefully** — it logs a warning and continues with PAYG-only data. If
you want the subtraction, request the role on the billing scope.

## Cost Management throttling

Cost Management's `/query` endpoint is aggressively rate-limited. Symptoms:

- `429 Too Many Requests` after a few minutes of `hidden-waste` running.
- The engine pauses, retries with exponential backoff, eventually
  succeeds.

If you see runs taking > 15 minutes per 20 subscriptions, options are:

1. Drop the parallelism (already serial per-sub by default).
2. Run during off-peak hours (the nightly workflow runs at 05:00 UTC
   precisely for this reason).
3. Accept the duration — the engine is single-threaded by design to be
   gentle on the API.

## Resource Graph paging

Resource Graph caps any single response at 1 000 rows. The engines page
via `$skipToken`. If you see a tenant with > 100 000 unattached disks
(extremely rare, but possible at very large scale) the page cap of 100
will truncate. To raise it, edit the `MAX_PAGES` constant near the top
of the relevant engine file.

## Windows quoting (cmd.exe)

The `az rest --body '<json>'` pattern fails silently on Windows cmd.exe
because of how cmd interprets quotes. The engines work around this by
writing the JSON body to a temp file and invoking `az rest --body @file`.

If you are extending the engines and need to add a new `az rest` call
that takes a body, **always use the temp-file pattern**. There are
helpers in each engine's `_az_rest_with_body()` function.

## Memory metrics missing

If the rightsizing CSV has many `INSUFFICIENT_DATA` verdicts driven by
`mem_used_p95 = None`, your VMs likely don't have the Diagnostic
Extension installed. Without it, Azure Monitor doesn't emit
`Available Memory Bytes` from inside the guest.

Two options:

1. **Install the Diagnostic Extension** (or Azure Monitor Agent with the
   Performance counters DCR) on the affected VMs.
2. **Make the engine CPU-only** — set `downsize_mem_p95_max = 100.0` in
   `DECISION_RULES`. The engine will then decide on CPU alone. This
   significantly weakens the safety guarantees; only do it if you've
   accepted the residual risk.

## "No findings" — empty CSVs

Most likely causes:

1. The `--subs` list is empty or wrong. The engine needs subscription
   *IDs* (GUIDs), not names. `az account list -o tsv --query '[].id'`
   to dump the IDs you can see.
2. The caller's permissions don't extend to those subs. `az graph query
   -q "Resources | take 1" --subscriptions "<id1>,<id2>"` will quickly
   tell you.
3. You really do have a clean tenant. Possible but rare; check with a
   single `hidden-waste` run on a known-noisy subscription first.

## Tag values not picked up by `context-enricher`

The enricher matches tag keys *case-insensitively*. If your `Owned By`
tag value is the literal string `UNTAGGED` (a common placeholder), it is
deliberately treated as "no value". Customise the `TAG_PLACEHOLDERS`
set at the top of `context_enricher.py` if you use a different sentinel.

If a tag exists but the enricher reports the resource as untagged, check:

- The key matches one of the `*_KEYS` tuples (case-insensitive).
- The value is not in `TAG_PLACEHOLDERS`.
- The value is non-empty after trimming whitespace.
