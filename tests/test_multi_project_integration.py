"""End-to-end multi-project integration tests (Unit 8).

Extends the existing _StubLinearClient pattern to cover dispatch isolation,
hot-reload isolation, shared dispatch budget, and backward-compat single-file
behavior across the full orchestrator surface.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from stokowski.models import Issue
from stokowski.orchestrator import Orchestrator


_PROJECT_YAML = """\
tracker:
  project_slug: {slug}
  api_key: dummy_{slug}
polling:
  interval_ms: {poll}
workspace:
  root: /tmp/ws-{slug}
linear_states:
  todo: "Todo"
  active: "{active}"
  review: "Human Review"
  gate_approved: "Gate Approved"
  rework: "Rework"
  terminal:
    - Done
    - Canceled
states:
  work:
    type: agent
    prompt: p.md
  done:
    type: terminal
    linear_state: terminal
agent:
  max_concurrent_agents: {budget}
"""


def _write(tmp_path: Path, slug: str, *, poll: int = 15000, budget: int = 4, active: str = "In Progress") -> Path:
    path = tmp_path / f"workflow.{slug}.yaml"
    path.write_text(
        _PROJECT_YAML.format(slug=slug, poll=poll, budget=budget, active=active)
    )
    return path


class _StubClient:
    """Per-project stub LinearClient.

    `candidates_by_project` is passed in at orchestrator-construction time
    and consulted when fetch_candidate_issues runs. Tracks posted comments.
    """

    def __init__(self, slug: str, candidates: list[Issue]):
        self.slug = slug
        self._candidates = list(candidates)
        self.posted: dict[str, list[str]] = {}
        self.closed = False

    async def fetch_candidate_issues(self, project_slug: str, states: list[str]):
        return list(self._candidates)

    async def fetch_issues_by_states(self, project_slug: str, states: list[str]):
        return []

    async def fetch_issue_states_by_ids(self, ids: list[str]):
        return {}

    async def fetch_comments(self, issue_id: str):
        return []

    async def post_comment(self, issue_id: str, body: str) -> bool:
        self.posted.setdefault(issue_id, []).append(body)
        return True

    async def update_issue_state(self, issue_id: str, state: str) -> bool:
        return True

    async def close(self):
        self.closed = True


def _make_multi_orch(
    tmp_path: Path,
    candidates_by_project: dict[str, list[Issue]],
    *,
    budgets: dict[str, int] | None = None,
) -> tuple[Orchestrator, dict[str, _StubClient]]:
    """Build a two-project orchestrator with stubbed clients."""
    budgets = budgets or {}
    paths = []
    for slug in candidates_by_project:
        paths.append(_write(tmp_path, slug, budget=budgets.get(slug, 4)))

    orch = Orchestrator(paths)
    # Populate configs pre-load so we can wire stubs before any tick.
    orch._load_all_workflows()

    stubs: dict[str, _StubClient] = {}
    for slug, issues in candidates_by_project.items():
        stub = _StubClient(slug, issues)
        orch._linear_clients[slug] = stub  # type: ignore[assignment]
        stubs[slug] = stub
    return orch, stubs


def _issue(issue_id: str, identifier: str, slug_hint: str, state: str = "In Progress"):
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"from {slug_hint}",
        state=state,
        labels=[],
    )


def _run(coro):
    return asyncio.run(coro)


# ── Scenario 1: Dispatch isolation ─────────────────────────────────────────


def test_dispatch_isolation_two_projects(tmp_path):
    """Each project fetches with its own client; _issue_project bound per issue."""
    alpha_issues = [_issue("a-1", "ALPHA-1", "alpha")]
    beta_issues = [_issue("b-1", "BETA-1", "beta")]
    orch, stubs = _make_multi_orch(
        tmp_path, {"alpha": alpha_issues, "beta": beta_issues}
    )

    # Run one tick; dispatch is asynchronous (claude subprocess) but binding
    # should be observable immediately after fetch.
    async def tick_once():
        # Call _tick but swallow any downstream dispatch exceptions (we
        # don't have a real claude subprocess).
        try:
            await orch._tick()
        except Exception:
            pass

    _run(tick_once())

    assert orch._issue_project.get("a-1") == "alpha"
    assert orch._issue_project.get("b-1") == "beta"

    # Each project's candidates came from its own stub.
    assert stubs["alpha"]._candidates[0].id == "a-1"
    assert stubs["beta"]._candidates[0].id == "b-1"


# ── Scenario 2: Per-project state-name routing ──────────────────────────────


def test_per_project_linear_state_names(tmp_path):
    """Project A's 'active' differs from project B's — each issue is evaluated
    against its OWN project's state-name set via _cfg_for_issue."""
    a_path = tmp_path / "workflow.alpha.yaml"
    b_path = tmp_path / "workflow.beta.yaml"
    a_path.write_text(
        _PROJECT_YAML.format(
            slug="alpha", poll=15000, budget=4, active="In Progress"
        )
    )
    b_path.write_text(
        _PROJECT_YAML.format(
            slug="beta", poll=15000, budget=4, active="Working"
        )
    )
    orch = Orchestrator([a_path, b_path])
    orch._load_all_workflows()

    # Stamp an issue belonging to project B with state "Working" — it must
    # pass _is_eligible using project B's active_linear_states(), not A's.
    issue_b = Issue(id="b-1", identifier="BETA-1", title="t", state="Working")
    orch._issue_project["b-1"] = "beta"
    assert orch._is_eligible(issue_b) is True

    # An issue in project A with state "Working" (A's active is "In Progress")
    # must NOT pass eligibility.
    issue_a = Issue(id="a-1", identifier="ALPHA-1", title="t", state="Working")
    orch._issue_project["a-1"] = "alpha"
    assert orch._is_eligible(issue_a) is False


# ── Scenario 3: Hot-reload isolation ────────────────────────────────────────


def test_hot_reload_broken_file_preserves_healthy_project(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    errors = orch._load_all_workflows()
    assert errors == {}
    assert set(orch.configs.keys()) == {"alpha", "beta"}
    cfg_b_before = orch.configs["beta"]

    # Corrupt the beta file.
    b.write_text("::not valid yaml ::")
    errors2 = orch._load_all_workflows()
    # beta errored, alpha still loads.
    assert errors2
    # beta cfg preserved as last-known-good (file still exists, parse failed):
    assert orch.configs.get("beta") is cfg_b_before
    # alpha cfg present:
    assert orch.configs.get("alpha") is not None


def test_hot_reload_removed_file_evicted_when_no_inflight(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    orch._load_all_workflows()
    assert "beta" in orch.configs

    # Simulate removing beta from the configured path list.
    orch.workflow_paths = [a]
    # No in-flight work for beta → eviction.
    orch._load_all_workflows()
    assert "beta" not in orch.configs
    assert "beta" not in orch._config_paths


def test_hot_reload_removed_file_preserved_when_inflight(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    orch._load_all_workflows()

    # Simulate an in-flight dispatch for beta: running RunAttempt + binding.
    from stokowski.models import RunAttempt
    orch.running["b-issue-1"] = RunAttempt(
        issue_id="b-issue-1", issue_identifier="BETA-1"
    )
    orch._issue_project["b-issue-1"] = "beta"

    # Remove beta from the path list.
    orch.workflow_paths = [a]
    orch._load_all_workflows()

    # beta preserved because in-flight work exists.
    assert "beta" in orch.configs


# ── Scenario 4: Shared dispatch budget ─────────────────────────────────────


def test_shared_dispatch_budget_first_file_wins(tmp_path):
    """max_concurrent_agents is first-file-wins (primary config)."""
    a = _write(tmp_path, "alpha", budget=2)
    b = _write(tmp_path, "beta", budget=10)
    orch = Orchestrator([a, b])
    orch._load_all_workflows()
    # Primary (alpha) budget = 2 applies globally.
    assert orch._primary_cfg().agent.max_concurrent_agents == 2


def test_min_polling_interval_across_files(tmp_path):
    a = _write(tmp_path, "alpha", poll=30000)
    b = _write(tmp_path, "beta", poll=5000)
    orch = Orchestrator([a, b])
    orch._load_all_workflows()
    # Cached min wins — tightest interval applies.
    assert orch._polling_interval_ms == 5000


# ── Scenario 5: Dashboard snapshot project_slug ────────────────────────────


def test_get_state_snapshot_emits_project_slug(tmp_path):
    a = _write(tmp_path, "alpha")
    b = _write(tmp_path, "beta")
    orch = Orchestrator([a, b])
    orch._load_all_workflows()

    from stokowski.models import RunAttempt
    orch.running["a-issue-1"] = RunAttempt(
        issue_id="a-issue-1", issue_identifier="ALPHA-1"
    )
    orch._issue_project["a-issue-1"] = "alpha"
    orch._pending_gates["b-issue-1"] = "review"
    orch._issue_project["b-issue-1"] = "beta"

    snap = orch.get_state_snapshot()
    running_slugs = {r["project_slug"] for r in snap["running"]}
    gate_slugs = {g["project_slug"] for g in snap["gates"]}
    assert running_slugs == {"alpha"}
    assert gate_slugs == {"beta"}


# ── Scenario 6: Backward compat — single file ──────────────────────────────


def test_single_file_legacy_mode(tmp_path):
    """Single-file invocation produces a one-entry self.configs."""
    a = _write(tmp_path, "alpha")
    orch = Orchestrator(a)
    orch._load_all_workflows()
    assert len(orch.configs) == 1
    assert orch._primary_cfg().tracker.project_slug == "alpha"


# ── Scenario 7: Duplicate project_slug ─────────────────────────────────────


def test_duplicate_project_slug_produces_error(tmp_path):
    a_path = tmp_path / "workflow.one.yaml"
    b_path = tmp_path / "workflow.two.yaml"
    a_path.write_text(
        _PROJECT_YAML.format(slug="shared", poll=15000, budget=4, active="In Progress")
    )
    b_path.write_text(
        _PROJECT_YAML.format(slug="shared", poll=15000, budget=4, active="In Progress")
    )
    orch = Orchestrator([a_path, b_path])
    errors = orch._load_all_workflows()
    assert "shared" in errors
    assert any("duplicate" in e for e in errors["shared"])


# ── Scenario 8: Workflow dir resolution per project ────────────────────────


def test_workflow_dir_for_issue_per_project(tmp_path):
    """Prompt files resolve against the owning project's directory."""
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = a_dir / "workflow.alpha.yaml"
    b = b_dir / "workflow.beta.yaml"
    a.write_text(_PROJECT_YAML.format(slug="alpha", poll=15000, budget=4, active="In Progress"))
    b.write_text(_PROJECT_YAML.format(slug="beta", poll=15000, budget=4, active="In Progress"))

    orch = Orchestrator([a, b])
    orch._load_all_workflows()

    orch._issue_project["a-1"] = "alpha"
    orch._issue_project["b-1"] = "beta"

    assert orch._workflow_dir_for_issue("a-1") == a_dir
    assert orch._workflow_dir_for_issue("b-1") == b_dir
