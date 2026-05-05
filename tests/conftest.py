"""Pytest configuration and shared helpers.

The four engines under ``tools/<name>/<name>.py`` are scripts, not packages.
We import them by absolute path so the test suite stays decoupled from
packaging choices: tests work whether or not the engines are installed.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_engine(name: str, path: Path) -> ModuleType:
    """Import an engine script by file path and register it in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load engine spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def rightsizing_peak() -> ModuleType:
    return _load_engine(
        "rightsizing_peak",
        TOOLS / "rightsizing-peak" / "rightsizing_peak.py",
    )


@pytest.fixture(scope="session")
def context_enricher() -> ModuleType:
    return _load_engine(
        "context_enricher",
        TOOLS / "context-enricher" / "context_enricher.py",
    )


@pytest.fixture(scope="session")
def hidden_waste() -> ModuleType:
    return _load_engine(
        "hidden_waste",
        TOOLS / "hidden-waste" / "hidden_waste.py",
    )


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


# ---------------------------------------------------------------------------
# CSV diff helpers — used by snapshot tests so failures point at offending
# row/column rather than dumping a thousand lines of text diff.
# ---------------------------------------------------------------------------

def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def assert_csv_matches(actual: Path, expected: Path,
                       *, ignore_columns: tuple[str, ...] = ()) -> None:
    """Assert that two CSVs match row-for-row, column-for-column.

    Failures show the first differing cell with row index, column name,
    expected, and actual — much cheaper to read than a raw text diff.
    """
    a_rows = read_csv_rows(actual)
    e_rows = read_csv_rows(expected)

    assert len(a_rows) == len(e_rows), (
        f"Row count mismatch: actual={len(a_rows)} expected={len(e_rows)}\n"
        f"  actual={actual}\n  expected={expected}"
    )
    if not e_rows:
        return

    e_cols = [c for c in e_rows[0].keys() if c not in ignore_columns]
    a_cols = [c for c in a_rows[0].keys() if c not in ignore_columns]
    assert a_cols == e_cols, (
        f"Column header mismatch:\n  actual={a_cols}\n  expected={e_cols}"
    )

    for i, (a, e) in enumerate(zip(a_rows, e_rows)):
        for col in e_cols:
            assert a[col] == e[col], (
                f"Mismatch at row {i}, column {col!r}:\n"
                f"  actual   = {a[col]!r}\n"
                f"  expected = {e[col]!r}"
            )
