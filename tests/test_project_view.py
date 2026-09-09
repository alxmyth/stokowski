"""Everything the operator configured must survive into the runtime config.

`Orchestrator._project_view` rebuilds a ServiceConfig per project by naming
fields explicitly, so any field it forgets is silently reset to its default
*after* parsing and validation have both passed. That is invisible: the config
is correct, `--dry-run` is clean, and the running system disagrees with it.

It has already happened once. `docker.enabled: true` was dropped here, so
agents ran on the host while every check said they were contained — the exact
failure the config guard had existed to prevent, arriving by another route.

This compares the parsed config against the view field by field rather than
listing the ones we remember, so a field added later is covered by default.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from stokowski.config import ServiceConfig, parse_workflow_file
from stokowski.orchestrator import Orchestrator

# Fields the view is *supposed* to replace with the per-project value.
PER_PROJECT = {
    "tracker", "workspace", "hooks", "claude", "linear_states", "prompts",
    "states", "workflows", "routing", "projects",
}

CONFIG = """
tracker: {api_key: k, project_slug: s}
prompts: {global: g.md}
linear_states: {todo: Todo, active: Doing, review: Review, done: Done}
polling: {interval_ms: 4242}
server: {port: 9876}
logging:
  enabled: true
  max_age_days: 3
  max_total_size_mb: 42
docker:
  enabled: true
  default_image: "my/agent:latest"
  volume_prefix: "custom-prefix"
repos:
  api: {label: "repo:api", clone_url: "git@x:o/a.git", default: true}
states:
  work: {type: agent, prompt: p.md, transitions: {complete: done}}
  done: {type: terminal}
"""


@pytest.fixture
def loaded(tmp_path):
    (tmp_path / "p.md").write_text("stage\n")
    (tmp_path / "g.md").write_text("global\n")
    path = tmp_path / "workflow.yaml"
    path.write_text(CONFIG)
    orch = Orchestrator(str(path))
    errors = orch._load_workflow()
    assert not errors, f"config errors: {errors}"
    return parse_workflow_file(str(path)).config, orch.cfg


def test_no_configured_field_is_lost_in_the_project_view(loaded):
    """The general guard: compare every field, do not enumerate the ones we recall."""
    parsed, view = loaded
    defaults = ServiceConfig()

    lost = []
    for f in dataclasses.fields(ServiceConfig):
        if f.name in PER_PROJECT:
            continue
        want = getattr(parsed, f.name)
        got = getattr(view, f.name)
        if want == got:
            continue
        # Only a value the operator actually set counts as lost.
        if want != getattr(defaults, f.name):
            lost.append(f"{f.name}: configured {want!r}, runtime has {got!r}")

    assert not lost, (
        "_project_view dropped configured values, which parse and validate "
        "cleanly and then do not apply:\n  " + "\n  ".join(lost)
    )


def test_docker_survives_specifically(loaded):
    """Named separately because the consequence is unsandboxed execution."""
    _, view = loaded
    assert view.docker.enabled is True, (
        "docker.enabled was configured true but the runtime config has it "
        "false — agents would run on the host while the config says otherwise"
    )
    assert view.docker.default_image == "my/agent:latest"
    assert view.docker.volume_prefix == "custom-prefix"


def test_logging_survives_specifically(loaded):
    """Named separately because the consequence is unbounded disk growth."""
    _, view = loaded
    assert view.logging.enabled is True
    assert view.logging.max_age_days == 3
    assert view.logging.max_total_size_mb == 42


def test_repos_survive_specifically(loaded):
    """Named separately because the consequence is a silent single-repo run."""
    _, view = loaded
    assert sorted(view.repos) == ["api"]
    assert view.repos_synthesized is False
