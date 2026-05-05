"""Unit tests for tools/finops_currency.py.

The helper is the only piece of the currency feature that is exercised
during real engine runs (engines themselves are tested via run() which
bypasses detection and inherits the £ default). These tests therefore
focus on the resolution order, the three Azure CLI response shapes the
helper claims to support, and the never-raises contract.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import finops_currency  # noqa: E402


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_az(monkeypatch, response: FakeCompleted | Exception | None):
    """Replace subprocess.run inside finops_currency with a sentinel.

    Pass ``None`` to assert that subprocess.run is never called.
    """
    calls: list[Any] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise AssertionError(
                "subprocess.run should not be called when override is set"
            )
        return response

    monkeypatch.setattr(finops_currency.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# detect_currency — override path
# ---------------------------------------------------------------------------

def test_override_short_circuits_subprocess(monkeypatch):
    calls = _patch_az(monkeypatch, None)
    sym, iso, src = finops_currency.detect_currency(override="$")
    assert (sym, iso, src) == ("$", "", "override")
    assert calls == []


def test_override_passed_through_verbatim(monkeypatch):
    _patch_az(monkeypatch, None)
    # Multi-char glyphs (e.g. 'kr', 'A$') must be returned untouched.
    sym, iso, src = finops_currency.detect_currency(override="kr")
    assert sym == "kr"
    assert src == "override"


# ---------------------------------------------------------------------------
# detect_currency — billing-account path (3 response shapes)
# ---------------------------------------------------------------------------

def test_mca_shape_resolves_currency(monkeypatch):
    payload = json.dumps([
        {"soldToInfo": {"billingCurrency": "USD"}},
    ])
    _patch_az(monkeypatch, FakeCompleted(0, payload))
    sym, iso, src = finops_currency.detect_currency()
    assert (sym, iso, src) == ("$", "USD", "billing-account")


def test_ea_shape_resolves_currency(monkeypatch):
    payload = json.dumps([
        {"properties": {"currency": "EUR"}},
    ])
    _patch_az(monkeypatch, FakeCompleted(0, payload))
    sym, iso, src = finops_currency.detect_currency()
    assert (sym, iso, src) == ("€", "EUR", "billing-account")


def test_top_level_fallback_shape(monkeypatch):
    payload = json.dumps([{"currency": "SEK"}])
    _patch_az(monkeypatch, FakeCompleted(0, payload))
    sym, iso, src = finops_currency.detect_currency()
    assert (sym, iso, src) == ("kr", "SEK", "billing-account")


def test_unknown_iso_falls_back_to_iso_code(monkeypatch):
    payload = json.dumps([{"properties": {"currency": "XYZ"}}])
    _patch_az(monkeypatch, FakeCompleted(0, payload))
    sym, iso, src = finops_currency.detect_currency()
    # XYZ isn't in CURRENCY_SYMBOLS — symbol falls back to the raw ISO.
    assert (sym, iso, src) == ("XYZ", "XYZ", "billing-account")


# ---------------------------------------------------------------------------
# detect_currency — default / failure paths (the never-raises contract)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response", [
    FakeCompleted(1, "", "ERROR: not logged in"),    # az returned non-zero
    FakeCompleted(0, ""),                             # empty stdout
    FakeCompleted(0, "[]"),                           # no billing accounts
    FakeCompleted(0, "{not valid json"),              # malformed JSON
    FakeCompleted(0, json.dumps({"not": "a list"})),  # wrong root type
    FakeCompleted(0, json.dumps([{"soldToInfo": {}}])),  # no currency field
    OSError("az not on PATH"),                        # subprocess raised
    subprocess.TimeoutExpired(cmd="az", timeout=20),  # billing API hung
])
def test_detection_failures_fall_back_to_default(monkeypatch, response):
    _patch_az(monkeypatch, response)
    sym, iso, src = finops_currency.detect_currency()
    assert (sym, iso, src) == ("£", "GBP", "default")


def test_detection_skips_non_dict_entries(monkeypatch):
    payload = json.dumps([None, "string", 42, {"properties": {"currency": "JPY"}}])
    _patch_az(monkeypatch, FakeCompleted(0, payload))
    sym, iso, src = finops_currency.detect_currency()
    assert (sym, iso, src) == ("¥", "JPY", "billing-account")


# ---------------------------------------------------------------------------
# format_amount + CURRENCY_SYMBOLS sanity checks
# ---------------------------------------------------------------------------

def test_format_amount_default_zero_dp():
    assert finops_currency.format_amount("£", 1234.56) == "£1,235"


def test_format_amount_with_dp():
    assert finops_currency.format_amount("$", 1234.5, dp=2) == "$1,234.50"


def test_currency_symbols_covers_major_isos():
    for iso in ("GBP", "USD", "EUR", "AUD", "CAD", "JPY", "INR", "SEK"):
        assert iso in finops_currency.CURRENCY_SYMBOLS, iso
    assert finops_currency.CURRENCY_SYMBOLS["GBP"] == "£"
    assert finops_currency.CURRENCY_SYMBOLS["USD"] == "$"
    assert finops_currency.CURRENCY_SYMBOLS["EUR"] == "€"
