"""Tests for workspace key composition and ensure/remove signatures.

Focuses on Unit 4's composite `{issue}-{repo}` key shape. Docker-path tests
live in tests/test_docker_runner.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from stokowski.config import HooksConfig
from stokowski.workspace import (
    compose_workspace_key,
    ensure_workspace,
    remove_workspace,
    sanitize_key,
)


def _run(coro):
    """Run an async coroutine in a fresh event loop. Keeps tests sync-shaped
    to match the existing test_cancel.py / test_docker_runner.py pattern."""
    return asyncio.run(coro)


# ── compose_workspace_key ───────────────────────────────────────────────────


def test_compose_workspace_key_basic():
    # `{len}-{issue}-{repo}` — length prefix makes parsing unambiguous
    assert compose_workspace_key("SMI-14", "api") == "6-SMI-14-api"


def test_compose_workspace_key_legacy_default():
    """Legacy synthesized _default repo carries the length prefix too."""
    assert compose_workspace_key("SMI-14", "_default") == "6-SMI-14-_default"


def test_compose_workspace_key_sanitizes_repo():
    """Slashes in repo name are sanitized (path safety)."""
    assert compose_workspace_key("SMI-14", "my/repo") == "6-SMI-14-my_repo"


def test_compose_workspace_key_sanitizes_issue():
    """Slashes in issue identifier are also sanitized."""
    assert compose_workspace_key("SMI/14", "api") == "6-SMI_14-api"


def test_compose_workspace_key_preserves_safe_chars():
    """Underscores, dots, hyphens all pass through."""
    assert compose_workspace_key("SMI-14", "py.service_3") == "6-SMI-14-py.service_3"


def test_compose_workspace_key_distinct_per_repo():
    """Two repos on the same issue produce distinct keys."""
    key_a = compose_workspace_key("SMI-14", "api")
    key_b = compose_workspace_key("SMI-14", "web")
    assert key_a != key_b


def test_compose_workspace_key_distinct_per_issue():
    """Two issues on the same repo produce distinct keys."""
    key_a = compose_workspace_key("SMI-14", "api")
    key_b = compose_workspace_key("SMI-15", "api")
    assert key_a != key_b


def test_compose_workspace_key_adv_001_collision_prevented():
    """ADV-001 regression: before the length prefix, `(SMI-my, repo)` and
    `(SMI, my-repo)` both mapped to `SMI-my-repo` and shared a workspace
    directory. The length prefix distinguishes them."""
    key_a = compose_workspace_key("SMI-my", "repo")   # "6-SMI-my-repo"
    key_b = compose_workspace_key("SMI", "my-repo")    # "3-SMI-my-repo"
    assert key_a != key_b


def test_compose_workspace_key_hyphen_heavy_components_distinct():
    """Exhaustive cross-component hyphen check — every 3-split of `a-b-c`
    over (issue, repo) must produce a distinct key."""
    keys = {
        compose_workspace_key("a-b-c", "x"),
        compose_workspace_key("a-b", "c-x"),
        compose_workspace_key("a", "b-c-x"),
        compose_workspace_key("a-b-c-x", ""),  # empty repo sanitizes to "" → "5-a-b-c-x-"
    }
    # The fourth one with empty repo is a bit degenerate but still should
    # produce a distinct value from the other three
    # Every key must be unique (no collisions)
    assert len(keys) == 4


# ── ensure_workspace / remove_workspace signatures (non-Docker path) ────────


def test_ensure_workspace_creates_composite_path(tmp_path):
    """ensure_workspace creates `{root}/{len}-{issue}-{repo}`."""
    hooks = HooksConfig()  # no hooks, just directory creation
    result = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))

    expected = tmp_path / "6-SMI-14-api"
    assert expected.exists()
    assert expected.is_dir()
    assert result.path == expected
    assert result.workspace_key == "6-SMI-14-api"
    assert result.created_now is True


def test_ensure_workspace_reuses_existing_dir(tmp_path):
    """Second call to ensure_workspace for same (issue, repo) reuses."""
    hooks = HooksConfig()
    first = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    second = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))

    assert first.path == second.path
    assert second.created_now is False


def test_ensure_workspace_different_repos_different_paths(tmp_path):
    """Same issue, different repos → different workspace dirs."""
    hooks = HooksConfig()
    ws_a = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    ws_b = _run(ensure_workspace(tmp_path, "SMI-14", "web", hooks))

    assert ws_a.path != ws_b.path
    assert ws_a.path.exists()
    assert ws_b.path.exists()


def test_ensure_workspace_legacy_default_repo(tmp_path):
    """Legacy `_default` repo produces the length-prefixed key."""
    hooks = HooksConfig()
    result = _run(ensure_workspace(tmp_path, "SMI-14", "_default", hooks))

    assert result.path == tmp_path / "6-SMI-14-_default"
    assert result.workspace_key == "6-SMI-14-_default"


def test_remove_workspace_removes_composite_path(tmp_path):
    """remove_workspace removes the composite-keyed dir."""
    hooks = HooksConfig()
    ws = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    assert ws.path.exists()

    _run(remove_workspace(tmp_path, "SMI-14", "api", hooks))
    assert not ws.path.exists()


def test_remove_workspace_missing_is_idempotent(tmp_path):
    """remove_workspace on non-existent dir is a silent no-op."""
    hooks = HooksConfig()
    # Should not raise
    _run(remove_workspace(tmp_path, "SMI-14", "api", hooks))


def test_remove_workspace_only_targets_specified_repo(tmp_path):
    """Removing (issue, api) leaves (issue, web) workspace intact."""
    hooks = HooksConfig()
    ws_a = _run(ensure_workspace(tmp_path, "SMI-14", "api", hooks))
    ws_b = _run(ensure_workspace(tmp_path, "SMI-14", "web", hooks))

    _run(remove_workspace(tmp_path, "SMI-14", "api", hooks))

    assert not ws_a.path.exists()
    assert ws_b.path.exists()
