"""Fixture-driven tests for ``rightsizing-peak``'s ``analyse_vm``.

Each ``tests/fixtures/rightsizing-peak/*.json`` file declares:
* ``vm``                  — a ``VmRecord``-shaped dict
* ``samples``             — number of metric points to broadcast
* ``cpu_avg``/``cpu_max``/``mem_used_pct`` — scalar (broadcast),
  list (explicit), or ``{"base","spike","spike_count"}`` (mixed)
* ``expected_verdict``    — DOWNSIZE_CANDIDATE / KEEP / UPSIZE / INSUFFICIENT_DATA
* ``expected_confidence`` — HIGH / MEDIUM / "" (insufficient data)
* ``expected_target_sku`` — ladder target, or "" if engine doesn't propose one

The test monkey-patches ``az_metrics`` so the engine never touches Azure.
``mem_used_pct`` is converted to ``Available Memory Bytes`` using the
SKU memory_gb the test injects via ``sku_cat``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def _expand(value: Any, samples: int, key: str) -> list[float]:
    """Normalise a fixture metric value to a flat list of length ``samples``."""
    if isinstance(value, (int, float)):
        return [float(value)] * samples
    if isinstance(value, list):
        if len(value) != samples:
            raise ValueError(
                f"{key}: explicit list length {len(value)} != samples {samples}"
            )
        return [float(v) for v in value]
    if isinstance(value, dict):
        base = float(value["base"])
        spike = float(value["spike"])
        spike_count = int(value["spike_count"])
        return [base] * (samples - spike_count) + [spike] * spike_count
    raise TypeError(f"{key}: unsupported type {type(value).__name__}")


def _fixtures() -> list[Path]:
    here = Path(__file__).resolve().parent / "fixtures" / "rightsizing-peak"
    return sorted(here.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _fixtures(), ids=lambda p: p.stem)
def test_analyse_vm_matches_expected(rightsizing_peak, fixture_path):
    rp = rightsizing_peak
    spec = json.loads(fixture_path.read_text(encoding="utf-8"))

    samples = int(spec["samples"])
    cpu_avg = _expand(spec["cpu_avg"], samples, "cpu_avg")
    cpu_max = _expand(spec["cpu_max"], samples, "cpu_max")
    mem_used = _expand(spec["mem_used_pct"], samples, "mem_used_pct")

    # Stable SKU catalogue — covers all SKUs referenced by fixtures.
    sku_cat = {
        "Standard_D4ds_v5": rp.SkuCapacity(vcpus=4, memory_gb=16.0),
        "Standard_D2ds_v5": rp.SkuCapacity(vcpus=2, memory_gb=8.0),
        "Standard_D4_v3":   rp.SkuCapacity(vcpus=4, memory_gb=16.0),
        "Standard_D2s_v3":  rp.SkuCapacity(vcpus=2, memory_gb=8.0),
    }
    total_bytes = sku_cat[spec["vm"]["vm_size"]].memory_gb * 1024 ** 3
    mem_min_bytes = [(1.0 - pct / 100.0) * total_bytes for pct in mem_used]

    def fake_az_metrics(_resource_id, *, metric, aggregation, **_):
        if metric == "Percentage CPU" and aggregation == "Average":
            return [{"average": v} for v in cpu_avg]
        if metric == "Percentage CPU" and aggregation == "Maximum":
            return [{"maximum": v} for v in cpu_max]
        if metric == "Available Memory Bytes" and aggregation == "Minimum":
            return [{"minimum": v} for v in mem_min_bytes]
        raise AssertionError(
            f"unexpected az_metrics call: metric={metric} agg={aggregation}"
        )

    # Patch on the loaded module — engine resolves az_metrics via its own globals.
    monkey_orig = rp.az_metrics
    rp.az_metrics = fake_az_metrics
    try:
        vm = rp.VmRecord(**spec["vm"])
        result = rp.analyse_vm(vm, days=7, sku_cat=sku_cat)
    finally:
        rp.az_metrics = monkey_orig

    assert result.verdict == spec["expected_verdict"], (
        f"verdict mismatch: rationale={result.rationale!r}"
    )
    assert result.confidence == spec["expected_confidence"], (
        f"confidence mismatch for verdict {result.verdict}"
    )
    assert result.target_sku == spec["expected_target_sku"], (
        f"target_sku mismatch for verdict {result.verdict}"
    )
    expected_rec = spec.get("expected_recommended_sku", "")
    assert result.recommended_sku == expected_rec, (
        f"recommended_sku mismatch for verdict {result.verdict}: "
        f"got {result.recommended_sku!r}, want {expected_rec!r}"
    )


def test_decision_thresholds_unchanged(rightsizing_peak):
    """Belt-and-braces guard: the verdict fixtures only make sense if these
    thresholds stay put. If you tune them, regenerate fixtures and this test."""
    rules = rightsizing_peak.DECISION_RULES
    assert rules["downsize_cpu_p95_max"] == 40.0
    assert rules["downsize_mem_p95_max"] == 50.0
    assert rules["downsize_cpu_p99_high_conf"] == 50.0
    assert rules["downsize_mem_p99_high_conf"] == 60.0
    assert rules["upsize_cpu_p95_min"] == 80.0
    assert rules["upsize_mem_p95_min"] == 85.0
    assert rules["min_data_coverage"] == 0.80
