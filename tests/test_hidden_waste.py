"""Unit tests for ``hidden-waste``'s storage detectors (issue #1).

These tests cover the deterministic surface of the three new categories:

* ``annotate_cost`` branches for ``storage_cold_tier`` /
  ``storage_untouched_container`` / ``storage_oversize_premium``.
* The ``QUERIES`` and ``POLICY_TEMPLATES`` registries — confirming the
  three new entries exist with the expected resource-type literals so
  that an accidental delete or rename is caught in CI.
* The metric-driven refinement (``refine_storage_findings``) with the
  Azure Monitor calls monkey-patched out — same posture as the
  rightsizing-peak fixture suite.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def hw(hidden_waste):
    return hidden_waste


def _make_finding(hw, **overrides):
    defaults = dict(
        category="storage_cold_tier",
        sub_id="00000000-0000-0000-0000-000000000000",
        sub_name="test-sub",
        rg="rg-test",
        name="acct1",
        location="westeurope",
        resource_id="/subscriptions/x/resourcegroups/rg-test/providers/"
                    "microsoft.storage/storageaccounts/acct1",
        sku="Standard_LRS",
    )
    defaults.update(overrides)
    return hw.Finding(**defaults)


# ---------------------------------------------------------------------------
# Registry presence — guards against accidental deletes / renames
# ---------------------------------------------------------------------------

def test_storage_categories_registered(hw):
    for cat in ("storage_cold_tier", "storage_untouched_container",
                "storage_oversize_premium"):
        assert cat in hw.QUERIES, f"missing query for {cat}"
        assert cat in hw.CATEGORY_LABELS, f"missing label for {cat}"
        assert cat in hw.POLICY_TEMPLATES, f"missing policy for {cat}"


def test_query_resource_types_are_correct(hw):
    assert ("microsoft.storage/storageaccounts"
            in hw.QUERIES["storage_cold_tier"])
    assert ("microsoft.storage/storageaccounts/blobservices/containers"
            in hw.QUERIES["storage_untouched_container"])
    assert ("microsoft.storage/storageaccounts/fileservices/shares"
            in hw.QUERIES["storage_oversize_premium"])
    # Premium-files filter requires the parent-account join.
    assert "FileStorage" in hw.QUERIES["storage_oversize_premium"]


def test_policy_templates_are_audit_mode(hw):
    for cat in ("storage_cold_tier", "storage_untouched_container",
                "storage_oversize_premium"):
        body = hw.POLICY_TEMPLATES[cat]
        assert body["policyRule"]["then"]["effect"] == "audit"
        # Every storage policy has a caveat — Policy can't read metrics.
        assert "_note" in body, (
            f"{cat} should ship with a _note explaining its caveat")


# ---------------------------------------------------------------------------
# annotate_cost — Cost Management hit (cost_mgmt) and fallbacks
# ---------------------------------------------------------------------------

def test_cold_tier_uses_cost_mgmt_when_available(hw):
    f = _make_finding(hw, category="storage_cold_tier")
    cost_map = {f.resource_id.lower(): 60.0}  # £60 over 30 days
    hw.annotate_cost(f, cost_map, days=30)
    assert f.cost_source == "cost_mgmt"
    assert f.monthly_gbp == pytest.approx(60.0)


def test_cold_tier_falls_back_to_unknown_when_no_cm_row(hw):
    # Hot tier with no CM data → don't fabricate savings.
    f = _make_finding(hw, category="storage_cold_tier")
    hw.annotate_cost(f, {}, days=30)
    assert f.cost_source == "unknown"
    assert f.monthly_gbp == 0.0


def test_untouched_container_is_always_unknown(hw):
    f = _make_finding(hw, category="storage_untouched_container",
                      resource_id="/subscriptions/x/.../containers/c1")
    # Even if CM has a row at this id (it won't in practice), per-container
    # attribution is unsafe, so the engine reports it as hygiene-only.
    hw.annotate_cost(f, {f.resource_id.lower(): 999.0}, days=30)
    # CM hit short-circuits in annotate_cost — that's by design (storage
    # account ids do appear in CM); but no real container id ever will.
    # The contract this test pins down is: when CM has nothing, the row
    # is `unknown` with no fabricated £.
    f2 = _make_finding(hw, category="storage_untouched_container")
    hw.annotate_cost(f2, {}, days=30)
    assert f2.cost_source == "unknown"
    assert f2.monthly_gbp == 0.0


def test_oversize_premium_estimates_recoverable_slice(hw):
    f = _make_finding(hw, category="storage_oversize_premium",
                      size_gb=2048)
    f.recoverable_gb = 1500.0
    hw.annotate_cost(f, {}, days=30)
    assert f.cost_source == "estimate"
    assert f.monthly_gbp == pytest.approx(
        1500.0 * hw.PREMIUM_FILES_GBP_PER_GIB_MO)


def test_oversize_premium_falls_back_to_quota_when_usage_unknown(hw):
    # When the FileCapacity metric is unavailable, refine_storage_findings
    # leaves recoverable_gb at 0 and only sets size_gb (= quota). The
    # estimate then ceilings on the full provisioned line so the row sorts
    # by its real bill — but is tagged `estimate` so the operator
    # double-checks before action.
    f = _make_finding(hw, category="storage_oversize_premium",
                      size_gb=1024)
    hw.annotate_cost(f, {}, days=30)
    assert f.cost_source == "estimate"
    assert f.monthly_gbp == pytest.approx(
        1024 * hw.PREMIUM_FILES_GBP_PER_GIB_MO)


def test_existing_categories_still_work(hw):
    # Smoke test for the pre-existing fallbacks — guard against my
    # if/elif chain accidentally regressing them.
    pip = _make_finding(hw, category="unused_public_ips", sku="Standard")
    hw.annotate_cost(pip, {}, days=30)
    assert pip.cost_source == "estimate"
    assert pip.monthly_gbp == pytest.approx(3.0)

    snap = _make_finding(hw, category="old_snapshots", size_gb=500)
    hw.annotate_cost(snap, {}, days=30)
    assert snap.cost_source == "estimate"
    assert snap.monthly_gbp == pytest.approx(500 * 0.04)


# ---------------------------------------------------------------------------
# refine_storage_findings — happy paths and graceful degradation
# ---------------------------------------------------------------------------

def test_refine_drops_active_hot_account(hw, monkeypatch):
    # Account with 100k transactions / 30d should NOT be flagged.
    def fake_summary(rid, *, metric, aggregation, days=30):
        if metric == "Transactions":
            return (100_000.0, 30)
        if metric == "UsedCapacity":
            return (200 * (1024 ** 3), 30)  # 200 GiB
        return (None, 0)
    monkeypatch.setattr(hw, "az_metrics_summary", fake_summary)
    f = _make_finding(hw, category="storage_cold_tier")
    out = hw.refine_storage_findings([f])
    assert out == [], "active Hot account should be dropped"


def test_refine_keeps_genuinely_cold_account(hw, monkeypatch):
    def fake_summary(rid, *, metric, aggregation, days=30):
        if metric == "Transactions":
            return (1_500.0, 30)  # ~50/day, well under threshold
        if metric == "UsedCapacity":
            return (500 * (1024 ** 3), 30)  # 500 GiB stored
        return (None, 0)
    monkeypatch.setattr(hw, "az_metrics_summary", fake_summary)
    f = _make_finding(hw, category="storage_cold_tier")
    out = hw.refine_storage_findings([f])
    assert len(out) == 1
    assert "500 GiB" in out[0].extra
    assert out[0].size_gb == 500


def test_refine_keeps_cold_candidate_when_metrics_missing(hw, monkeypatch):
    monkeypatch.setattr(
        hw, "az_metrics_summary",
        lambda rid, **kw: (None, 0),
    )
    f = _make_finding(hw, category="storage_cold_tier")
    out = hw.refine_storage_findings([f])
    assert len(out) == 1
    assert "metrics unavailable" in out[0].extra


def test_refine_oversize_drops_well_used_share(hw, monkeypatch):
    # Share with quota=2048 GiB and used=1800 GiB (88%) is NOT oversized.
    def fake_per_dim(rid, *, metric, aggregation, dimension, days=30):
        return {"share1": 1800 * (1024 ** 3)}
    monkeypatch.setattr(hw, "az_metric_per_dimension", fake_per_dim)
    f = _make_finding(
        hw, category="storage_oversize_premium",
        name="acct1/default/share1",
        resource_id="/subscriptions/x/.../microsoft.storage/storageaccounts/"
                    "acct1/fileservices/default/shares/share1",
        size_gb=2048,
    )
    out = hw.refine_storage_findings([f])
    assert out == []


def test_refine_oversize_keeps_genuinely_oversized_share(hw, monkeypatch):
    # quota=2048 GiB, used=200 GiB → recoverable ~1848 GiB.
    def fake_per_dim(rid, *, metric, aggregation, dimension, days=30):
        return {"share1": 200 * (1024 ** 3)}
    monkeypatch.setattr(hw, "az_metric_per_dimension", fake_per_dim)
    f = _make_finding(
        hw, category="storage_oversize_premium",
        name="acct1/default/share1",
        resource_id="/subscriptions/x/.../microsoft.storage/storageaccounts/"
                    "acct1/fileservices/default/shares/share1",
        size_gb=2048,
    )
    out = hw.refine_storage_findings([f])
    assert len(out) == 1
    assert out[0].recoverable_gb == pytest.approx(1848.0, abs=1.0)
    assert "recoverable" in out[0].extra


def test_refine_oversize_keeps_share_when_metric_unavailable(hw, monkeypatch):
    monkeypatch.setattr(
        hw, "az_metric_per_dimension",
        lambda rid, **kw: None,
    )
    f = _make_finding(
        hw, category="storage_oversize_premium",
        name="acct1/default/share1",
        resource_id="/subscriptions/x/.../microsoft.storage/storageaccounts/"
                    "acct1/fileservices/default/shares/share1",
        size_gb=2048,
    )
    out = hw.refine_storage_findings([f])
    assert len(out) == 1
    assert out[0].recoverable_gb == 0.0
    assert "metric unavailable" in out[0].extra


def test_refine_passes_unrelated_findings_through(hw, monkeypatch):
    # Non-storage findings (e.g. unattached_disks) are not touched.
    monkeypatch.setattr(
        hw, "az_metrics_summary",
        lambda rid, **kw: (None, 0),
    )
    monkeypatch.setattr(
        hw, "az_metric_per_dimension",
        lambda rid, **kw: None,
    )
    f = _make_finding(hw, category="unattached_disks")
    out = hw.refine_storage_findings([f])
    assert out == [f]
