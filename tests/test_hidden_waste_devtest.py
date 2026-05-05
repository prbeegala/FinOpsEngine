"""Unit tests for ``hidden-waste``'s dev/test auto-shutdown gap detectors
(issue #5).

Covers:

* Registry presence — three categories with labels, KQL embedding the
  shared ``DEVTEST_ENV_VALUES`` list.
* ``annotate_cost`` applies ``DEVTEST_WASTE_RATIO`` to the Cost-
  Management bill; fallback when CM has no row.
* ``refine_devtest_findings`` — VM uptime confirmation with metrics
  monkey-patched out (skip-metrics, missing-metrics, low-coverage,
  high-coverage paths). SQL/AKS pass through untouched.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def hw(hidden_waste):
    return hidden_waste


def _vm(hw, **overrides):
    defaults = dict(
        category="devtest_no_shutdown_vm",
        sub_id="00000000-0000-0000-0000-000000000000",
        sub_name="test-sub",
        rg="rg-dev",
        name="vm-dev-1",
        location="westeurope",
        resource_id="/subscriptions/x/resourcegroups/rg-dev/providers/"
                    "microsoft.compute/virtualmachines/vm-dev-1",
        sku="Standard_D4ds_v5",
        extra="dev",
    )
    defaults.update(overrides)
    return hw.Finding(**defaults)


# ---------------------------------------------------------------------------
# Registry presence
# ---------------------------------------------------------------------------

def test_devtest_categories_registered(hw):
    for cat in ("devtest_no_shutdown_vm", "devtest_no_shutdown_sql",
                "devtest_no_shutdown_aks"):
        assert cat in hw.QUERIES, f"missing query for {cat}"
        assert cat in hw.CATEGORY_LABELS, f"missing label for {cat}"
    assert hw.DEVTEST_CATS == frozenset({
        "devtest_no_shutdown_vm",
        "devtest_no_shutdown_sql",
        "devtest_no_shutdown_aks",
    })


def test_devtest_queries_target_correct_resource_types(hw):
    assert ("microsoft.compute/virtualmachines"
            in hw.QUERIES["devtest_no_shutdown_vm"])
    # VM detector must left/right-anti join against DTL shutdown
    # schedules so VMs *with* a schedule are excluded.
    assert ("microsoft.devtestlab/schedules"
            in hw.QUERIES["devtest_no_shutdown_vm"])
    assert "ComputeVmShutdownTask" in hw.QUERIES["devtest_no_shutdown_vm"]
    assert "rightanti" in hw.QUERIES["devtest_no_shutdown_vm"]
    # AKS-managed RGs and node-pool VMs must be excluded so we don't
    # flag platform-spawned compute.
    assert "databricks-rg-" in hw.QUERIES["devtest_no_shutdown_vm"]
    assert "mc_" in hw.QUERIES["devtest_no_shutdown_vm"]

    assert ("microsoft.sql/servers/databases"
            in hw.QUERIES["devtest_no_shutdown_sql"])
    # Serverless tier (auto-pause-capable) must be excluded.
    assert "GP_S_" in hw.QUERIES["devtest_no_shutdown_sql"]

    assert ("microsoft.containerservice/managedclusters"
            in hw.QUERIES["devtest_no_shutdown_aks"])
    # Only currently-running clusters are waste candidates.
    assert "Running" in hw.QUERIES["devtest_no_shutdown_aks"]


def test_queries_use_shared_devtest_value_list(hw):
    # Each detector must filter on the same canonical env-tag value
    # list so context-enricher and hidden-waste can never disagree.
    from tag_keys import DEVTEST_ENV_VALUES
    sample_values = ("dev", "test", "uat", "sandbox", "preprod",
                     "non-prod", "staging")
    for cat in ("devtest_no_shutdown_vm", "devtest_no_shutdown_sql",
                "devtest_no_shutdown_aks"):
        q = hw.QUERIES[cat]
        for v in sample_values:
            assert v in DEVTEST_ENV_VALUES, (
                f"sample value {v!r} drifted out of DEVTEST_ENV_VALUES")
            assert f"'{v}'" in q, f"{cat} missing dev/test value {v!r}"


# ---------------------------------------------------------------------------
# annotate_cost — wasted-ratio applied to CM, unknown otherwise
# ---------------------------------------------------------------------------

def test_devtest_vm_applies_waste_ratio_to_cm_bill(hw):
    f = _vm(hw)
    cost_map = {f.resource_id.lower(): 100.0}  # £100 over 30 days
    hw.annotate_cost(f, cost_map, days=30)
    assert f.cost_source == "cost_mgmt"
    # 12h × 5d cadence vs 24×7 → 108/168 wasted.
    assert f.monthly_gbp == pytest.approx(100.0 * (108.0 / 168.0))


def test_devtest_sql_applies_waste_ratio_to_cm_bill(hw):
    f = _vm(hw, category="devtest_no_shutdown_sql",
            name="srv1/db1", sku="GP_Gen5_2")
    cost_map = {f.resource_id.lower(): 70.0}
    hw.annotate_cost(f, cost_map, days=30)
    assert f.cost_source == "cost_mgmt"
    assert f.monthly_gbp == pytest.approx(70.0 * hw.DEVTEST_WASTE_RATIO)


def test_devtest_aks_falls_back_to_unknown_when_no_cm_row(hw):
    f = _vm(hw, category="devtest_no_shutdown_aks", name="aks-dev")
    hw.annotate_cost(f, {}, days=30)
    assert f.cost_source == "unknown"
    assert f.monthly_gbp == 0.0


def test_non_devtest_categories_keep_full_monthly(hw):
    # Sanity: my new branch must not affect the existing CM short-
    # circuit for non-devtest categories.
    f = hw.Finding(
        category="unattached_disks",
        sub_id="x", sub_name="test", rg="rg", name="disk1",
        location="westeurope",
        resource_id="/subscriptions/x/.../disk1",
        sku="Premium_LRS", size_gb=128,
    )
    hw.annotate_cost(f, {f.resource_id.lower(): 30.0}, days=30)
    assert f.monthly_gbp == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# refine_devtest_findings
# ---------------------------------------------------------------------------

def test_refine_keeps_high_uptime_vm(hw, monkeypatch):
    # 14d × 24h = 336 expected hourly samples; 320 ≈ 95.2% coverage.
    monkeypatch.setattr(
        hw, "az_metric_timeseries",
        lambda rid, **kw: [10.0] * 320,
    )
    f = _vm(hw)
    out = hw.refine_devtest_findings(
        [f], uptime_days=14, uptime_threshold=0.95)
    assert len(out) == 1
    assert "uptime" in out[0].extra
    assert "95%" in out[0].extra or "96%" in out[0].extra


def test_refine_drops_low_uptime_vm(hw, monkeypatch):
    # 14d × 24h = 336; 100 samples ≈ 30% coverage — drop.
    monkeypatch.setattr(
        hw, "az_metric_timeseries",
        lambda rid, **kw: [10.0] * 100,
    )
    f = _vm(hw)
    out = hw.refine_devtest_findings(
        [f], uptime_days=14, uptime_threshold=0.95)
    assert out == [], "intermittent VM should not be flagged"


def test_refine_keeps_vm_when_metrics_unavailable(hw, monkeypatch):
    # Best-effort: keep with explanatory tag rather than silently drop.
    monkeypatch.setattr(
        hw, "az_metric_timeseries",
        lambda rid, **kw: None,
    )
    f = _vm(hw)
    out = hw.refine_devtest_findings(
        [f], uptime_days=14, uptime_threshold=0.95)
    assert len(out) == 1
    assert "uptime=unknown" in out[0].extra


def test_refine_skip_metrics_keeps_all_vms(hw, monkeypatch):
    # Should not call az_metric_timeseries at all.
    def boom(*args, **kwargs):
        raise AssertionError("metric call should be skipped")
    monkeypatch.setattr(hw, "az_metric_timeseries", boom)
    f = _vm(hw)
    out = hw.refine_devtest_findings(
        [f], uptime_days=14, uptime_threshold=0.95, skip_metrics=True)
    assert len(out) == 1
    assert "metric-skipped" in out[0].extra


def test_refine_passes_through_sql_and_aks(hw, monkeypatch):
    # Non-VM dev/test categories must not trigger metric calls.
    def boom(*args, **kwargs):
        raise AssertionError("metric call should not fire for SQL/AKS")
    monkeypatch.setattr(hw, "az_metric_timeseries", boom)
    sql = _vm(hw, category="devtest_no_shutdown_sql", name="srv1/db1")
    aks = _vm(hw, category="devtest_no_shutdown_aks", name="aks-dev")
    out = hw.refine_devtest_findings(
        [sql, aks], uptime_days=14, uptime_threshold=0.95)
    cats = sorted(f.category for f in out)
    assert cats == ["devtest_no_shutdown_aks", "devtest_no_shutdown_sql"]


def test_refine_no_op_when_no_devtest_findings(hw):
    # Pure pass-through must not blow up when there's nothing to do.
    f = hw.Finding(
        category="unattached_disks", sub_id="x", sub_name="t",
        rg="rg", name="d1", location="weu",
        resource_id="/subscriptions/x/.../d1", sku="Premium_LRS",
    )
    out = hw.refine_devtest_findings(
        [f], uptime_days=14, uptime_threshold=0.95)
    assert out == [f]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_devtest_waste_ratio_is_12h_5d_cadence(hw):
    # 168 hours/week running today, 60 target (12h × 5d).
    assert hw.DEVTEST_WASTE_RATIO == pytest.approx(108.0 / 168.0)
    assert hw.DEVTEST_WASTE_RATIO < 0.65 and hw.DEVTEST_WASTE_RATIO > 0.64
