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
import importlib
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


# Modules this fork owns outright; every other module is upstream's and may be
# replaced wholesale by the next merge.
FORK_MODULES = {"workspace", "docker_runner"}


def _cross_seam_calls():
    """Every call from an upstream-owned module into a fork-owned one.

    Discovered by parsing, not listed by hand, so a call site upstream adds or
    moves in a future merge is covered the moment it lands.
    """
    found = []
    for path in sorted((REPO / "stokowski").glob("*.py")):
        if path.stem in FORK_MODULES:
            continue
        tree = ast.parse(path.read_text())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORK_MODULES:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        if not imported:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                ident = getattr(node.func, "id", None)
                if ident in imported:
                    module, real = imported[ident]
                    found.append((path.name, node.lineno, module, real, node))
    return found


def test_the_seam_is_actually_covered():
    """A discovery test that finds nothing is a test that guarantees nothing."""
    calls = _cross_seam_calls()
    assert len(calls) >= 5, (
        f"only {len(calls)} upstream->fork call sites found; the seam moved or "
        f"the import style changed, so the binding test below is now vacuous"
    )


@pytest.mark.parametrize(
    "caller,lineno,module,name,call",
    _cross_seam_calls(),
    ids=lambda v: f"{v}" if isinstance(v, (str, int)) else "",
)
def test_every_upstream_call_into_fork_code_binds(caller, lineno, module, name, call):
    """Upstream calls our modules; nothing in either suite drives that path.

    Binding the real call sites against the real signatures is what stands in
    for the integration test neither side has.
    """
    fn = getattr(importlib.import_module(f"stokowski.{module}"), name)
    kwargs = {kw.arg: None for kw in call.keywords if kw.arg}
    try:
        inspect.signature(fn).bind(*([None] * len(call.args)), **kwargs)
    except TypeError as exc:
        pytest.fail(
            f"{caller}:{lineno} calls {module}.{name} with {len(call.args)} "
            f"positional + {sorted(kwargs)} — {exc}"
        )


def test_docker_config_survived_convergence():
    """DockerConfig is a fork addition re-applied onto upstream's ServiceConfig."""
    cfg = ServiceConfig()
    assert hasattr(cfg, "docker"), "ServiceConfig lost the fork's docker field"
    assert cfg.docker.enabled is False, "docker must default off for upstream configs"
    assert cfg.docker_if_enabled is None, "disabled docker must resolve to None"


def test_enabling_docker_while_unwired_is_refused():
    """Failing open here means agents run on the host, not in a container.

    Docker config parses today but nothing consumes it, so accepting
    `enabled: true` would silently drop the isolation an operator asked for.
    Delete this test when the Docker layer is re-applied — its failure is then
    the signal that the guard is stale.
    """
    from stokowski.config import DockerConfig, ProjectConfig, validate_config

    cfg = ServiceConfig(projects=[ProjectConfig(name="p")])
    assert not [e for e in validate_config(cfg) if "docker" in e], (
        "docker disabled must not produce an error"
    )

    cfg.docker = DockerConfig(enabled=True)
    errors = [e for e in validate_config(cfg) if "docker" in e]
    assert errors, "docker.enabled=true was accepted while Docker is not wired"
    assert "unsandboxed" in errors[0], "the error must say what actually goes wrong"


def test_workspace_creation_uses_the_state_merged_hooks():
    """A fork patch on an upstream line — so this test doubles as its tripwire.

    `merge_state_config` resolves a state's hook overrides, and the runner
    honours them. Upstream's workspace-creation call passes the root hooks
    instead, so a state-level `after_create` is silently ignored while
    `before_run` on the same state works.

    Upstream still carries the original line, so a future merge can quietly
    revert this. That is the point: when it does, this fails and names the fix
    rather than letting the override go quiet again.
    """
    tree = ast.parse(ORCHESTRATOR.read_text())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ensure_workspace"
    ]
    assert calls, "ensure_workspace call not found in orchestrator.py"

    for call in calls:
        hooks_arg = call.args[2] if len(call.args) > 2 else None
        rendered = ast.unparse(hooks_arg) if hooks_arg is not None else "<missing>"
        assert rendered == "hooks_cfg", (
            f"orchestrator.py:{call.lineno} passes {rendered!r} as hooks to "
            f"ensure_workspace. It must pass 'hooks_cfg' — the value "
            f"merge_state_config produced — or a state-level after_create "
            f"override is dropped. If a merge just reverted this, re-apply the "
            f"fork patch and send it upstream."
        )


def test_unwired_fork_config_keys_are_refused():
    """A removed feature whose config key still parses is a silent downgrade.

    `repos:` survived the convergence in two shipped example files and in the
    README's copy-this instruction, while `RepoConfig` parsing did not. The
    config validated cleanly and every `repo:` label was ignored, so a
    multi-repo pipeline quietly ran against one repo.

    Each entry is deleted from UNWIRED_FORK_KEYS when its layer is re-applied.
    """
    from stokowski.config import UNWIRED_FORK_KEYS, parse_workflow_file, validate_config

    assert UNWIRED_FORK_KEYS, "registry is empty — delete this test with the last entry"

    for name in ("workflow.multi-repo.example.yaml", "workflow.multi-repo-triage.example.yaml"):
        path = REPO / name
        if not path.exists():
            continue
        errors = validate_config(parse_workflow_file(str(path)).config)
        assert any("repos:" in e for e in errors), (
            f"{name} ships a repos: block but validates clean — an operator "
            f"following README's copy instruction gets a silent single-repo run"
        )

    # Upstream's own example must stay clean, or the guard is too broad.
    assert not validate_config(parse_workflow_file(str(REPO / "workflow.example.yaml")).config)
