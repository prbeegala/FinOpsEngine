"""End-to-end test for ``context-enricher``.

Feeds the engine a small ``hidden-waste.csv`` and a canned ``tags.json``
(via ``fetch_tags_for_ids`` monkey-patch), then snapshots the engine's
``enriched-<date>.csv`` against ``expected/enriched.csv``.

The date column in the filename is normalised away — we always read
the single produced ``enriched-*.csv``.

To regenerate the snapshot after a deliberate engine change:

    python -c "import json; from pathlib import Path; \
        import tests.test_context_enricher as t; t.regenerate_snapshot()"

or just delete ``expected/enriched.csv`` and run the test once — it will
fail with a useful diff that you can copy in.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.conftest import assert_csv_matches

FIX = Path(__file__).resolve().parent / "fixtures" / "context-enricher"
INPUT = FIX / "input"
EXPECTED = FIX / "expected" / "enriched.csv"


def _run_engine(context_enricher, out_dir: Path, monkeypatch) -> Path:
    """Run the engine with mocked tag lookup; return the produced CSV path."""
    tag_map_raw = json.loads((INPUT / "tags.json").read_text(encoding="utf-8"))
    # Drop the `_doc` key and lowercase resource IDs (engine compares lowercase).
    tag_map = {
        k.lower(): v for k, v in tag_map_raw.items() if not k.startswith("_")
    }

    def fake_fetch_tags(_ids):
        return tag_map

    monkeypatch.setattr(context_enricher, "fetch_tags_for_ids", fake_fetch_tags)

    context_enricher.run(
        hidden_waste_csv=INPUT / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=out_dir,
    )

    csvs = sorted(out_dir.glob("enriched-*.csv"))
    assert len(csvs) == 1, f"expected exactly one enriched-*.csv, got {csvs}"
    return csvs[0]


def test_enriched_csv_matches_snapshot(context_enricher, tmp_path, monkeypatch):
    if not EXPECTED.exists():
        pytest.fail(
            f"Snapshot missing: {EXPECTED}\n"
            f"Run `pytest --regen-context-enricher` (or call "
            f"tests.test_context_enricher.regenerate_snapshot()) to create it."
        )
    actual = _run_engine(context_enricher, tmp_path, monkeypatch)
    assert_csv_matches(actual, EXPECTED)


def regenerate_snapshot() -> None:  # pragma: no cover - dev convenience
    """Helper used when intentionally updating the snapshot."""
    import importlib.util
    import sys
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "context_enricher",
        repo / "tools" / "context-enricher" / "context_enricher.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["context_enricher"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    tag_map_raw = json.loads((INPUT / "tags.json").read_text(encoding="utf-8"))
    tag_map = {k.lower(): v for k, v in tag_map_raw.items() if not k.startswith("_")}
    mod.fetch_tags_for_ids = lambda _ids: tag_map  # type: ignore[attr-defined]

    out = FIX / "_regen"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    mod.run(hidden_waste_csv=INPUT / "hidden-waste.csv",
            rightsizing_csv=None, out_dir=out)
    csvs = sorted(out.glob("enriched-*.csv"))
    assert csvs, "engine did not produce an enriched CSV"
    EXPECTED.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(csvs[0], EXPECTED)
    shutil.rmtree(out)
    print(f"Snapshot regenerated: {EXPECTED}")
