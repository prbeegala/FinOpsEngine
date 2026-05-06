"""Tests for the YAML / tag / CODEOWNERS owner-resolution chain.

Covers:

* YAML override file wins over Azure tags.
* ``--owner-tag-keys`` re-targets which tag keys are read.
* CODEOWNERS path-glob fallback fires when neither YAML nor tags resolve.
* Unrouted findings are recorded as ``owner_source=unrouted``.
* The new ``owner_source`` column is emitted in the enriched CSV.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "context-enricher" / "input"


def _patch_tags(monkeypatch, mod) -> None:
    raw = json.loads((FIX / "tags.json").read_text(encoding="utf-8"))
    tag_map = {k.lower(): v for k, v in raw.items() if not k.startswith("_")}
    monkeypatch.setattr(mod, "fetch_tags_for_ids", lambda _ids: tag_map)


def _read_enriched(out_dir: Path) -> list[dict[str, str]]:
    csvs = sorted(out_dir.glob("enriched-*.csv"))
    assert len(csvs) == 1, csvs
    with csvs[0].open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# CSV column
# ---------------------------------------------------------------------------

def test_owner_source_column_present(context_enricher, tmp_path, monkeypatch):
    _patch_tags(monkeypatch, context_enricher)
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
    )
    rows = _read_enriched(tmp_path)
    assert "owner_source" in rows[0], rows[0].keys()
    sources = {r["owner_source"] for r in rows}
    # Tag-resolved rows + one untagged row.
    assert sources == {"tag", "unrouted"}, sources


# ---------------------------------------------------------------------------
# YAML override
# ---------------------------------------------------------------------------

def test_yaml_override_beats_tag(context_enricher, tmp_path, monkeypatch):
    _patch_tags(monkeypatch, context_enricher)
    yml = tmp_path / "owners.yaml"
    yml.write_text(
        "overrides:\n"
        "  - resource_group: rg-app-data\n"
        "    owner: data-platform-override\n",
        encoding="utf-8",
    )
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        owner_yaml=yml,
    )
    rows = _read_enriched(tmp_path)
    by_name = {r["name"]: r for r in rows}
    disk = by_name["disk-orphan-001"]
    # YAML wins over the tag value (`platform-storage`).
    assert disk["owner"] == "data-platform-override"
    assert disk["owner_source"] == "yaml"
    # A finding *not* matched by the YAML still falls through to its tag.
    assert by_name["asp-empty-eu"]["owner"] == "web-team"
    assert by_name["asp-empty-eu"]["owner_source"] == "tag"


def test_yaml_override_via_json_file(context_enricher, tmp_path, monkeypatch):
    """JSON files with a matching schema are accepted (subset of YAML)."""
    _patch_tags(monkeypatch, context_enricher)
    j = tmp_path / "owners.json"
    j.write_text(json.dumps({
        "overrides": [
            {"name": "disk-orphan-001", "owner": "json-team"},
        ]
    }), encoding="utf-8")
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        owner_yaml=j,
    )
    rows = _read_enriched(tmp_path)
    disk = next(r for r in rows if r["name"] == "disk-orphan-001")
    assert disk["owner"] == "json-team"
    assert disk["owner_source"] == "yaml"


# ---------------------------------------------------------------------------
# --owner-tag-keys
# ---------------------------------------------------------------------------

def test_owner_tag_keys_can_redirect_lookup(context_enricher, tmp_path,
                                            monkeypatch):
    """Restricting --owner-tag-keys to a non-existent key suppresses tag hits."""
    _patch_tags(monkeypatch, context_enricher)
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        owner_tag_keys=("nosuchkey",),
    )
    rows = _read_enriched(tmp_path)
    # No row should have an owner — every finding is unrouted.
    assert all(r["owner"] == "" for r in rows)
    assert {r["owner_source"] for r in rows} == {"unrouted"}


# ---------------------------------------------------------------------------
# CODEOWNERS fallback
# ---------------------------------------------------------------------------

def test_codeowners_fallback_routes_unowned(context_enricher, tmp_path,
                                            monkeypatch):
    _patch_tags(monkeypatch, context_enricher)
    co = tmp_path / "CODEOWNERS"
    co.write_text(
        "# untagged public IPs land here\n"
        "*publicipaddresses* @org/network-team\n",
        encoding="utf-8",
    )
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        codeowners=co,
    )
    rows = _read_enriched(tmp_path)
    by_name = {r["name"]: r for r in rows}
    pip = by_name["pip-leaky-001"]
    # The untagged Public IP now routes via CODEOWNERS.
    assert pip["owner"] == "org/network-team"
    assert pip["owner_source"] == "codeowners"
    # Tag-routed findings are unaffected — CODEOWNERS only fires when the
    # earlier sources produced nothing.
    assert by_name["disk-orphan-001"]["owner_source"] == "tag"


def test_codeowners_does_not_override_yaml_or_tag(context_enricher, tmp_path,
                                                  monkeypatch):
    _patch_tags(monkeypatch, context_enricher)
    co = tmp_path / "CODEOWNERS"
    co.write_text("* @org/everyone\n", encoding="utf-8")
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        codeowners=co,
    )
    rows = _read_enriched(tmp_path)
    disk = next(r for r in rows if r["name"] == "disk-orphan-001")
    # Tag still wins over CODEOWNERS.
    assert disk["owner"] == "platform-storage"
    assert disk["owner_source"] == "tag"


# ---------------------------------------------------------------------------
# Unit-level tests for the helpers (don't need the full run path).
# ---------------------------------------------------------------------------

def test_parse_simple_yaml_overrides(context_enricher):
    text = (
        "# header comment\n"
        "overrides:\n"
        "  - resource_id: /subscriptions/abc/disks/foo\n"
        "    owner: team-a\n"
        "  - resource_group: rg-data  # rg-level\n"
        "    sub_name: sub-prod\n"
        "    owner: data-team\n"
    )
    rules = context_enricher._parse_simple_yaml_overrides(text)
    assert rules == [
        {"resource_id": "/subscriptions/abc/disks/foo", "owner": "team-a"},
        {"resource_group": "rg-data", "sub_name": "sub-prod",
         "owner": "data-team"},
    ]


def test_load_codeowners_picks_first_at_owner(context_enricher, tmp_path):
    co = tmp_path / "CODEOWNERS"
    co.write_text(
        "# comment\n"
        "/foo/* @org/team-a @org/reviewer\n"
        "*.snap @org/storage\n",
        encoding="utf-8",
    )
    rules = context_enricher.load_codeowners(co)
    assert rules == [("/foo/*", "org/team-a"), ("*.snap", "org/storage")]
