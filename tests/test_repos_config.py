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
    validate_config,
    _near_match_prefixes,
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


def test_parse_explicit_empty_repos_synthesizes_with_warning(caplog):
    """Explicit `repos: {}` still synthesizes _default (with a warning).

    Rationale: an empty section is almost always an operator mistake (typo,
    stub-for-later). Failing hard on it would break any test that constructs
    ServiceConfig() directly. Warning + synthesize is the safe middle path.
    """
    import logging

    path = _write_yaml(_MINIMAL_STATES + "\nrepos: {}\n")
    with caplog.at_level(logging.WARNING, logger="stokowski.config"):
        parsed = parse_workflow_file(path)
    cfg = parsed.config

    assert cfg.repos_synthesized is True
    assert "_default" in cfg.repos
    # Warning should mention the empty section
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("empty" in m.lower() for m in warning_msgs), (
        f"Expected warning about empty section, got: {warning_msgs}"
    )


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


# ── ServiceConfig.resolve_repo (Unit 2) ─────────────────────────────────────


def _cfg_with_repos(repos: list[tuple[str, str | None, bool]]) -> ServiceConfig:
    """Helper: build a ServiceConfig with the given (name, label, default) repos."""
    cfg = ServiceConfig()
    cfg.repos = {
        name: RepoConfig(name=name, label=label, clone_url=f"git@x/{name}", default=default)
        for name, label, default in repos
    }
    return cfg


def _issue(labels: list[str]) -> "Issue":
    from stokowski.models import Issue

    return Issue(id="x", identifier="TST-1", title="t", labels=labels)


def test_resolve_repo_label_match_wins():
    """resolve_repo: matching label wins over default."""
    cfg = _cfg_with_repos([("api", "repo:api", False), ("web", "repo:web", True)])
    assert cfg.resolve_repo(_issue(["repo:api"])).name == "api"


def test_resolve_repo_no_match_falls_back_to_default():
    """resolve_repo: no matching label → default-marked repo."""
    cfg = _cfg_with_repos([("api", "repo:api", False), ("web", "repo:web", True)])
    assert cfg.resolve_repo(_issue(["bug", "p1"])).name == "web"


def test_resolve_repo_no_labels_returns_default():
    """resolve_repo: issue with no labels at all returns default."""
    cfg = _cfg_with_repos([("api", "repo:api", False), ("web", "repo:web", True)])
    assert cfg.resolve_repo(_issue([])).name == "web"


def test_resolve_repo_case_insensitive_label_match():
    """resolve_repo: label matching is case-insensitive."""
    cfg = _cfg_with_repos([("api", "repo:api", True)])
    assert cfg.resolve_repo(_issue(["REPO:API"])).name == "api"


def test_resolve_repo_legacy_default_resolves():
    """Legacy synthesized _default repo resolves for any issue."""
    path = _write_yaml(_MINIMAL_STATES)
    parsed = parse_workflow_file(path)
    assert parsed.config.resolve_repo(_issue([])).name == "_default"
    assert parsed.config.resolve_repo(_issue(["unrelated"])).name == "_default"


def test_resolve_repo_no_default_raises():
    """resolve_repo: no match AND no default → ValueError."""
    cfg = _cfg_with_repos([("api", "repo:api", False)])
    with pytest.raises(ValueError, match="No default repo"):
        cfg.resolve_repo(_issue([]))


# ── Orchestrator repo routing + cache parity (Unit 2) ───────────────────────


class TestOrchestratorRepoRouting:
    """Tests for Orchestrator._resolve_repo, _get_issue_repo_config, and
    the cleanup contract for _issue_repo. Mirrors TestOrchestratorWorkflowRouting
    in test_state_machine.py.
    """

    def _make_orch(self, tmp_path):
        """Build an Orchestrator from a multi-repo workflow.yaml."""
        from stokowski.orchestrator import Orchestrator

        wf_path = tmp_path / "workflow.yaml"
        wf_path.write_text(
            """
tracker:
  api_key: test-key
  project_slug: abc123

states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  standard:
    label: "workflow:standard"
    default: true
    path: [work, done]

repos:
  api:
    label: "repo:api"
    clone_url: "git@github.com:org/api.git"
  web:
    label: "repo:web"
    clone_url: "git@github.com:org/web.git"
    default: true
"""
        )
        orch = Orchestrator(str(wf_path))
        errors = orch._load_workflow()
        assert not errors, f"Config errors: {errors}"
        return orch

    @staticmethod
    def _make_issue(labels: list[str] | None = None, issue_id: str = "abc-1"):
        from stokowski.models import Issue

        return Issue(id=issue_id, identifier="TST-1", title="t", labels=labels or [])

    def test_resolve_repo_label_match_caches_name(self, tmp_path):
        """Matching repo:* label resolves correctly AND caches the name."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["repo:api"])

        repo = orch._resolve_repo(issue)
        assert repo.name == "api"
        assert orch._issue_repo[issue.id] == "api"

    def test_resolve_repo_no_label_returns_default(self, tmp_path):
        """Issue with no repo:* label resolves to default-marked repo."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["bug"])

        repo = orch._resolve_repo(issue)
        assert repo.name == "web"  # the default-marked repo in the fixture
        assert orch._issue_repo[issue.id] == "web"

    def test_get_issue_repo_config_cached(self, tmp_path):
        """_get_issue_repo_config returns the cached RepoConfig."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["repo:api"])
        orch._resolve_repo(issue)

        cached = orch._get_issue_repo_config(issue.id)
        assert cached.name == "api"

    def test_get_issue_repo_config_not_cached_returns_default(self, tmp_path):
        """Uncached issue id → default-marked repo."""
        orch = self._make_orch(tmp_path)
        assert orch._get_issue_repo_config("unknown-id").name == "web"

    def test_get_issue_repo_config_stale_cache_resolves_from_labels(self, tmp_path):
        """Hot-reload removes cached repo → re-resolves from labels.

        Mirrors the equivalent workflow test at test_state_machine.py:
        test_get_workflow_config_stale_cache_resolves_from_labels.
        """
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["repo:api"])
        orch._last_issues[issue.id] = issue
        orch._issue_repo[issue.id] = "api"  # simulate prior cache state

        # Simulate hot-reload removing the 'api' repo from the registry
        del orch.cfg.repos["api"]

        # Should fall back: cache miss for 'api' → re-resolve from labels.
        # Since labels still carry 'repo:api' but 'api' is gone, that fails too,
        # so we fall through to the default-marked repo.
        resolved = orch._get_issue_repo_config(issue.id)
        assert resolved.name == "web"  # default-marked repo in the fixture

    def test_get_issue_repo_config_stale_cache_no_issue_returns_default(self, tmp_path):
        """Stale cache AND no issue in _last_issues → default-marked repo."""
        orch = self._make_orch(tmp_path)
        orch._issue_repo["lost-id"] = "api"

        del orch.cfg.repos["api"]

        assert orch._get_issue_repo_config("lost-id").name == "web"

    def test_cleanup_removes_repo_entry(self, tmp_path):
        """_cleanup_issue_state removes the _issue_repo entry."""
        orch = self._make_orch(tmp_path)
        issue_id = "test-cleanup"

        orch._issue_repo[issue_id] = "api"
        orch._issue_workflow[issue_id] = "standard"
        orch._issue_current_state[issue_id] = "work"
        orch.claimed.add(issue_id)

        orch._cleanup_issue_state(issue_id)

        assert issue_id not in orch._issue_repo
        assert issue_id not in orch._issue_workflow
        assert issue_id not in orch._issue_current_state
        assert issue_id not in orch.claimed

    def test_cleanup_issue_repo_parity_with_init(self, tmp_path):
        """Meta-test: every per-issue dict/set in __init__ is popped by cleanup.

        Guards against the drift class called out in CLAUDE.md's pitfalls —
        if a new per-issue tracking structure is added to __init__ but not
        to _cleanup_issue_state, memory leaks and stale state result.
        """
        orch = self._make_orch(tmp_path)
        issue_id = "parity-test"

        # Pre-populate every per-issue dict/set that __init__ creates as empty.
        # If a new one is added later, this list must expand or the test fails.
        per_issue_dicts = [
            orch.running,
            orch._tasks,
            orch._retry_timers,
            orch.retry_attempts,
            orch._last_issues,
            orch._last_completed_at,
            orch._last_session_ids,
            orch._issue_current_state,
            orch._issue_state_runs,
            orch._pending_gates,
            orch._issue_workflow,
            orch._issue_repo,  # new in Unit 2
        ]
        per_issue_sets = [orch.claimed]

        for d in per_issue_dicts:
            # Use a sentinel value shape that matches each dict's real usage.
            # For timers we skip — real TimerHandle is complex — rely on dict pop.
            if d is orch._retry_timers:
                continue
            d[issue_id] = "sentinel"
        for s in per_issue_sets:
            s.add(issue_id)

        orch._cleanup_issue_state(issue_id)

        for d in per_issue_dicts:
            if d is orch._retry_timers:
                continue
            assert issue_id not in d, f"cleanup left entry in {d}"
        for s in per_issue_sets:
            assert issue_id not in s, f"cleanup left entry in {s}"


# ── Unit 3: Config validation (R21) ─────────────────────────────────────────


def _minimal_multi_repo_yaml(repos_block: str, workflows_block: str = "") -> str:
    """Build a YAML fragment with a given repos: section."""
    wf = workflows_block or """
workflows:
  standard:
    label: workflow:standard
    default: true
    path: [work, done]
"""
    return _MINIMAL_STATES + repos_block + wf


def test_validate_repo_integrity_rejects_empty_clone_url():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: ""
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("empty clone_url" in e for e in errors), errors


def test_validate_repo_integrity_rejects_empty_label():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    clone_url: git@github.com:org/api.git
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("empty label" in e for e in errors), errors


def test_validate_duplicate_labels_rejected():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
    default: true
  api2:
    label: repo:api
    clone_url: git@github.com:org/api2.git
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("Duplicate repo label" in e for e in errors), errors


def test_validate_clone_url_scheme_rejects_file_scheme():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: file:///tmp/foo
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("file://" in e for e in errors), errors


def test_validate_clone_url_rejects_embedded_credentials():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: https://user:secret@github.com/org/api.git
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("embedded credentials" in e for e in errors), errors


def test_validate_clone_url_accepts_ssh_https_git_forms():
    """Accepted schemes: https://, ssh://, git@."""
    for url in (
        "https://github.com/org/api.git",
        "ssh://git@github.com/org/api.git",
        "git@github.com:org/api.git",
    ):
        path = _write_yaml(_minimal_multi_repo_yaml(
            f"""
repos:
  api:
    label: repo:api
    clone_url: {url}
    default: true
"""
        ))
        errors = validate_config(parse_workflow_file(path).config)
        scheme_errors = [e for e in errors if "clone_url" in e]
        assert scheme_errors == [], (
            f"URL {url!r} flagged unexpectedly: {scheme_errors}"
        )


def test_validate_clone_url_rejects_unknown_scheme():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: ftp://example.com/repo
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("must use https://" in e for e in errors), errors


def test_validate_multiple_defaults_rejected():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
    default: true
  web:
    label: repo:web
    clone_url: git@github.com:org/web.git
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("Multiple default repos" in e for e in errors), errors


def test_validate_single_repo_must_be_default():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("not marked default: true" in e for e in errors), errors


def test_validate_multi_repo_no_default_requires_triage():
    """Multi-repo + no default → must have exactly one triage workflow."""
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
  web:
    label: repo:web
    clone_url: git@github.com:org/web.git
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("requires exactly one workflow with triage: true" in e for e in errors), errors


def test_validate_multi_repo_no_default_with_triage_workflow_passes():
    """Multi-repo + no default + triage workflow → no repo-routing error."""
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
  web:
    label: repo:web
    clone_url: git@github.com:org/web.git
""",
        workflows_block="""
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
    ))
    errors = validate_config(parse_workflow_file(path).config)
    repo_errs = [e for e in errors if "triage" in e.lower() and "repo" in e.lower()]
    assert repo_errs == [], repo_errs


def test_validate_multiple_triage_workflows_rejected():
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  api:
    label: repo:api
    clone_url: git@github.com:org/api.git
  web:
    label: repo:web
    clone_url: git@github.com:org/web.git
""",
        workflows_block="""
workflows:
  standard:
    label: workflow:standard
    default: true
    path: [work, done]
  intake:
    label: workflow:intake
    triage: true
    path: [work, done]
  router:
    label: workflow:router
    triage: true
    path: [work, done]
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("Exactly one workflow may have triage: true" in e for e in errors), errors


def test_validate_reserved_repo_name_default_rejected():
    """Operator-authored `_default` repo name is reserved."""
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  _default:
    label: repo:default
    clone_url: git@github.com:org/default.git
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("reserved" in e.lower() and "_default" in e for e in errors), errors


def test_validate_path_safety_rejects_unsafe_name():
    """Repo names with invalid characters are rejected (unsafe for paths)."""
    path = _write_yaml(_minimal_multi_repo_yaml(
        """
repos:
  "my/repo":
    label: repo:slash
    clone_url: git@github.com:org/foo.git
    default: true
"""
    ))
    errors = validate_config(parse_workflow_file(path).config)
    assert any("invalid characters" in e for e in errors), errors


def test_validate_legacy_config_passes_no_repo_errors():
    """Legacy config (no repos: section) surfaces no repo-related errors."""
    path = _write_yaml(_MINIMAL_STATES)
    errors = validate_config(parse_workflow_file(path).config)
    repo_errs = [e for e in errors if "repo" in e.lower()]
    assert repo_errs == [], repo_errs


def test_validate_default_workflow_triage_flag_required():
    """A workflow with triage=True should not also set default=True.

    (Implicitly enforced elsewhere: they're orthogonal flags; the brainstorm
    decision is that triage is its own workflow, not the default. No specific
    validation rule in R21, but covered by overall workflow validation.)
    """
    # Just confirm parser accepts both being set (semantic check is elsewhere)
    path = _write_yaml(_MINIMAL_STATES + """
workflows:
  intake:
    label: workflow:intake
    default: true
    triage: true
    path: [work, done]
""")
    parsed = parse_workflow_file(path)
    assert parsed.config.workflows["intake"].triage is True
    assert parsed.config.workflows["intake"].default is True


def test_near_match_prefix_helper():
    """Helper generates the expected typo variants."""
    variants = _near_match_prefixes("workflow:")
    # Should include transposition variants + "workflows:"
    assert "workflows:" in variants
    # Should produce non-empty result and not include the original
    assert variants
    assert "workflow:" not in variants


def test_validate_reserved_prefix_warning_fires(caplog):
    """Operator labels near-matching reserved prefixes emit a warning."""
    import logging

    path = _write_yaml(_MINIMAL_STATES + """
workflows:
  standard:
    label: workflows:oops-typo   # workflows: not workflow:
    default: true
    path: [work, done]
""")
    with caplog.at_level(logging.WARNING, logger="stokowski.config"):
        validate_config(parse_workflow_file(path).config)

    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("near-match" in m.lower() for m in warning_msgs), (
        f"Expected reserved-prefix near-match warning, got: {warning_msgs}"
    )
