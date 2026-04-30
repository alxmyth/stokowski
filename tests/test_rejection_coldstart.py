"""Tests for the async rejection pre-pass and cold-start repo recovery
(Unit 7 orchestrator-side).

These tests exercise Orchestrator._process_rejections and
Orchestrator._resolve_repo_for_coldstart with stubbed LinearClient
interactions (no real network calls).
"""

from __future__ import annotations

import asyncio

import pytest

from stokowski.models import Issue
from stokowski.orchestrator import Orchestrator
import json as _json

from stokowski.tracking import (
    make_migrated_comment,
    make_rejection_comment,
    REJECTED_PATTERN,
    MIGRATED_PATTERN,
)


def _legacy_state_comment(
    state: str, run: int, workflow: str | None = None,
    repo: str | None = None,
) -> str:
    """Fabricate a pre-attachment-era ``stokowski:state`` comment body.

    Production code no longer emits these — state lives in the Linear
    attachment. Tests still need to seed legacy thread shapes to verify
    ``parse_latest_tracking`` migration fallback behaviour.
    """
    payload: dict[str, object] = {"state": state, "run": run}
    if workflow is not None:
        payload["workflow"] = workflow
    if repo is not None:
        payload["repo"] = repo
    return f"<!-- stokowski:state {_json.dumps(payload)} -->"


def _run(coro):
    return asyncio.run(coro)


class _StubLinearClient:
    """In-memory stand-in for LinearClient used by rejection + cold-start tests.

    Tracks posted comments per issue_id and supports preloading a comment
    thread to be returned from fetch_comments.
    """

    def __init__(self):
        self.posted: dict[str, list[str]] = {}
        self.preloaded: dict[str, list[dict]] = {}

    async def fetch_comments(self, issue_id: str) -> list[dict]:
        # Return preloaded thread plus any posted-during-test comments
        preloaded = list(self.preloaded.get(issue_id, []))
        posted_now = [
            {"body": body, "createdAt": "2026-04-20T00:00:00Z"}
            for body in self.posted.get(issue_id, [])
        ]
        return preloaded + posted_now

    async def post_comment(self, issue_id: str, body: str) -> bool:
        self.posted.setdefault(issue_id, []).append(body)
        return True

    async def close(self):
        pass


def _make_orch(tmp_path):
    """Standard multi-repo orchestrator fixture."""
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


def _issue(labels: list[str], issue_id: str = "iid-1"):
    return Issue(id=issue_id, identifier="SMI-1", title="t", labels=labels)


# ── Rejection pre-pass ──────────────────────────────────────────────────────


def test_rejection_prepass_single_repo_label_no_marker(tmp_path):
    """One repo:* label → no rejection, dispatch proceeds."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    _run(orch._process_rejections([_issue(["repo:api"])]))

    assert "iid-1" not in orch._rejected_issues
    assert orch._linear.posted == {}


def test_rejection_prepass_dual_repo_labels_marks_rejected(tmp_path):
    """Two repo:* labels → marker added, rejection comment posted."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    _run(orch._process_rejections([_issue(["repo:api", "repo:web"])]))

    assert "iid-1" in orch._rejected_issues
    assert len(orch._linear.posted["iid-1"]) == 1
    assert REJECTED_PATTERN.search(orch._linear.posted["iid-1"][0])


def test_rejection_prepass_dedup_across_ticks(tmp_path):
    """Second tick with same dual labels posts no new rejection comment."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()
    issue = _issue(["repo:api", "repo:web"])

    _run(orch._process_rejections([issue]))
    first_count = len(orch._linear.posted["iid-1"])

    # Simulate the posted comment now existing in the comment thread
    orch._linear.preloaded["iid-1"] = [
        {"body": c, "createdAt": "2026-04-20T00:00:00Z"}
        for c in orch._linear.posted["iid-1"]
    ]
    orch._linear.posted = {}  # reset to count new posts

    # Clear the in-memory marker (as if orchestrator restarted)
    orch._rejected_issues.discard("iid-1")

    _run(orch._process_rejections([issue]))

    # has_pending_rejection should have caught the existing sentinel →
    # no new comment posted. Marker is re-populated.
    assert "iid-1" not in orch._linear.posted  # no new post
    assert "iid-1" in orch._rejected_issues


def test_rejection_prepass_label_change_invalidates_marker(tmp_path):
    """When labels change to a new dual-label set, the sentinel doesn't match
    and a fresh rejection fires.

    Regression test for COR-001: the invalidation now compares against
    _prev_issue_labels (captured BEFORE _last_issues is updated each tick),
    not _last_issues (which would always reflect the current tick and make
    the comparison a no-op).
    """
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    # Tick 1: dual labels {api, web}
    _run(orch._process_rejections([_issue(["repo:api", "repo:web"])]))

    # Preload existing comment into the thread
    orch._linear.preloaded["iid-1"] = [
        {"body": c, "createdAt": "2026-04-20T00:00:00Z"}
        for c in orch._linear.posted.get("iid-1", [])
    ]
    orch._linear.posted = {}

    # Simulate the _tick snapshot discipline: record the PRIOR tick's labels
    # BEFORE the new-tick update. This matches what _tick does at its
    # "Snapshot prior-tick labels BEFORE updating _last_issues" block.
    orch._prev_issue_labels["iid-1"] = sorted(
        l.lower() for l in ["repo:api", "repo:web"]
    )

    # Tick 2: labels change to {api, mobile}
    new_issue = _issue(["repo:api", "repo:mobile"])
    _run(orch._process_rejections([new_issue]))

    # Stale marker discarded, new rejection posted for the new label set
    assert "iid-1" in orch._rejected_issues
    assert "iid-1" in orch._linear.posted
    assert len(orch._linear.posted["iid-1"]) == 1


def test_rejection_prepass_coro1_covers_full_tick_prior_labels_flow(tmp_path):
    """End-to-end regression test for COR-001: exercise _process_rejections
    through two simulated ticks that use the same _prev_issue_labels snapshot
    discipline as the real _tick loop.

    Without the COR-001 fix, tick 2 would incorrectly keep the issue
    rejected because the stale-label invalidation always compared the
    current issue against itself.
    """
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    def simulate_tick(issues):
        """Mini-_tick that mirrors the prior-labels snapshot discipline."""
        for i in issues:
            prior = orch._last_issues.get(i.id)
            if prior is not None:
                orch._prev_issue_labels[i.id] = sorted(
                    l.lower() for l in prior.labels
                )
            else:
                orch._prev_issue_labels.pop(i.id, None)
        for i in issues:
            orch._last_issues[i.id] = i
        _run(orch._process_rejections(issues))

    # Tick 1: dual labels → rejection posted, marker set
    issue_t1 = _issue(["repo:api", "repo:web"])
    simulate_tick([issue_t1])
    assert "iid-1" in orch._rejected_issues
    t1_posts = list(orch._linear.posted.get("iid-1", []))
    assert len(t1_posts) == 1

    # Preload the first post into the comment thread and reset posted tracker
    orch._linear.preloaded["iid-1"] = [
        {"body": t1_posts[0], "createdAt": "2026-04-20T00:00:00Z"}
    ]
    orch._linear.posted = {}

    # Tick 2: labels change {api, web} → {api, mobile}. Without the COR-001
    # fix, prior_labels would equal current_labels (both = {api, mobile}
    # because _last_issues was already updated), the invalidation would
    # not fire, the marker would stay, and no new rejection would post.
    issue_t2 = _issue(["repo:api", "repo:mobile"])
    simulate_tick([issue_t2])

    assert "iid-1" in orch._rejected_issues, (
        "Marker should be re-set for the new label combination"
    )
    assert "iid-1" in orch._linear.posted, (
        "New rejection comment should fire for the new label set "
        "(regression: COR-001 made this a silent stall)"
    )


def test_rejection_prepass_labels_fixed_clears_marker(tmp_path):
    """Operator removes one of two labels → marker discarded, dispatch OK."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    # Start rejected
    orch._rejected_issues.add("iid-1")

    # Tick sees only one repo label now
    _run(orch._process_rejections([_issue(["repo:api"])]))

    assert "iid-1" not in orch._rejected_issues


def test_rejection_prepass_no_repo_labels_no_marker(tmp_path):
    """Zero repo:* labels is fine — dispatch will route via default."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    _run(orch._process_rejections([_issue(["bug", "p1"])]))

    assert "iid-1" not in orch._rejected_issues
    assert orch._linear.posted == {}


def test_rejection_prepass_triage_origin_detection(tmp_path):
    """Dual labels on a ticket whose most recent state comment was from
    a triage workflow → rejection reason is triage_multi_repo."""
    # Rebuild orch with a triage workflow defined
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
  intake:
    label: "workflow:intake"
    triage: true
    path: [work, done]

repos:
  api:
    label: "repo:api"
    clone_url: "git@github.com:org/api.git"
  web:
    label: "repo:web"
    clone_url: "git@github.com:org/web.git"
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors

    stub = _StubLinearClient()
    orch._linear = stub

    # Preload a state comment from the triage workflow (the ticket came out
    # of triage, then triage labeled it with two repos — human or bug).
    # Use a legacy comment shape because ``_process_rejections`` consults
    # ``parse_latest_tracking`` over the comment thread for triage-origin
    # detection.
    triage_state_comment = _legacy_state_comment(
        state="work", run=1, workflow="intake", repo="_default",
    )
    stub.preloaded["iid-1"] = [
        {"body": triage_state_comment, "createdAt": "2026-04-20T00:00:00Z"}
    ]

    _run(orch._process_rejections([_issue(["repo:api", "repo:web"])]))

    # Rejection should be tagged as triage-originated
    assert "iid-1" in orch._rejected_issues
    posted = stub.posted["iid-1"][0]
    assert "triage_multi_repo" in posted
    assert "Triage applied two" in posted


# ── Cold-start recovery ─────────────────────────────────────────────────────


def test_coldstart_cache_already_populated_noop(tmp_path):
    """If _issue_repo already has the entry, cold-start is a no-op."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()
    orch._issue_repo["iid-1"] = "api"

    issue = _issue(["repo:api"])
    _run(orch._resolve_repo_for_coldstart(issue, tracking=None, comments=[]))

    # Unchanged
    assert orch._issue_repo["iid-1"] == "api"
    assert orch._linear.posted == {}


def test_coldstart_tracking_repo_field_restored(tmp_path):
    """Tracking comment with repo field → cache populated, no migration post."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    issue = _issue(["repo:api"])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": "api"}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    assert orch._issue_repo["iid-1"] == "api"
    assert orch._linear.posted == {}


def test_coldstart_tracking_repo_missing_falls_back_to_labels(tmp_path):
    """Pre-migration tracking (no repo field) → resolve via labels, no migrated
    post because we resolved to a non-default repo."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    issue = _issue(["repo:api"])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": None}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    assert orch._issue_repo["iid-1"] == "api"
    # No migrated comment — fell back to an explicit repo, not _default
    assert orch._linear.posted == {}


def test_coldstart_tracking_repo_missing_defaults_to_default_posts_migrated(tmp_path):
    """Pre-migration tracking + no repo label → _default + migrated notice."""
    # Swap to a config where synthesized _default would apply (no repos: section)
    legacy_wf = tmp_path / "legacy.yaml"
    legacy_wf.write_text(
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
"""
    )
    orch = Orchestrator(str(legacy_wf))
    errors = orch._load_workflow()
    assert not errors
    orch._linear = _StubLinearClient()

    issue = _issue([])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": None}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    assert orch._issue_repo["iid-1"] == "_default"
    # Migrated comment posted
    assert "iid-1" in orch._linear.posted
    assert MIGRATED_PATTERN.search(orch._linear.posted["iid-1"][0])
    assert "iid-1" in orch._migrated_issues


def test_coldstart_migrated_posted_only_once(tmp_path):
    """Repeated cold-start calls on the same issue don't spam migrated."""
    legacy_wf = tmp_path / "legacy.yaml"
    legacy_wf.write_text(
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
"""
    )
    orch = Orchestrator(str(legacy_wf))
    orch._load_workflow()
    orch._linear = _StubLinearClient()

    issue = _issue([])
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": None}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))
    first = list(orch._linear.posted.get("iid-1", []))

    # Clear cache to simulate a fresh call that triggers cold-start again
    orch._issue_repo.clear()
    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))
    second = list(orch._linear.posted.get("iid-1", []))

    assert len(second) == len(first)  # no additional migration posts


def test_coldstart_tracking_points_to_removed_repo_falls_back(tmp_path):
    """Tracking names a repo that no longer exists in config → label resolve."""
    orch = _make_orch(tmp_path)
    orch._linear = _StubLinearClient()

    issue = _issue(["repo:api"])
    # Tracking names a repo that doesn't exist (hot-reload removed it)
    tracking = {"type": "state", "state": "work", "run": 1, "workflow": "standard", "repo": "removed-repo"}

    _run(orch._resolve_repo_for_coldstart(issue, tracking, comments=[]))

    # Falls back to label resolution
    assert orch._issue_repo["iid-1"] == "api"


# ── Cleanup parity (Unit 2 meta-test extended) ──────────────────────────────


def test_cleanup_removes_rejected_and_migrated_markers(tmp_path):
    """_cleanup_issue_state removes the new _rejected_issues and
    _migrated_issues entries (parity with __init__)."""
    orch = _make_orch(tmp_path)
    issue_id = "cleanup-test"
    orch._rejected_issues.add(issue_id)
    orch._migrated_issues.add(issue_id)
    orch._config_blocked.add(issue_id)
    orch._rejection_fetch_pending.add(issue_id)

    orch._cleanup_issue_state(issue_id)

    assert issue_id not in orch._rejected_issues
    assert issue_id not in orch._migrated_issues
    assert issue_id not in orch._config_blocked
    assert issue_id not in orch._rejection_fetch_pending


# ── R10 integration: _rejected_issues -> _is_eligible -> blocked dispatch ──


def test_is_eligible_rejects_when_in_rejected_issues_set(tmp_path):
    """T-02 integration path: _is_eligible must return False for any issue
    in _rejected_issues. This was untested before — a regression that
    removed the guard in _is_eligible would pass all other rejection
    tests because they only verify _rejected_issues is populated, not
    that it actually blocks dispatch.
    """
    orch = _make_orch(tmp_path)
    # Build an Issue that would otherwise be eligible: active state,
    # required fields present, no blockers.
    issue = _issue(["repo:api"])
    issue.state = "In Progress"  # one of cfg.active_linear_states()

    # Sanity: eligible when NOT in _rejected_issues
    assert orch._is_eligible(issue) is True

    # Add to _rejected_issues — now must be ineligible
    orch._rejected_issues.add(issue.id)
    assert orch._is_eligible(issue) is False

    # Discard — back to eligible
    orch._rejected_issues.discard(issue.id)
    assert orch._is_eligible(issue) is True


def test_is_eligible_rejects_when_in_config_blocked_set(tmp_path):
    """Companion check for the config_error block: _is_eligible must
    return False when an issue is in _config_blocked (hook template typo).
    """
    orch = _make_orch(tmp_path)
    issue = _issue(["repo:api"])
    issue.state = "In Progress"

    assert orch._is_eligible(issue) is True

    orch._config_blocked.add(issue.id)
    assert orch._is_eligible(issue) is False

    orch._config_blocked.discard(issue.id)
    assert orch._is_eligible(issue) is True


def test_rejection_prepass_fails_closed_on_fetch_error(tmp_path):
    """ADV-003 regression: when fetch_comments fails for a dual-labeled
    issue, the issue MUST NOT be dispatched. Failing closed prevents
    arbitrary first-wins repo routing from being committed to the
    tracking thread during a transient Linear outage.

    The fix: mark rejected AND flag for retry in _rejection_fetch_pending
    so the next tick re-attempts the fetch regardless of label changes.
    """

    class _FailingFetchClient(_StubLinearClient):
        async def fetch_comments(self, issue_id: str) -> list[dict]:
            raise RuntimeError("Linear API unavailable")

    orch = _make_orch(tmp_path)
    orch._linear = _FailingFetchClient()

    dual_label_issue = _issue(["repo:api", "repo:web"])
    _run(orch._process_rejections([dual_label_issue]))

    # Must be marked rejected (block this tick)
    assert "iid-1" in orch._rejected_issues
    # And flagged for retry on next tick
    assert "iid-1" in orch._rejection_fetch_pending
    # _is_eligible must agree — dispatch is blocked
    dual_label_issue.state = "In Progress"
    assert orch._is_eligible(dual_label_issue) is False


def test_rejection_fetch_failure_retries_on_next_tick(tmp_path):
    """Companion to ADV-003: on the next tick, _rejection_fetch_pending
    clears the marker so fetch is re-attempted. If fetch succeeds and
    there's no prior sentinel, the normal path runs."""

    class _TransientFailClient(_StubLinearClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def fetch_comments(self, issue_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return await super().fetch_comments(issue_id)

    orch = _make_orch(tmp_path)
    orch._linear = _TransientFailClient()

    # Tick 1: fetch fails → pessimistic rejection
    dual_label_issue = _issue(["repo:api", "repo:web"])
    _run(orch._process_rejections([dual_label_issue]))
    assert "iid-1" in orch._rejected_issues
    assert "iid-1" in orch._rejection_fetch_pending

    # Tick 2: same labels, fetch succeeds — retry is honored, pessimistic
    # marker is cleared at the top of the loop iteration, fetch runs,
    # sentinel posted, issue re-added to _rejected_issues (confirmed now).
    _run(orch._process_rejections([dual_label_issue]))
    assert "iid-1" in orch._rejected_issues
    # The pessimistic flag is cleared (fetch succeeded)
    assert "iid-1" not in orch._rejection_fetch_pending
