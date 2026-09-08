"""The seam between upstream's code and this fork's additive layers.

Convergence put upstream's `orchestrator.py` in charge of calling modules we
own (`workspace.py`, and `docker_runner.py` behind it). Nothing in either test
suite exercises that seam: upstream's tests never import our modules, and ours
never drive upstream's orchestrator.

That gap already shipped a real break. Our `ensure_workspace` took `repo_name`
third for multi-repo support, upstream's orchestrator calls it with three
positional arguments expecting `hooks` there, and every agent dispatch would
have raised TypeError — with the whole suite green.

These tests bind the orchestrator's real call shapes against our real
signatures. When a fork feature is re-applied from `tests_pending/`, any
parameter it adds must stay keyword-optional or these fail.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from stokowski.config import HooksConfig, ServiceConfig
from stokowski.workspace import ensure_workspace, remove_workspace

REPO = Path(__file__).resolve().parent.parent
ORCHESTRATOR = REPO / "stokowski" / "orchestrator.py"


@pytest.mark.parametrize("fn", [ensure_workspace, remove_workspace])
def test_upstream_orchestrator_call_shape_binds(fn):
    """Upstream calls these as (root, identifier, hooks) and nothing more."""
    inspect.signature(fn).bind(Path("/tmp"), "ENG-123", HooksConfig())


@pytest.mark.parametrize("fn", [ensure_workspace, remove_workspace])
def test_fork_extras_are_keyword_optional(fn):
    """A fork parameter that is not optional breaks upstream's call site.

    This is the rule that keeps re-applying a feature cheap: add capability as
    a defaulted keyword, never as a new required positional.
    """
    params = list(inspect.signature(fn).parameters.values())
    required = [
        p.name for p in params
        if p.default is inspect.Parameter.empty
        and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert required == ["workspace_root", "issue_identifier", "hooks"], (
        f"{fn.__name__} requires {required}; upstream's orchestrator passes only "
        f"(workspace_root, issue_identifier, hooks). Extra parameters must be "
        f"keyword arguments with defaults."
    )


def _calls_to(module_path: Path, names: set[str]) -> list[ast.Call]:
    tree = ast.parse(module_path.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            ident = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if ident in names:
                out.append(node)
    return out


@pytest.mark.parametrize("name", ["ensure_workspace", "remove_workspace"])
def test_every_orchestrator_call_site_binds(name):
    """Bind each real call in orchestrator.py, not just the shape we assume.

    Guards against upstream changing its call site during a future merge.
    """
    fn = {"ensure_workspace": ensure_workspace, "remove_workspace": remove_workspace}[name]
    sig = inspect.signature(fn)
    calls = _calls_to(ORCHESTRATOR, {name})
    assert calls, f"no call to {name} found in orchestrator.py — did the seam move?"
    for call in calls:
        kwargs = {kw.arg: None for kw in call.keywords if kw.arg}
        try:
            sig.bind(*([None] * len(call.args)), **kwargs)
        except TypeError as exc:
            pytest.fail(
                f"orchestrator.py:{call.lineno} calls {name} with "
                f"{len(call.args)} positional + {sorted(kwargs)} — {exc}"
            )


def test_docker_config_survived_convergence():
    """DockerConfig is a fork addition re-applied onto upstream's ServiceConfig."""
    cfg = ServiceConfig()
    assert hasattr(cfg, "docker"), "ServiceConfig lost the fork's docker field"
    assert cfg.docker.enabled is False, "docker must default off for upstream configs"
    assert cfg.docker_if_enabled is None, "disabled docker must resolve to None"
