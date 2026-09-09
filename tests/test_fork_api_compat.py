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


def _cross_seam_calls(package_dir=None):
    """Every call from an upstream-owned module into a fork-owned one.

    Discovered by parsing, not listed by hand, so a call site upstream adds or
    moves in a future merge is covered the moment it lands.
    """
    found = []
    package_dir = package_dir or (REPO / "stokowski")
    for path in sorted(package_dir.glob("*.py")):
        if path.stem in FORK_MODULES:
            continue
        tree = ast.parse(path.read_text())
        imported = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in FORK_MODULES:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, alias.name)
        # `import x.workspace as ws` / `from . import workspace` -> ws.fn(...)
        # Built before the skip: a module that imports only this way has an
        # empty `imported` and would otherwise be dropped before we look.
        module_aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is None:
                for alias in node.names:
                    if alias.name in FORK_MODULES:
                        module_aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    tail = alias.name.rsplit(".", 1)[-1]
                    if tail in FORK_MODULES:
                        module_aliases[alias.asname or tail] = tail

        if not imported and not module_aliases:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # bare name: `ensure_workspace(...)` after `from .workspace import ...`
            ident = getattr(func, "id", None)
            if ident in imported:
                module, real = imported[ident]
                found.append((path.name, node.lineno, module, real, node))
                continue
            # attribute: `workspace.ensure_workspace(...)`
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                module = module_aliases.get(func.value.id)
                if module:
                    found.append((path.name, node.lineno, module, func.attr, node))
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
    mod = importlib.import_module(f"stokowski.{module}")
    fn = getattr(mod, name, None)
    assert fn is not None, (
        f"{caller}:{lineno} calls {module}.{name}, which {module}.py does not define"
    )
    kwargs = {kw.arg: None for kw in call.keywords if kw.arg}
    try:
        inspect.signature(fn).bind(*([None] * len(call.args)), **kwargs)
    except TypeError as exc:
        pytest.fail(
            f"{caller}:{lineno} calls {module}.{name} with {len(call.args)} "
            f"positional + {sorted(kwargs)} — {exc}"
        )


def test_docker_config_survived_convergence():
    """DockerConfig is a fork addition re-applied onto upstream's ServiceConfig.

    Both branches of `docker_if_enabled` are asserted deliberately. An earlier
    version checked only the disabled branch, so replacing the whole property
    with `return None` left the suite green — mutation testing caught it. A
    property with two branches needs two assertions or it is not pinned.
    """
    from stokowski.config import DockerConfig  # noqa: PLC0415

    cfg = ServiceConfig()
    assert hasattr(cfg, "docker"), "ServiceConfig lost the fork's docker field"
    assert cfg.docker.enabled is False, "docker must default off for upstream configs"
    assert cfg.docker_if_enabled is None, "disabled docker must resolve to None"

    cfg.docker = DockerConfig(enabled=True, default_image="python:3.12")
    resolved = cfg.docker_if_enabled
    assert resolved is not None, (
        "docker_if_enabled returns None even when docker is enabled — the "
        "property is not reading self.docker.enabled"
    )
    assert resolved.default_image == "python:3.12", (
        "docker_if_enabled returned something other than the live config"
    )


def test_workspace_creation_does_not_use_the_merged_hooks():
    """Guards a regression this fork shipped and reverted, not an upstream bug.

    It is tempting to pass `hooks_cfg` here: `merge_state_config` computed it
    just above, the runner honours it, and passing the root config means a
    state-level `after_create` override is ignored. That reasoning is wrong,
    and the reason is in `merge_state_config` itself — it replaces the hooks
    object wholesale instead of merging per field, and `_parse_hooks` fills
    unset keys with None.

    So a state declaring only `on_stage_enter` — an ordinary thing to write,
    and the only place `on_stage_enter` can be read from — yields
    `after_create=None`. Passing `hooks_cfg` drops the root's clone step: the
    workspace is created empty, `_ensure_git_ignored` returns early on a
    non-git directory so nothing raises, and the agent is dispatched into an
    empty directory to report on a repository that is not there. It also resets
    `timeout_ms` to the 60s default.

    Measured, before the revert:

        merged.after_create : None
        workspace contents  : []          # with hooks_cfg
        workspace contents  : ['cloned.txt']   # with self.cfg.hooks

    Upstream's inconsistency is the lesser bug and goes upstream with the merge
    semantics question. Do not "fix" this line without fixing `merge_state_config`
    first.
    """
    import ast

    tree = ast.parse(ORCHESTRATOR.read_text())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ensure_workspace"
    ]
    assert calls, "ensure_workspace call not found in orchestrator.py"
    for call in calls:
        rendered = ast.unparse(call.args[2]) if len(call.args) > 2 else "<missing>"
        assert rendered == "self.cfg.hooks", (
            f"orchestrator.py:{call.lineno} passes {rendered!r} as hooks to "
            f"ensure_workspace. Passing the merged hooks_cfg drops the root's "
            f"after_create for any state that declares a hooks block, creating "
            f"an empty workspace that is dispatched into. Read this test's "
            f"docstring before changing it."
        )


def test_merge_state_config_replaces_hooks_wholesale():
    """Pins the behaviour the test above depends on.

    If upstream ever changes `merge_state_config` to merge hooks per field,
    this fails — and that is the signal that passing `hooks_cfg` at the
    workspace call site becomes correct and the fork can drop its comment.
    """
    from stokowski.config import ClaudeConfig, HooksConfig, StateConfig, merge_state_config

    root = HooksConfig(after_create="clone.sh", timeout_ms=90_000)
    state = StateConfig(type="agent")
    state.hooks = HooksConfig(on_stage_enter="enter.sh")

    _, merged = merge_state_config(state, ClaudeConfig(), root)
    assert merged.after_create is None, (
        "merge_state_config now preserves root hooks — revisit the workspace "
        "call site, which only passes self.cfg.hooks because of this behaviour"
    )
    assert merged.timeout_ms == 60_000, "root timeout_ms is no longer discarded"


def test_the_scanner_sees_alias_style_imports(tmp_path):
    """Guards the guard: an earlier version skipped these silently.

    The module scan bailed on `if not imported` before it had looked for module
    aliases, so a file importing only `from . import workspace` was dropped
    entirely — while `test_the_seam_is_actually_covered` still passed on the
    other nine sites. A coverage tool with a blind spot reports the blind spot
    as covered.
    """
    (tmp_path / "workspace.py").write_text("def ensure_workspace(a, b, c): ...\n")
    (tmp_path / "caller.py").write_text(
        "from . import workspace\n"
        "def go():\n"
        "    return workspace.ensure_workspace(1, 2, 3)\n"
    )
    found = _cross_seam_calls(package_dir=tmp_path)
    assert [(c[2], c[3]) for c in found] == [("workspace", "ensure_workspace")], (
        f"alias-style call not discovered; got {found!r}"
    )


def test_the_scanner_ignores_unrelated_modules(tmp_path):
    """And does not invent call sites, which would make the count assertion lie."""
    (tmp_path / "other.py").write_text(
        "import json\ndef go():\n    return json.dumps({})\n"
    )
    assert _cross_seam_calls(package_dir=tmp_path) == []


def test_the_unwired_key_scan_still_covers_project_blocks(monkeypatch):
    """The registry is empty today; the mechanism still has to work.

    `UNWIRED_FORK_KEYS` is how a removed feature's config key gets refused
    rather than silently ignored — the shape that let `repos:` be dropped while
    the README told operators to copy that file. Both entries have since been
    re-applied, so this exercises the scan against a synthetic key instead of a
    live one, and specifically through `projects:`, which bypassed an earlier
    version of the scan.
    """
    from stokowski import config as config_mod  # noqa: PLC0415

    monkeypatch.setattr(config_mod, "UNWIRED_FORK_KEYS", {"widgets": "widgets are not wired"})

    assert config_mod._find_unwired_keys({"widgets": {}}) == ["widgets"], "top-level key missed"
    assert config_mod._find_unwired_keys(
        {"projects": [{"name": "a", "widgets": {}}]}
    ) == ["widgets"], "a key nested under projects: was not detected"
    assert config_mod._find_unwired_keys({"projects": [{"name": "a"}]}) == []
    assert config_mod._find_unwired_keys({}) == []
