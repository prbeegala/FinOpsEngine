"""Unit tests for ``hidden-waste``'s PaaS rightsizing detectors (issue #4).

Covers:

* Registry presence (``QUERIES`` / ``CATEGORY_LABELS`` /
  ``POLICY_TEMPLATES``) for ``idle_app_service_plan`` and
  ``idle_container_app``.
* ``annotate_cost`` branches for both new categories.
* The ``_percentile`` helper.
* ``refine_paas_findings`` happy paths, threshold-equality, missing-
  metric drop, ``--skip-metrics`` short-circuit, ElasticPremium etc.

Metrics calls are monkey-patched at the module level (no live Azure
calls); same posture as ``test_hidden_waste.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def hw(hidden_waste):
    return hidden_waste


def _asp(hw, **overrides):
    defaults = dict(
        category="idle_app_service_plan",
        sub_id="00000000-0000-0000-0000-000000000000",
        sub_name="test-sub",
        rg="rg-test",
        name="plan1",
        location="westeurope",
        resource_id="/subscriptions/x/resourcegroups/rg-test/providers/"
                    "microsoft.web/serverfarms/plan1",
        sku="P1v3",
    )
    defaults.update(overrides)
    return hw.Finding(**defaults)


def _ca(hw, *, min_replicas: int = 1, **overrides):
    defaults = dict(
        category="idle_container_app",
        sub_id="00000000-0000-0000-0000-000000000000",
        sub_name="test-sub",
        rg="rg-test",
        name="app1",
        location="westeurope",
        resource_id="/subscriptions/x/resourcegroups/rg-test/providers/"
                    "microsoft.app/containerapps/app1",
        extra=str(min_replicas),
    )
    defaults.update(overrides)
    return hw.Finding(**defaults)


# ---------------------------------------------------------------------------
# Registry presence
# ---------------------------------------------------------------------------

def test_paas_categories_registered(hw):
    for cat in ("idle_app_service_plan", "idle_container_app"):
        assert cat in hw.QUERIES, f"missing query for {cat}"
        assert cat in hw.CATEGORY_LABELS, f"missing label for {cat}"
        assert cat in hw.POLICY_TEMPLATES, f"missing policy for {cat}"


def test_asp_query_excludes_free_shared_dynamic_elastic_premium(hw):
    q = hw.QUERIES["idle_app_service_plan"]
    # Sanity: tier exclusion list should mention each of these literals.
    for tier in ("Free", "Shared", "Dynamic", "ElasticPremium"):
        assert tier in q, f"ASP query should exclude tier {tier!r}"
    # And it should require non-empty
    assert "numberOfSites" in q
    assert ">= 1" in q


def test_ca_query_filters_min_replicas_ge_1(hw):
    q = hw.QUERIES["idle_container_app"]
    assert "minReplicas" in q
    assert ">= 1" in q


def test_paas_policies_are_audit_mode(hw):
    for cat in ("idle_app_service_plan", "idle_container_app"):
        body = hw.POLICY_TEMPLATES[cat]
        effect = body["policyRule"]["then"]["effect"]
        assert effect == "audit", f"{cat} policy should be audit, got {effect}"
        assert "_note" in body, f"{cat} policy should carry a _note caveat"


# ---------------------------------------------------------------------------
# _percentile
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vals,q,expected", [
    ([], 95, 0.0),
    ([5.0], 95, 5.0),
    ([1.0, 2.0, 3.0, 4.0, 5.0], 0, 1.0),
    ([1.0, 2.0, 3.0, 4.0, 5.0], 100, 5.0),
    ([1.0, 2.0, 3.0, 4.0, 5.0], 50, 3.0),
])
def test_percentile_edges(hw, vals, q, expected):
    assert hw._percentile(vals, q) == pytest.approx(expected)


def test_percentile_p95_of_uniform(hw):
    # 100 evenly-spaced values 0..99; P95 should land near 94.05
    vals = [float(i) for i in range(100)]
    assert hw._percentile(vals, 95) == pytest.approx(94.05, abs=0.01)


# ---------------------------------------------------------------------------
# annotate_cost
# ---------------------------------------------------------------------------

def test_annotate_cost_idle_asp_uses_cost_mgmt_when_priced(hw):
    f = _asp(hw)
    cost_map = {f.resource_id.lower(): 100.0}
    hw.annotate_cost(f, cost_map, days=30)
    assert f.cost_source == "cost_mgmt"
    assert f.monthly_gbp == pytest.approx(100.0)


def test_annotate_cost_idle_asp_unknown_when_missing(hw):
    f = _asp(hw)
    hw.annotate_cost(f, {}, days=30)
    assert f.cost_source == "unknown"
    assert f.monthly_gbp == 0.0


def test_annotate_cost_idle_container_app_unknown_when_missing(hw):
    f = _ca(hw)
    hw.annotate_cost(f, {}, days=30)
    assert f.cost_source == "unknown"
    assert f.monthly_gbp == 0.0


# ---------------------------------------------------------------------------
# refine_paas_findings — ASP
# ---------------------------------------------------------------------------

def _patch_metrics(monkeypatch, hw, *,
                   timeseries=None,
                   summary=None):
    """Install monkeypatched metric helpers.

    ``timeseries``: dict ``(metric, aggregation) -> list[float] | None`` or
    a callable ``(rid, metric, aggregation, days, interval) -> ...``.
    ``summary``: dict ``(metric, aggregation) -> (value, count)`` or callable.
    """
    def _ts(rid, *, metric, aggregation, days, interval="PT1H"):
        if callable(timeseries):
            return timeseries(rid, metric, aggregation, days, interval)
        if timeseries is None:
            return None
        return timeseries.get((metric, aggregation))

    def _sum(rid, *, metric, aggregation, days):
        if callable(summary):
            return summary(rid, metric, aggregation, days)
        if summary is None:
            return (None, 0)
        v = summary.get((metric, aggregation))
        if v is None:
            return (None, 0)
        return v

    monkeypatch.setattr(hw, "az_metric_timeseries", _ts)
    monkeypatch.setattr(hw, "az_metrics_summary", _sum)


def test_refine_asp_below_threshold_flagged(hw, monkeypatch):
    f = _asp(hw)
    _patch_metrics(monkeypatch, hw,
                   timeseries={("CpuPercentage", "Maximum"):
                               [2.0] * (14 * 24)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert len(out) == 1
    assert "P95 CPU 2.0%" in out[0].extra
    assert "P1v3" in out[0].extra


def test_refine_asp_above_threshold_dropped(hw, monkeypatch):
    f = _asp(hw)
    _patch_metrics(monkeypatch, hw,
                   timeseries={("CpuPercentage", "Maximum"):
                               [50.0] * (14 * 24)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


def test_refine_asp_threshold_equality_drops(hw, monkeypatch):
    """Strict-less-than: P95 == threshold should drop."""
    f = _asp(hw)
    _patch_metrics(monkeypatch, hw,
                   timeseries={("CpuPercentage", "Maximum"):
                               [5.0] * (14 * 24)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


def test_refine_asp_low_coverage_dropped(hw, monkeypatch):
    """Coverage below 80% drops even with low P95."""
    f = _asp(hw)
    _patch_metrics(monkeypatch, hw,
                   timeseries={("CpuPercentage", "Maximum"):
                               # only ~50% coverage
                               [1.0] * (14 * 12)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


def test_refine_asp_metrics_unavailable_dropped(hw, monkeypatch):
    f = _asp(hw)
    _patch_metrics(monkeypatch, hw, timeseries=None)
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


# ---------------------------------------------------------------------------
# refine_paas_findings — Container Apps
# ---------------------------------------------------------------------------

def test_refine_ca_warm_and_quiet_flagged(hw, monkeypatch):
    f = _ca(hw, min_replicas=2)
    _patch_metrics(monkeypatch, hw,
                   summary={("Requests", "Total"): (0.0, 14),
                            ("Replicas", "Average"): (2.0, 14)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert len(out) == 1
    assert "set min-replicas: 0" in out[0].extra
    assert "min-replicas=2" in out[0].extra


def test_refine_ca_with_traffic_dropped(hw, monkeypatch):
    f = _ca(hw, min_replicas=1)
    _patch_metrics(monkeypatch, hw,
                   summary={("Requests", "Total"): (5_000.0, 14),
                            ("Replicas", "Average"): (1.0, 14)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


def test_refine_ca_replicas_scaled_down_dropped(hw, monkeypatch):
    """Replicas ~0 means platform already scales to 0 — not waste."""
    f = _ca(hw, min_replicas=1)
    _patch_metrics(monkeypatch, hw,
                   summary={("Requests", "Total"): (0.0, 14),
                            ("Replicas", "Average"): (0.05, 14)})
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


def test_refine_ca_metrics_unavailable_dropped(hw, monkeypatch):
    f = _ca(hw)
    _patch_metrics(monkeypatch, hw, summary=None)
    out = hw.refine_paas_findings(
        [f], asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0)
    assert out == []


# ---------------------------------------------------------------------------
# --skip-metrics
# ---------------------------------------------------------------------------

def test_refine_skip_metrics_drops_paas_keeps_others(hw, monkeypatch):
    asp = _asp(hw)
    ca = _ca(hw)
    other = hw.Finding(
        category="orphan_nics", sub_id="x", sub_name="x", rg="rg",
        name="nic", location="westeurope", resource_id="/x/nic")

    # Patch with sentinel that would explode if called — proves we skip.
    def _explode(*args, **kwargs):
        raise AssertionError("metric helper called despite skip_metrics")
    monkeypatch.setattr(hw, "az_metric_timeseries", _explode)
    monkeypatch.setattr(hw, "az_metrics_summary", _explode)

    out = hw.refine_paas_findings(
        [asp, ca, other],
        asp_days=14, asp_cpu_p95_max=5.0,
        ca_days=14, ca_requests_max=0,
        skip_metrics=True)
    cats = sorted(f.category for f in out)
    assert cats == ["orphan_nics"]
