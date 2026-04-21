"""Tests for multi-repo config parsing, synthesis, and validation.

Follows the pattern in tests/test_state_machine.py — pure-function tests on
the config module, no mocks, no network, no Linear/Docker. Sibling of the
existing state-machine/workflow tests; kept separate for clarity while
multi-repo plumbing is landing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from stokowski.config import (
    RepoConfig,
    ServiceConfig,
    WorkflowConfig,
    parse_workflow_file,
)


def _write_yaml(content: str) -> Path:
    """Write a YAML fragment to a temp file and return its path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


_MINIMAL_STATES = """
tracker:
  project_slug: abc
  api_key: dummy
states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal
"""


# ── RepoConfig dataclass ────────────────────────────────────────────────────


def test_repo_config_defaults():
    """RepoConfig constructs with sensible defaults."""
    r = RepoConfig(name="api")
    assert r.name == "api"
    assert r.label is None
    assert r.clone_url == ""
    assert r.default is False
    assert r.docker_image is None


def test_repo_config_full():
    """RepoConfig accepts all expected fields."""
    r = RepoConfig(
        name="api",
        label="repo:api",
        clone_url="git@github.com:org/api.git",
        default=True,
        docker_image="stokowski/node:latest",
    )
    assert r.label == "repo:api"
    assert r.clone_url == "git@github.com:org/api.git"
    assert r.default is True
    assert r.docker_image == "stokowski/node:latest"


# ── WorkflowConfig.triage field ─────────────────────────────────────────────


def test_workflow_config_triage_defaults_false():
    """WorkflowConfig.triage defaults to False so existing configs unchanged."""
    w = WorkflowConfig(name="standard")
    assert w.triage is False


def test_workflow_config_triage_explicit():
    """Operators can set triage=True on a workflow."""
    w = WorkflowConfig(name="intake", triage=True)
    assert w.triage is True


# ── Legacy synthesis (no `repos:` section) ──────────────────────────────────


def test_parse_legacy_synthesizes_default_repo():
    """Configs with no `repos:` section get a synthetic _default entry."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
hooks:
  after_create: 'git clone foo .'
"""
    )
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is True
    assert "_default" in cfg.repos
    assert len(cfg.repos) == 1

    default_repo = cfg.repos["_default"]
    assert default_repo.name == "_default"
    assert default_repo.label is None
    assert default_repo.clone_url == ""
    assert default_repo.default is True
    assert default_repo.docker_image is None


def test_parse_legacy_no_hooks_still_synthesizes():
    """Even without hooks, absent `repos:` triggers synthesis."""
    path = _write_yaml(_MINIMAL_STATES)
    parsed = parse_workflow_file(path)

    assert parsed.config.repos_synthesized is True
    assert "_default" in parsed.config.repos


# ── Explicit `repos:` registry ──────────────────────────────────────────────


def test_parse_explicit_repos_registry():
    """Explicit `repos:` section parses each entry correctly."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
    default: true
  web:
    label: repo:web
    clone_url: git@github.com:org/web.git
    docker_image: stokowski/node:latest
"""
    )
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is False
    assert len(cfg.repos) == 2

    api = cfg.repos["api"]
    assert api.name == "api"
    assert api.label == "repo:api"
    assert api.clone_url == "git@github.com:org/api.git"
    assert api.default is True
    assert api.docker_image is None

    web = cfg.repos["web"]
    assert web.default is False
    assert web.docker_image == "stokowski/node:latest"


def test_parse_explicit_empty_repos_no_synthesis():
    """Explicit `repos: {}` is distinct from absent — NO synthesis.

    Validation (Unit 3) surfaces this as an error; parsing leaves `repos` empty
    so the validator has an unambiguous signal.
    """
    path = _write_yaml(_MINIMAL_STATES + "\nrepos: {}\n")
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is False
    assert cfg.repos == {}


# ── WorkflowConfig.triage parsing ───────────────────────────────────────────


def test_parse_workflow_triage_flag():
    """Operators designate a triage workflow via `triage: true`."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
workflows:
  standard:
    label: workflow:standard
    default: true
    path: [work, done]
  intake:
    label: workflow:intake
    triage: true
    path: [work, done]
"""
    )
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.workflows["standard"].triage is False
    assert cfg.workflows["intake"].triage is True


def test_parse_workflow_triage_defaults_false_when_absent():
    """Workflows without explicit `triage:` default to False (backward compat)."""
    path = _write_yaml(
        _MINIMAL_STATES
        + """
workflows:
  standard:
    label: workflow:standard
    default: true
    path: [work, done]
"""
    )
    parsed = parse_workflow_file(path)
    assert parsed.config.workflows["standard"].triage is False


# ── Legacy-mode detection symmetry with workflows ───────────────────────────


def test_legacy_synthesis_symmetry_workflow_and_repo():
    """Legacy configs synthesize both _default workflow AND _default repo.

    Mirrors the existing multi-workflow _default pattern — a legacy config
    should have exactly one auto-generated entry in each of cfg.workflows
    and cfg.repos, both keyed '_default' with default=True.
    """
    path = _write_yaml(_MINIMAL_STATES)
    parsed = parse_workflow_file(path)
    cfg = parsed.config

    # Workflow side (pre-existing behavior)
    assert len(cfg.workflows) == 1
    assert "_default" in cfg.workflows
    assert cfg.workflows["_default"].default is True

    # Repo side (new in this change)
    assert len(cfg.repos) == 1
    assert "_default" in cfg.repos
    assert cfg.repos["_default"].default is True
    assert cfg.repos_synthesized is True
