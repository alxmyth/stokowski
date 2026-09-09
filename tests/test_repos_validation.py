"""The repos: registry rejects what it should before anything runs.

Replaces the validation half of the fork's pre-convergence test_repos_config.py,
which was 815 lines built on a config model the convergence replaced — its
fixtures declared workflows inline with `path:` lists. The rules themselves are
still live and several are security-relevant, so they are re-tested here against
upstream's config shape rather than rehabilitated on a dead one.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from stokowski.config import parse_workflow_file, validate_config

_BASE = """
tracker: {api_key: k, project_slug: s}
prompts: {global: g.md}
linear_states: {todo: Todo, active: Doing, review: Review, done: Done}
states:
  work: {type: agent, prompt: p.md, transitions: {complete: done}}
  done: {type: terminal}
"""


def _errors(tmp_path: Path, repos_block: str) -> list[str]:
    (tmp_path / "p.md").write_text("stage\n")
    (tmp_path / "g.md").write_text("global\n")
    path = tmp_path / "workflow.yaml"
    path.write_text(_BASE + textwrap.dedent(repos_block))
    return validate_config(parse_workflow_file(str(path)).config)


def _has(errors: list[str], *fragments: str) -> bool:
    return any(all(f in e for f in fragments) for e in errors)


def test_a_valid_registry_passes(tmp_path):
    errors = _errors(tmp_path, """
        repos:
          api: {label: "repo:api", clone_url: "https://github.com/o/a.git", default: true}
          web: {label: "repo:web", clone_url: "git@github.com:o/w.git"}
    """)
    assert not errors, errors


def test_embedded_credentials_in_a_clone_url_are_rejected(tmp_path):
    """A URL with user:pass@ ends up in hook scripts, logs and process lists."""
    errors = _errors(tmp_path, """
        repos:
          api:
            label: "repo:api"
            clone_url: "https://user:hunter2@github.com/o/a.git"
            default: true
    """)
    assert _has(errors, "api", "credentials"), errors


def test_a_file_scheme_clone_url_is_rejected(tmp_path):
    """file:// would let a workflow file point an agent at the local disk."""
    errors = _errors(tmp_path, """
        repos:
          api: {label: "repo:api", clone_url: "file:///etc", default: true}
    """)
    assert _has(errors, "api", "file://"), errors


def test_an_unknown_clone_url_scheme_is_rejected(tmp_path):
    errors = _errors(tmp_path, """
        repos:
          api: {label: "repo:api", clone_url: "ftp://example.com/a.git", default: true}
    """)
    assert _has(errors, "api", "clone_url"), errors


def test_a_repo_name_that_is_unsafe_as_a_path_is_rejected(tmp_path):
    """The name becomes part of a filesystem path and a Docker volume name."""
    errors = _errors(tmp_path, """
        repos:
          "../escape": {label: "repo:x", clone_url: "https://x/a.git", default: true}
    """)
    assert _has(errors, "invalid characters"), errors


def test_the_reserved_default_name_cannot_be_authored(tmp_path):
    """`_default` is the legacy synthesis marker; an operator entry would shadow it."""
    errors = _errors(tmp_path, """
        repos:
          _default: {label: "repo:d", clone_url: "https://x/a.git", default: true}
    """)
    assert _has(errors, "_default", "reserved"), errors


def test_an_empty_clone_url_or_label_is_rejected(tmp_path):
    errors = _errors(tmp_path, """
        repos:
          api: {label: "repo:api", clone_url: "", default: true}
          web: {label: "", clone_url: "https://x/w.git"}
    """)
    assert _has(errors, "api", "clone_url"), errors
    assert _has(errors, "web", "label"), errors


def test_two_repos_cannot_share_a_label(tmp_path):
    """Otherwise routing picks one by dict order and the other is unreachable."""
    errors = _errors(tmp_path, """
        repos:
          api: {label: "repo:same", clone_url: "https://x/a.git", default: true}
          web: {label: "repo:same", clone_url: "https://x/w.git"}
    """)
    assert any("label" in e for e in errors), errors


def test_a_legacy_config_with_no_registry_still_validates(tmp_path):
    """Every pre-multi-repo config must keep working untouched."""
    assert not _errors(tmp_path, "")


def test_an_explicitly_empty_registry_falls_back_to_legacy(tmp_path):
    """`repos:` with nothing under it is an operator mistake, not a hard error."""
    errors = _errors(tmp_path, "repos:\n")
    assert not errors, errors
    (tmp_path / "p.md").write_text("stage\n")
    cfg = parse_workflow_file(str(tmp_path / "workflow.yaml")).config
    assert cfg.repos_synthesized is True
