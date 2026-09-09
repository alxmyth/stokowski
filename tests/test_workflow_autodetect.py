"""Which workflow file the CLI picks when given no path.

Upstream implements this inline in `cli()` and nothing covered it. The fork's
own version was tested, but as part of a multi-path discovery model that the
convergence dropped in favour of upstream's MultiOrchestrator — so the tests
went with it and took this precedence chain along by accident.

It is worth keeping on its own: an operator with both workflow.yaml and
workflow.yml gets one of them silently, and a wrong precedence is invisible
until the agent runs against the wrong pipeline.
"""
from __future__ import annotations

import sys

import pytest

from stokowski import main as main_mod


@pytest.fixture
def picked(monkeypatch, tmp_path):
    """Run `stokowski --dry-run` in tmp_path and report the workflow it chose."""
    chosen = {}

    def fake_dry_run(path):
        chosen["path"] = path

        async def _noop():
            return None
        return _noop()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "dry_run", fake_dry_run)
    monkeypatch.setattr(sys, "argv", ["stokowski", "--dry-run"])

    def run():
        main_mod.cli()
        return chosen.get("path")
    return run


def test_prefers_workflow_yaml(picked, tmp_path):
    (tmp_path / "workflow.yaml").write_text("polling: {}\n")
    (tmp_path / "workflow.yml").write_text("polling: {}\n")
    (tmp_path / "WORKFLOW.md").write_text("---\npolling: {}\n---\n")
    assert picked().endswith("workflow.yaml")


def test_falls_back_to_workflow_yml(picked, tmp_path):
    (tmp_path / "workflow.yml").write_text("polling: {}\n")
    (tmp_path / "WORKFLOW.md").write_text("---\npolling: {}\n---\n")
    assert picked().endswith("workflow.yml")


def test_falls_back_to_workflow_md(picked, tmp_path):
    (tmp_path / "WORKFLOW.md").write_text("---\npolling: {}\n---\n")
    assert picked().endswith("WORKFLOW.md")


def test_no_workflow_file_exits_rather_than_guessing(picked):
    with pytest.raises(SystemExit) as exc:
        picked()
    assert exc.value.code == 1, "a missing workflow must exit non-zero, not proceed"


def test_an_explicit_path_beats_autodetection(monkeypatch, tmp_path):
    """Auto-detection must never override what the operator actually typed."""
    chosen = {}

    def fake_dry_run(path):
        chosen["path"] = path

        async def _noop():
            return None
        return _noop()

    (tmp_path / "workflow.yaml").write_text("polling: {}\n")
    explicit = tmp_path / "other.yaml"
    explicit.write_text("polling: {}\n")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_mod, "dry_run", fake_dry_run)
    monkeypatch.setattr(sys, "argv", ["stokowski", str(explicit), "--dry-run"])
    main_mod.cli()
    assert chosen["path"].endswith("other.yaml")
