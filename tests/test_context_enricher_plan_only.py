"""Tests for ``context-enricher --plan-only`` dry-run mode (issue #21)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "context-enricher" / "input"


def _patch_tags(monkeypatch, mod) -> None:
    raw = json.loads((FIX / "tags.json").read_text(encoding="utf-8"))
    tag_map = {k.lower(): v for k, v in raw.items() if not k.startswith("_")}
    monkeypatch.setattr(mod, "fetch_tags_for_ids", lambda _ids: tag_map)


def test_plan_only_writes_to_issues_planned_not_issues(
    context_enricher, tmp_path, monkeypatch
):
    _patch_tags(monkeypatch, context_enricher)

    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        plan_only=True,
    )

    planned = tmp_path / "issues-planned"
    issues = tmp_path / "issues"

    assert planned.exists() and planned.is_dir(), \
        "plan-only should create out/issues-planned/"
    bodies = list(planned.glob("*.md"))
    assert bodies, "plan-only should still write per-owner Issue bodies"
    assert not issues.exists() or not list(issues.glob("*.md")), \
        "plan-only must NOT write into issues/ — the workflow globs that path"


def test_plan_only_bodies_carry_dry_run_banner(
    context_enricher, tmp_path, monkeypatch
):
    _patch_tags(monkeypatch, context_enricher)
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        plan_only=True,
    )
    bodies = list((tmp_path / "issues-planned").glob("*.md"))
    assert bodies
    for b in bodies:
        text = b.read_text(encoding="utf-8")
        assert "DRY-RUN" in text and "--plan-only" in text, \
            f"missing dry-run banner in {b.name}:\n{text[:200]}"


def test_default_run_writes_to_issues_no_banner(
    context_enricher, tmp_path, monkeypatch
):
    _patch_tags(monkeypatch, context_enricher)
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
    )

    issues = tmp_path / "issues"
    planned = tmp_path / "issues-planned"
    assert issues.exists()
    bodies = list(issues.glob("*.md"))
    assert bodies, "default run should write per-owner bodies under issues/"
    assert not planned.exists(), \
        "default run must NOT create issues-planned/"
    for b in bodies:
        assert "DRY-RUN" not in b.read_text(encoding="utf-8")


def test_plan_only_prints_summary(
    context_enricher, tmp_path, monkeypatch, capsys
):
    _patch_tags(monkeypatch, context_enricher)
    context_enricher.run(
        hidden_waste_csv=FIX / "hidden-waste.csv",
        rightsizing_csv=None,
        out_dir=tmp_path,
        plan_only=True,
    )
    out = capsys.readouterr().out
    assert "PLAN-ONLY" in out
    assert "no GitHub Issues" in out


def test_plan_only_cli_flag_present(context_enricher):
    """--plan-only must be wired into argparse so `--help` documents it."""
    import argparse
    src = Path(context_enricher.__file__).read_text(encoding="utf-8")
    assert "--plan-only" in src
    assert "issues-planned" in src
