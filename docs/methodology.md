# Methodology

Why the FinOps Engine looks the way it does. If you're trying to decide
whether to adopt these engines, modify them, or write your own — start
here.

## 1. Peak, not average

Azure Advisor's "Cost — Resize Virtual Machine" recommendation is computed
on the *average* CPU over its observation window. For workloads with a flat
utilisation profile this is fine. For workloads with any of:

- Diurnal peaks (online retail traffic, CMS read load).
- Nightly batch (ETL, reporting, indexing).
- Monthly/quarterly cycles (close-of-books, regulatory submissions).
- Sporadic bursts (CI runners, scheduled jobs, ad-hoc analytics).

…the average is structurally below 40% but the peak is at 100%. Following
Advisor in those cases causes a missed peak — typically a pager event whose
business cost dwarfs years of savings on the resized SKU.

The `rightsizing-peak` engine therefore:

- Pulls per-hour `Maximum CPU` (not just `Average`).
- Computes **P95** and **P99** of those per-hour maxima.
- Decides on those percentiles, not the mean.
- Diffs against Advisor and **flags Advisor's recs that would have been
  unsafe** under peak data. That diff is the headline metric.

The thresholds (40 / 50 / 80 / 85 %) are deliberately conservative. Loosen
them only after several nightly cycles validate the output for your
workload mix.

## 2. Pricing it, not just naming it

Most "find your unused resources" tools stop at the resource list. The FinOps
Engine prices every finding against actual Cost Management data over the
last 30 days, with a published-list-price fallback for resources too new or
too cheap for Cost Management to have aggregated yet.

This matters because the *order* of remediation work is dictated by £, not
by count. In one customer pilot:

- 592 unattached managed disks (54% of all findings) → £4,845 / month
- 54 old snapshots (5% of all findings) → £3,053 / month

That is, a category making up 5% of the row count generated 31% of the £.
Without pricing, the team would have prioritised the disk hunt; with it,
they tackled the snapshot retention chains first.

## 3. The cancellation-exposure buffer

Reservations and Savings Plans charge a 12% cancellation fee. So an
"unbounded best-savings" recommender will quickly stack £100k+ of
hypothetical refund risk if you let it.

The `ri-coverage` engine instead asks: *given a £X buffer of cancellation
exposure you're willing to absorb, which commitments produce the most
savings without breaching it?* Greedy-pack within the buffer, surface
everything outside the buffer as the "what raising the buffer would unlock"
list, and let procurement adjust the buffer rather than the engine adjust
the recommendations.

This pattern is portable beyond Azure — any committed-spend product
(GCP CUDs, AWS Reserved Instances / Savings Plans) has the same
mathematical structure.

## 4. Deterministic first, LLM later

The FinOps Engine has zero LLM calls in its data path. This is by design.

A FinOps recommendation that lands in front of a domain owner on a Monday
morning needs to be *reproducible*. If they ask "why did this finding flip
from MED to HIGH this week?", the answer must be a one-line rule reference
("monthly £ crossed the £100 floor"), not "the model decided".

Once the deterministic baseline is trusted (typically 4–6 weeks of nightly
cycles with single-digit reject rates), an LLM ranking / rephrasing layer
can be added on top of the rationale field to:

- Rewrite the rationale in the owner's tone.
- Flag near-duplicate findings across runs.
- Cluster findings that share a likely root cause.

That layer is *additive*, not load-bearing. The decision logic stays in
the deterministic engines.

## 5. Read-only, by construction

None of the four engines write to Azure. They issue `GET` calls plus a
single `POST /query` to Cost Management (which is itself read-only despite
being a POST). The Azure Policy templates ship in audit mode.

Every remediation is therefore a human decision — usually a domain owner
clicking `accept` on a per-owner GitHub Issue. The engines exist to put
the right decision in front of the right person at the right moment;
they do not act on their behalf.

## 6. Audit-first policy promotion

The Azure Policy starter pack in `tools/hidden-waste/policy/` is designed
to be applied in **audit mode** for at least 30 days before any are
promoted to `deny`. This is non-negotiable:

- It catches false positives the engine couldn't have known about
  (e.g. a disk legitimately detached during a controlled migration).
- It surfaces false negatives — resources that should be flagged but
  aren't matching the Policy's predicate.
- It builds the political buy-in needed to put a `deny` policy on a
  production management group.

Two of the seven categories (`stopped_not_deallocated`, `old_snapshots`)
ship with `_note` annotations explaining that the Policy alone is
insufficient — `powerState` and `timeCreated` are not directly auditable
in Policy and need to be paired with a Workbook query plus (for stopped
VMs) a Function App that re-tags. The annotations are deliberately
inline so reviewers can't miss them.
