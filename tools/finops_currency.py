"""finops_currency.py — runtime currency detection for the FinOps Engine.

Part of the FinOps Engine shared utilities. No third-party dependencies —
uses the Python standard library and the Azure CLI only.

The Cost Management ``/query`` API already returns numbers in the
**tenant's billing currency** (a single tenant has exactly one billing
currency at any point in time). We therefore never need to convert
amounts; we only need to know which symbol to render so report headers,
shortlists, and per-owner Issue bodies stop hard-coding ``£`` for tenants
that are billed in USD / EUR / SEK / etc.

Public API
----------
    CURRENCY_SYMBOLS
        ``dict[str, str]`` mapping ISO-4217 codes to the conventional
        display glyph (defaults fall back to the raw code).

    detect_currency(override=None) -> tuple[str, str, str]
        Returns ``(symbol, iso_code, source)`` where ``source`` is one of
        ``"override"`` / ``"billing-account"`` / ``"default"``. The
        function never raises — failures fall back to ``("£", "GBP",
        "default")`` so existing snapshots / fixtures continue to pass.

    format_amount(symbol, value, dp=0) -> str
        Convenience formatter that renders ``"£1,234"`` / ``"$1,234.56"``
        with thousands separators. Engines that maintain their own
        bespoke formatters (e.g. ``hidden-waste`` switches between
        ``£0.00`` and ``£1,234`` depending on magnitude) are not forced
        to use this — the symbol is the contract, not the formatter.

The detection call is **best-effort, single-shot, and silent on failure**
— it must not block engine startup, and any stderr from ``az`` is
swallowed. Operators on tenants where the API response is unhelpful
should set ``--currency-symbol`` explicitly.
"""
from __future__ import annotations

import json
import subprocess
from typing import Tuple

# ISO-4217 → conventional display glyph. Currencies not in this map fall
# back to the ISO code itself (e.g. ``"NOK"`` rather than guessing
# ``"kr"``). Order is alphabetical by ISO code for diffability.
CURRENCY_SYMBOLS: dict[str, str] = {
    "AUD": "A$",
    "CAD": "C$",
    "CHF": "CHF",
    "CNY": "¥",
    "DKK": "kr",
    "EUR": "€",
    "GBP": "£",
    "HKD": "HK$",
    "INR": "₹",
    "JPY": "¥",
    "KRW": "₩",
    "NOK": "kr",
    "NZD": "NZ$",
    "SEK": "kr",
    "SGD": "S$",
    "TWD": "NT$",
    "USD": "$",
    "ZAR": "R",
}

DEFAULT_SYMBOL = "£"
DEFAULT_ISO = "GBP"


def _symbol_for(iso: str) -> str:
    """Return the display glyph for an ISO code, or the code itself."""
    iso = (iso or "").strip().upper()
    if not iso:
        return DEFAULT_SYMBOL
    return CURRENCY_SYMBOLS.get(iso, iso)


def detect_currency(override: str | None = None) -> Tuple[str, str, str]:
    """Resolve the display currency for an engine run.

    Resolution order:

    1. ``override`` — if a non-empty string is supplied (typically from a
       ``--currency-symbol`` CLI flag), it wins. Returned as-is; no
       lookup, no validation.
    2. ``az billing account list`` — first non-empty
       ``soldToInfo.billingCurrency`` (Microsoft Customer Agreement
       shape) or top-level ``properties.currency`` (Enterprise
       Agreement shape). Mapped via :data:`CURRENCY_SYMBOLS`.
    3. Default ``("£", "GBP", "default")`` — preserves existing
       behaviour and keeps every snapshot fixture green.

    The function **never raises**. Any subprocess failure, JSON parse
    error, or unexpected response shape falls through to the default.

    Returns
    -------
    (symbol, iso_code, source)
        ``source`` is one of ``"override"``, ``"billing-account"``,
        ``"default"`` — used by engines to print a one-line provenance
        message at startup.
    """
    if override:
        return (override, "", "override")

    iso = _query_billing_currency()
    if iso:
        return (_symbol_for(iso), iso, "billing-account")

    return (DEFAULT_SYMBOL, DEFAULT_ISO, "default")


def _query_billing_currency() -> str:
    """Best-effort ``az billing account list`` → ISO code, ``""`` on any failure."""
    try:
        proc = subprocess.run(
            ["az", "billing", "account", "list", "--only-show-errors", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""

    if proc.returncode != 0 or not proc.stdout.strip():
        return ""

    try:
        accounts = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""

    if not isinstance(accounts, list):
        return ""

    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        # Microsoft Customer Agreement: soldToInfo.billingCurrency.
        sold = acc.get("soldToInfo") or {}
        if isinstance(sold, dict):
            cur = sold.get("billingCurrency") or sold.get("currency")
            if cur:
                return str(cur)
        # Enterprise Agreement / legacy: properties.currency.
        props = acc.get("properties") or {}
        if isinstance(props, dict):
            cur = props.get("currency") or props.get("billingCurrency")
            if cur:
                return str(cur)
        # Top-level fallback (some tenants surface it here).
        cur = acc.get("currency") or acc.get("billingCurrency")
        if cur:
            return str(cur)
    return ""


def format_amount(symbol: str, value: float, dp: int = 0) -> str:
    """Render ``value`` with thousands separators prefixed by ``symbol``.

    Engines may keep using f-strings like ``f"{CURRENCY}{x:,.0f}"``;
    this helper exists for the few call sites where the dp count is
    conditional on magnitude.
    """
    return f"{symbol}{value:,.{dp}f}"
