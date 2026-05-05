# Hidden Waste & Lifecycle

**Generated**: 2026-01-01 06:21 UTC
**Window**: last 30 days
**Subscriptions scanned**: 20

> ⚠️  **Synthetic data — illustration only.** Numbers below are a hand-crafted
> example; they are not from any real Azure tenant.

## Headline

- Total flagged resources: **1,196**
- Estimated monthly £ recoverable (deletion or rightsizing): **£12,952**
- Annualised: **£155,424**

## By category

| Category                                  | Count | Monthly £ | Annualised £ |
|-------------------------------------------|------:|----------:|-------------:|
| Unattached managed disks                  |   592 |    £4,845 |      £58,143 |
| Old snapshots (>90d)                      |    54 |    £3,053 |      £36,635 |
| Hot-tier storage accounts (cold workload) |    11 |    £2,210 |      £26,520 |
| Empty App Service Plans                   |    20 |    £1,717 |      £20,607 |
| Oversized premium file shares             |     6 |      £820 |       £9,840 |
| Under-utilised App Service Plans          |     1 |      £178 |       £2,136 |
| Unused public IPs                         |    46 |       £91 |       £1,098 |
| Idle Standard load balancers              |     2 |       £30 |         £360 |
| Stopped-not-deallocated VMs               |     1 |     £6.67 |          £80 |
| Orphan NICs                               |   420 |     £0.00 |        £0.00 |
| Untouched blob containers (>90d)          |    38 |     £0.00 |        £0.00 |
| Idle Container Apps (warm replicas)       |     5 |     £0.00 |        £0.00 |

## Top 25 individual offenders

| Sub                  | RG                       | Resource                          | Category                                  | Monthly £ | Source       |
|----------------------|--------------------------|-----------------------------------|-------------------------------------------|----------:|--------------|
| ContosoMigration.Prod| rg-asr-replicas          | disk-asr-seed-uks-01              | Unattached managed disks                  |    £2,752 | cost_mgmt    |
| ContosoData.Prod     | rg-archive-blob          | stcontosoarchive01                | Hot-tier storage accounts (cold workload) |      £820 | cost_mgmt    |
| ContosoApp.Prod      | rg-snap-archive-2024     | snap-bkp-2024-q4-chain            | Old snapshots (>90d)                      |      £318 | cost_mgmt    |
| ContosoApp.Prod      | rg-snap-archive-2024     | snap-bkp-2024-q3-chain            | Old snapshots (>90d)                      |      £312 | cost_mgmt    |
| ContosoData.Prod     | rg-files-prod-uks        | stcontosofiles01/default/share-archive-old | Oversized premium file shares    |      £288 | estimate     |
| ContosoBatch.Prod    | rg-legacy-portal         | asp-legacy-portal-001             | Empty App Service Plans                   |      £180 | cost_mgmt    |
| ContosoBatch.Prod    | rg-platform-uks          | asp-platform-shared-01            | Under-utilised App Service Plans          |      £178 | cost_mgmt    |
| ContosoData.Prod     | rg-archive-disks         | disk-old-sql-data-04              | Unattached managed disks                  |      £176 | cost_mgmt    |
| ContosoApp.Prod      | rg-legacy-portal         | asp-legacy-api-002                | Empty App Service Plans                   |      £172 | cost_mgmt    |
| ContosoBatch.Prod    | rg-snap-archive-2024     | snap-bkp-2024-q2-chain            | Old snapshots (>90d)                      |      £168 | cost_mgmt    |
| ...                  | ...                      | ...                               | ...                                       |       ... | ...          |

## Recommended Azure Policy guardrails (top 3 by £)

The audit-mode policy JSON is in `policy/`. Promote to `deny` only after a
30-day audit cycle confirms zero false positives:

1. `unattached_disks.audit.json` — flag disks where `diskState =~ 'Unattached'`.
2. `old_snapshots.audit.json` — flag any new snapshot creation (apply with
   the workbook KQL filter on `properties.timeCreated > 90d`; Policy alone
   cannot filter on `timeCreated`).
3. `storage_cold_tier.audit.json` — flag GPv2 / blob storage accounts in
   the `Hot` access tier; pair with the engine's metric refinement
   (Transactions / UsedCapacity) to drop genuinely active workloads.

## Method notes

- Resource Graph for enumeration; Cost Management `/query` for actual £ over
  the last 30 days. Storage detectors run an additional best-effort Azure
  Monitor pass (`Transactions`, `UsedCapacity`, `FileCapacity`) to filter
  the Hot-tier and oversized-share candidates down to the genuine waste.
  PaaS rightsizing detectors (`idle_app_service_plan`,
  `idle_container_app`) run an Azure Monitor pass on
  `CpuPercentage` / `Requests` / `Replicas`; candidates whose metrics
  can't be retrieved are dropped (different posture from storage —
  see `tools/hidden-waste/README.md` for the rationale).
- `cost_mgmt` source = actual £ from Cost Management.
- `estimate` source = published-rate or quota-based fallback when Cost
  Management has no row for the resource (e.g. never-attached disks,
  premium files line items split across an account).
- `unknown` source = no £ attribution available; verify manually. Used for
  hygiene-only findings (orphan NICs, untouched containers, idle
  Container Apps under consumption pricing) and for Hot-tier accounts
  where Cost Management has no row to anchor to.

_Generated by `hidden-waste`._
