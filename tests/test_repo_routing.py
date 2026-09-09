"""Repo routing, and the create/remove key symmetry it has to preserve.

A multi-repo workspace is keyed by (issue, repo). Creating under one repo key
and removing under another leaves the real directory and its Docker volume on
disk, reported as nothing at all — so these assert the two halves agree.
"""
from __future__ import annotations

import asyncio

import pytest

from stokowski.config import HooksConfig, RepoConfig, ServiceConfig
from stokowski.models import Issue
from stokowski.workspace import compose_workspace_key, ensure_workspace, remove_workspace


def _cfg() -> ServiceConfig:
    cfg = ServiceConfig()
    cfg.repos = {
        "api": RepoConfig(name="api", label="repo:api", clone_url="https://x/a.git"),
        "web": RepoConfig(name="web", label="repo:web", clone_url="https://x/w.git", default=True),
    }
    return cfg


def _issue(labels: list[str]) -> Issue:
    return Issue(
        id="uuid-1", identifier="ENG-1", title="t", description="",
        state="Todo", url="https://linear.app/x", labels=labels,
    )


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["repo:api"], "api"),
        (["REPO:API"], "api"),          # label matching is case-insensitive
        (["repo:web"], "web"),
        ([], "web"),                     # unlabelled falls to the default entry
        (["unrelated"], "web"),
    ],
)
def test_label_routes_to_repo(labels, expected):
    assert _cfg().resolve_repo(_issue(labels)).name == expected


def test_a_config_with_no_default_and_no_label_raises():
    """Better a loud failure than silently picking a repo for the operator."""
    cfg = ServiceConfig()
    cfg.repos = {"api": RepoConfig(name="api", label="repo:api", clone_url="https://x/a.git")}
    with pytest.raises(ValueError):
        cfg.resolve_repo(_issue([]))


def test_legacy_single_repo_config_resolves_to_the_synthetic_default():
    """Every pre-multi-repo config must keep working untouched."""
    cfg = ServiceConfig()
    cfg.repos = {"_default": RepoConfig(name="_default", default=True)}
    cfg.repos_synthesized = True
    assert cfg.resolve_repo(_issue(["anything"])).name == "_default"


def test_workspace_is_removed_under_the_key_it_was_created_with(tmp_path):
    """The symmetry that a repo-blind removal breaks.

    ensure_workspace keys by (issue, repo); if removal defaults to `_default`
    while creation used `api`, the directory survives and nothing reports it.
    """
    hooks = HooksConfig()
    repo = "api"

    ws = asyncio.run(ensure_workspace(tmp_path, "ENG-1", hooks, repo_name=repo))
    assert ws.path.exists()
    assert ws.workspace_key == compose_workspace_key("ENG-1", repo)

    # The wrong key must not remove it — this is the failure being guarded.
    asyncio.run(remove_workspace(tmp_path, "ENG-1", hooks, repo_name="_default"))
    assert ws.path.exists(), (
        "removing under the wrong repo key appeared to succeed; a real removal "
        "bug here would leave the workspace and its Docker volume behind"
    )

    asyncio.run(remove_workspace(tmp_path, "ENG-1", hooks, repo_name=repo))
    assert not ws.path.exists(), "the matching key failed to remove the workspace"


def test_distinct_repos_get_distinct_workspaces(tmp_path):
    """Two repos on one issue must not collide onto a single directory."""
    hooks = HooksConfig()
    a = asyncio.run(ensure_workspace(tmp_path, "ENG-1", hooks, repo_name="api"))
    b = asyncio.run(ensure_workspace(tmp_path, "ENG-1", hooks, repo_name="web"))
    assert a.path != b.path
    assert a.path.exists() and b.path.exists()
