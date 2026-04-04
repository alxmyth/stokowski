# Attachment-Based State Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers2:subagent-driven-development (recommended) or superpowers2:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move machine-readable tracking state from Linear comments to Linear Attachment metadata, keeping comments human-readable only.

**Architecture:** Each issue gets a single Stokowski-owned attachment (`stokowski://state/{identifier}`) whose `metadata` JSON field carries the full state machine position. Comments fire only when they carry information the Linear ticket state doesn't convey (rework, escalation, evaluation findings). Crash recovery reads the attachment first, falls back to legacy comment scanning during migration.

**Tech Stack:** Python 3.10+, asyncio, httpx, Linear GraphQL API (attachments)

**User Verification:** NO

---

### Task 1: Linear API attachment methods

**Goal:** Add attachment CRUD (upsert, fetch, delete) to `LinearClient`.

**Files:**
- Modify: `stokowski/linear.py`
- Create: `tests/test_attachment_api.py`

**Acceptance Criteria:**
- [ ] `upsert_stokowski_attachment()` sends `attachmentCreate` mutation with correct URL, title, subtitle, metadata
- [ ] `fetch_stokowski_attachment()` queries `attachmentsForURL` and returns metadata dict or None
- [ ] `delete_stokowski_attachment()` finds attachment by URL and deletes it
- [ ] All three handle errors gracefully (return False/None, log warning)

**Verify:** `cd /Users/amsmith/code/personal/stokowski && .venv/bin/python -m pytest tests/test_attachment_api.py -v`

**Steps:**

- [ ] **Step 1: Write tests for the three attachment methods**

Create `tests/test_attachment_api.py`:

```python
"""Tests for Linear attachment API methods."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from stokowski.linear import LinearClient


@pytest.fixture
def client():
    return LinearClient(
        endpoint="https://api.linear.app/graphql",
        api_key="test-key",
    )


class TestUpsertAttachment:
    @pytest.mark.asyncio
    async def test_upsert_sends_correct_mutation(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentCreate": {"success": True, "attachment": {"id": "att-1"}}
        })
        result = await client.upsert_stokowski_attachment(
            issue_id="issue-1",
            identifier="ALX-9",
            metadata={"state": "implement", "run": 1},
            subtitle="implement · run 1",
        )
        assert result is True
        call_args = client._graphql.call_args
        variables = call_args[0][1]
        assert variables["issueId"] == "issue-1"
        assert variables["url"] == "stokowski://state/ALX-9"
        assert variables["title"] == "Stokowski"
        assert variables["subtitle"] == "implement · run 1"
        assert variables["metadata"] == {"state": "implement", "run": 1}

    @pytest.mark.asyncio
    async def test_upsert_returns_false_on_error(self, client):
        client._graphql = AsyncMock(side_effect=RuntimeError("API error"))
        result = await client.upsert_stokowski_attachment(
            issue_id="issue-1", identifier="ALX-9",
            metadata={}, subtitle="",
        )
        assert result is False


class TestFetchAttachment:
    @pytest.mark.asyncio
    async def test_fetch_returns_metadata(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentsForURL": {
                "nodes": [{"id": "att-1", "metadata": {"state": "implement", "run": 2}}]
            }
        })
        result = await client.fetch_stokowski_attachment("ALX-9")
        assert result == {"state": "implement", "run": 2}

    @pytest.mark.asyncio
    async def test_fetch_returns_none_when_missing(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentsForURL": {"nodes": []}
        })
        result = await client.fetch_stokowski_attachment("ALX-9")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_returns_none_on_error(self, client):
        client._graphql = AsyncMock(side_effect=RuntimeError("API error"))
        result = await client.fetch_stokowski_attachment("ALX-9")
        assert result is None


class TestDeleteAttachment:
    @pytest.mark.asyncio
    async def test_delete_finds_and_removes(self, client):
        client._graphql = AsyncMock(side_effect=[
            {"attachmentsForURL": {"nodes": [{"id": "att-1"}]}},
            {"attachmentDelete": {"success": True}},
        ])
        result = await client.delete_stokowski_attachment("ALX-9")
        assert result is True
        assert client._graphql.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentsForURL": {"nodes": []}
        })
        result = await client.delete_stokowski_attachment("ALX-9")
        assert result is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_attachment_api.py -v`
Expected: FAIL — methods not defined

- [ ] **Step 3: Add GraphQL queries and mutations to linear.py**

Add after existing mutations (around line 138):

```python
ATTACHMENT_CREATE_MUTATION = """
mutation AttachmentCreate(
    $issueId: String!, $url: String!, $title: String!,
    $subtitle: String, $metadata: JSONObject, $iconUrl: String
) {
    attachmentCreate(input: {
        issueId: $issueId, url: $url, title: $title,
        subtitle: $subtitle, metadata: $metadata, iconUrl: $iconUrl
    }) {
        success
        attachment { id }
    }
}
"""

ATTACHMENTS_BY_URL_QUERY = """
query AttachmentsByURL($url: String!) {
    attachmentsForURL(url: $url) {
        nodes { id metadata }
    }
}
"""

ATTACHMENT_DELETE_MUTATION = """
mutation AttachmentDelete($id: String!) {
    attachmentDelete(id: $id) {
        success
    }
}
"""
```

- [ ] **Step 4: Add three methods to LinearClient**

```python
STOKOWSKI_URL_PREFIX = "stokowski://state/"

async def upsert_stokowski_attachment(
    self, issue_id: str, identifier: str, metadata: dict, subtitle: str
) -> bool:
    """Create or update the Stokowski state attachment on an issue."""
    try:
        data = await self._graphql(
            ATTACHMENT_CREATE_MUTATION,
            {
                "issueId": issue_id,
                "url": f"{STOKOWSKI_URL_PREFIX}{identifier}",
                "title": "Stokowski",
                "subtitle": subtitle,
                "metadata": metadata,
            },
        )
        return data.get("attachmentCreate", {}).get("success", False)
    except Exception as e:
        logger.error(f"Failed to upsert attachment for {identifier}: {e}")
        return False

async def fetch_stokowski_attachment(
    self, identifier: str
) -> dict | None:
    """Fetch the Stokowski state attachment metadata for an issue."""
    try:
        data = await self._graphql(
            ATTACHMENTS_BY_URL_QUERY,
            {"url": f"{STOKOWSKI_URL_PREFIX}{identifier}"},
        )
        nodes = data.get("attachmentsForURL", {}).get("nodes", [])
        if nodes:
            return nodes[0].get("metadata")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch attachment for {identifier}: {e}")
        return None

async def delete_stokowski_attachment(
    self, identifier: str
) -> bool:
    """Delete the Stokowski state attachment for an issue."""
    try:
        data = await self._graphql(
            ATTACHMENTS_BY_URL_QUERY,
            {"url": f"{STOKOWSKI_URL_PREFIX}{identifier}"},
        )
        nodes = data.get("attachmentsForURL", {}).get("nodes", [])
        if not nodes:
            return False
        att_id = nodes[0]["id"]
        data = await self._graphql(
            ATTACHMENT_DELETE_MUTATION, {"id": att_id}
        )
        return data.get("attachmentDelete", {}).get("success", False)
    except Exception as e:
        logger.error(f"Failed to delete attachment for {identifier}: {e}")
        return False
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_attachment_api.py -v`
Expected: PASS

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add stokowski/linear.py tests/test_attachment_api.py
git commit -m "feat(linear): add attachment CRUD for state tracking"
```

---

### Task 2: Tracking module — attachment metadata + human-only comments

**Goal:** Add `build_attachment_metadata()` builder, refactor `make_*_comment()` to return human-only text or None, add `parse_attachment_state()`.

**Files:**
- Modify: `stokowski/tracking.py`
- Create: `tests/test_attachment_tracking.py`
- Modify: `tests/test_state_machine.py` (update existing tests that check for `<!-- stokowski:` in comments)
- Modify: `tests/test_evaluator.py` (update evaluation comment tests)

**Acceptance Criteria:**
- [ ] `build_attachment_metadata()` returns a flat dict with state/type/run/workflow + type-specific fields
- [ ] `make_state_comment()` returns `None` (state transitions are silent)
- [ ] `make_gate_comment()` returns human text only for rework and escalation, `None` for waiting/approved
- [ ] `make_evaluation_comment()` returns human text with findings for review-required, `None` for approve
- [ ] `parse_attachment_state()` converts attachment metadata dict to the same format `parse_latest_tracking()` returns
- [ ] `parse_latest_tracking()` still works for legacy comment scanning (migration fallback)
- [ ] Existing tests updated to reflect new comment format

**Verify:** `.venv/bin/python -m pytest tests/test_attachment_tracking.py tests/test_state_machine.py tests/test_evaluator.py -v`

**Steps:**

- [ ] **Step 1: Write tests for new tracking functions**

Create `tests/test_attachment_tracking.py`:

```python
"""Tests for attachment-based state tracking."""

from __future__ import annotations

from stokowski.tracking import (
    build_attachment_metadata,
    make_gate_comment,
    make_state_comment,
    make_evaluation_comment,
    parse_attachment_state,
)


class TestBuildAttachmentMetadata:
    def test_state_metadata(self):
        meta = build_attachment_metadata(
            state="implement", type="state", run=1, workflow="default",
        )
        assert meta["state"] == "implement"
        assert meta["type"] == "state"
        assert meta["run"] == 1
        assert meta["workflow"] == "default"
        assert "timestamp" in meta

    def test_gate_metadata(self):
        meta = build_attachment_metadata(
            state="merge-review", type="gate", run=2, workflow="default",
            status="waiting",
        )
        assert meta["type"] == "gate"
        assert meta["status"] == "waiting"

    def test_gate_rework_metadata(self):
        meta = build_attachment_metadata(
            state="merge-review", type="gate", run=2, workflow="default",
            status="rework", rework_to="implement",
        )
        assert meta["rework_to"] == "implement"

    def test_evaluation_metadata(self):
        meta = build_attachment_metadata(
            state="eval-merge", type="evaluation", run=1, workflow="default",
            tier="approve", summary="LGTM", findings=[],
        )
        assert meta["tier"] == "approve"
        assert meta["summary"] == "LGTM"


class TestParseAttachmentState:
    def test_parses_state_metadata(self):
        meta = {"state": "implement", "type": "state", "run": 2, "workflow": "default"}
        result = parse_attachment_state(meta)
        assert result["type"] == "state"
        assert result["state"] == "implement"
        assert result["run"] == 2
        assert result["workflow"] == "default"

    def test_parses_gate_metadata(self):
        meta = {"state": "review", "type": "gate", "run": 1, "status": "waiting", "workflow": "default"}
        result = parse_attachment_state(meta)
        assert result["type"] == "gate"
        assert result["status"] == "waiting"

    def test_returns_none_for_empty(self):
        assert parse_attachment_state(None) is None
        assert parse_attachment_state({}) is None


class TestHumanOnlyComments:
    def test_state_comment_returns_none(self):
        result = make_state_comment(state="implement", run=1)
        assert result is None

    def test_gate_waiting_returns_none(self):
        result = make_gate_comment(state="review", status="waiting", run=1)
        assert result is None

    def test_gate_approved_returns_none(self):
        result = make_gate_comment(state="review", status="approved", run=1)
        assert result is None

    def test_gate_rework_returns_human_text(self):
        result = make_gate_comment(
            state="merge-review", status="rework",
            rework_to="implement", run=2,
        )
        assert result is not None
        assert "<!-- stokowski:" not in result
        assert "Rework" in result
        assert "implement" in result

    def test_gate_escalated_returns_human_text(self):
        result = make_gate_comment(
            state="merge-review", status="escalated", run=5,
        )
        assert result is not None
        assert "<!-- stokowski:" not in result
        assert "escalat" in result.lower()

    def test_evaluation_approve_returns_none(self):
        result = make_evaluation_comment(
            state="eval-merge", tier="approve", summary="LGTM",
        )
        assert result is None

    def test_evaluation_review_required_returns_findings(self):
        result = make_evaluation_comment(
            state="eval-merge", tier="review-required",
            summary="Issues found",
            findings=["Missing tests", "SQL injection risk"],
        )
        assert result is not None
        assert "<!-- stokowski:" not in result
        assert "Missing tests" in result
        assert "SQL injection" in result
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Implement `build_attachment_metadata()` in tracking.py**

```python
def build_attachment_metadata(
    state: str,
    type: str,
    run: int = 1,
    workflow: str | None = None,
    status: str | None = None,
    rework_to: str | None = None,
    tier: str | None = None,
    summary: str | None = None,
    findings: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build the metadata dict for a Stokowski attachment."""
    meta: dict[str, Any] = {
        "state": state,
        "type": type,
        "run": run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if workflow is not None:
        meta["workflow"] = workflow
    if session_id is not None:
        meta["session_id"] = session_id
    # Gate fields
    if status is not None:
        meta["status"] = status
    if rework_to is not None:
        meta["rework_to"] = rework_to
    # Evaluation fields
    if tier is not None:
        meta["tier"] = tier
    if summary is not None:
        meta["summary"] = summary
    if findings is not None:
        meta["findings"] = findings
    return meta
```

- [ ] **Step 4: Implement `parse_attachment_state()`**

```python
def parse_attachment_state(metadata: dict | None) -> dict[str, Any] | None:
    """Convert attachment metadata to the same format as parse_latest_tracking().

    Returns None if metadata is empty or missing required fields.
    """
    if not metadata or "state" not in metadata:
        return None
    result = dict(metadata)
    result.setdefault("type", "state")
    result.setdefault("workflow", None)
    return result
```

- [ ] **Step 5: Refactor `make_state_comment()` to return None**

```python
def make_state_comment(
    state: str, run: int = 1, workflow: str | None = None
) -> str | None:
    """Build a human-readable state comment, or None if the transition is silent.

    State transitions are conveyed by the Linear ticket state change,
    so no comment is needed.
    """
    return None
```

- [ ] **Step 6: Refactor `make_gate_comment()` — human text for rework/escalation only**

```python
def make_gate_comment(
    state: str,
    status: str,
    prompt: str = "",
    rework_to: str | None = None,
    run: int = 1,
    workflow: str | None = None,
) -> str | None:
    """Build a human-readable gate comment, or None if the event is silent.

    Only rework and escalation produce comments — waiting and approved
    are conveyed by the Linear ticket state change.
    """
    if status == "rework":
        text = (
            f"**[Stokowski]** Rework requested at **{state}** "
            f"— returning to **{rework_to}**"
        )
        if run > 1:
            text += f" (run {run})"
        return text
    elif status == "escalated":
        return (
            f"**[Stokowski]** Max rework exceeded at **{state}**. "
            f"Escalating for human intervention."
        )
    return None
```

- [ ] **Step 7: Refactor `make_evaluation_comment()` — human text for review-required only**

```python
def make_evaluation_comment(
    state: str,
    tier: str,
    summary: str = "",
    findings: list[str] | None = None,
    run: int = 1,
    workflow: str | None = None,
) -> str | None:
    """Build a human-readable evaluation comment, or None if silent.

    Only review-required produces a comment (with findings).
    Approve is silent — the gate is simply skipped.
    """
    if tier != "review-required":
        return None

    text = f"**[Stokowski]** Evaluation flagged {len(findings or [])} concern(s)"
    if summary:
        text += f"\n\n{summary}"
    if findings:
        text += "\n\n**Findings:**\n"
        for finding in findings:
            text += f"- {finding}\n"
    return text
```

- [ ] **Step 8: Update existing tests that check for `<!-- stokowski:` in comments**

In `tests/test_state_machine.py`, find tests in `TestTrackingWorkflowField` and `TestLifecycleSection` that assert `<!-- stokowski:state` or `<!-- stokowski:gate` in comment output. These need to be updated:
- State comment tests: assert result is `None`
- Gate comment tests: rework/escalated assert human text without JSON; waiting/approved assert `None`

In `tests/test_evaluator.py`, update `TestEvaluationTracking`:
- `test_make_evaluation_comment_approve`: assert result is `None`
- `test_make_evaluation_comment_review_required`: assert no `<!-- stokowski:` in output, has findings text

- [ ] **Step 9: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 10: Commit**

```bash
git add stokowski/tracking.py tests/test_attachment_tracking.py tests/test_state_machine.py tests/test_evaluator.py
git commit -m "refactor(tracking): split machine state from human comments"
```

---

### Task 3: Orchestrator — wire attachment upserts and conditional comments

**Goal:** Replace all 12 `post_comment` call sites with attachment upsert + conditional human comment. Update crash recovery to read attachment first.

**Files:**
- Modify: `stokowski/orchestrator.py`
- Modify: `tests/test_state_machine.py` (update orchestrator tests that mock post_comment)

**Acceptance Criteria:**
- [ ] Every state transition upserts the attachment with current metadata
- [ ] Human comments only fire for: rework, escalation, evaluation findings
- [ ] `_resolve_current_state()` reads attachment first, falls back to comment scan
- [ ] `_cleanup_issue_state()` deletes the attachment on terminal state
- [ ] Subtitle is updated on each transition (e.g. `implement · run 3`)

**Verify:** `.venv/bin/python -m pytest tests/ -v`

**Steps:**

- [ ] **Step 1: Add `_upsert_state()` helper to Orchestrator**

This centralizes the attachment upsert + conditional comment logic:

```python
async def _upsert_state(
    self,
    issue: Issue,
    metadata: dict,
    comment: str | None = None,
) -> None:
    """Upsert the Stokowski attachment and optionally post a human comment."""
    client = self._ensure_linear_client()
    state = metadata.get("state", "")
    run = metadata.get("run", 1)
    subtitle = f"{state} · run {run}"
    await client.upsert_stokowski_attachment(
        issue_id=issue.id,
        identifier=issue.identifier,
        metadata=metadata,
        subtitle=subtitle,
    )
    if comment:
        await client.post_comment(issue.id, comment)
```

- [ ] **Step 2: Replace state comment calls with attachment upserts**

At every location that currently calls `post_comment(issue.id, make_state_comment(...))`, replace with:

```python
from .tracking import build_attachment_metadata

meta = build_attachment_metadata(
    state=target_name, type="state", run=run, workflow=workflow.name,
)
await self._upsert_state(issue, meta)
```

This applies to lines: 605, 769, 845, 931, 1172 (5 locations).

State comments return `None` now, so no human comment is passed.

- [ ] **Step 3: Replace gate comment calls with attachment upserts + conditional comments**

At every gate comment location, build the attachment metadata and pass the human comment only when it's not None:

```python
from .tracking import build_attachment_metadata, make_gate_comment

meta = build_attachment_metadata(
    state=gate_state, type="gate", run=run, workflow=workflow.name,
    status="rework", rework_to=rework_to,
)
comment = make_gate_comment(
    state=gate_state, status="rework", rework_to=rework_to, run=run,
)
await self._upsert_state(issue, meta, comment=comment)
```

This applies to lines: 592, 626, 821, 909, 924 (5 locations).

For `status="waiting"` and `status="approved"`, `make_gate_comment()` returns `None` — no human comment posted.
For `status="rework"` and `status="escalated"`, human comment is posted.

- [ ] **Step 4: Replace evaluation comment call with attachment upsert + conditional comment**

In `_handle_evaluator_exit()` (line 1460):

```python
meta = build_attachment_metadata(
    state=attempt.state_name, type="evaluation", run=run,
    workflow=wf.name, tier=tier, summary=summary, findings=findings,
)
comment = make_evaluation_comment(
    state=attempt.state_name, tier=tier, summary=summary, findings=findings,
)
await self._upsert_state(issue, meta, comment=comment)
```

For `tier="approve"`, `make_evaluation_comment()` returns `None` — silent.
For `tier="review-required"`, human comment with findings is posted.

- [ ] **Step 5: Update `_resolve_current_state()` — attachment-first recovery**

At the top of the method (after the cache check), before fetching comments, try the attachment:

```python
# Try attachment-based recovery first
client = self._ensure_linear_client()
att_meta = await client.fetch_stokowski_attachment(issue.identifier)
if att_meta:
    from .tracking import parse_attachment_state
    tracking = parse_attachment_state(att_meta)
    if tracking:
        # Same logic as comment-based recovery, using tracking dict
        # ... (reuse existing tracking resolution logic)
```

If the attachment is not found, fall back to the existing comment-scanning path (migration compatibility).

- [ ] **Step 6: Update `_cleanup_issue_state()` — delete attachment on terminal**

Add attachment deletion alongside the existing cleanup:

```python
# Delete Stokowski attachment (best-effort)
try:
    client = self._ensure_linear_client()
    await client.delete_stokowski_attachment(
        self._last_issues.get(issue_id, Issue(id="", identifier="")).identifier
    )
except Exception:
    pass
```

Note: `_cleanup_issue_state` is synchronous. The attachment delete needs to be called from the async terminal transition in `_transition()` instead, before calling `_cleanup_issue_state()`.

- [ ] **Step 7: Update cancellation comment (line 200)**

The `_post_cancellation_comment()` method posts plain text — no tracking JSON. Keep it as-is since it's already human-readable. But also upsert a terminal attachment:

```python
meta = build_attachment_metadata(
    state=state_name, type="state", run=0, workflow="",
)
await client.upsert_stokowski_attachment(
    issue_id, identifier, meta, subtitle=f"cancelled · {state_name}"
)
```

- [ ] **Step 8: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add stokowski/orchestrator.py tests/test_state_machine.py
git commit -m "feat(orchestrator): use attachment metadata for state tracking"
```

---

### Task 4: Update documentation

**Goal:** Update CLAUDE.md to reflect the new attachment-based tracking model.

**Files:**
- Modify: `CLAUDE.md`

**Acceptance Criteria:**
- [ ] Structured comment tracking section updated to describe attachment model
- [ ] Common pitfalls updated (no more `<!-- stokowski:` in comments)
- [ ] Migration path documented

**Verify:** Read CLAUDE.md and confirm accuracy.

**Steps:**

- [ ] **Step 1: Update the "Structured comment tracking" description**

Replace references to `<!-- stokowski:state/gate/evaluation {...} -->` comments with the attachment model:
- State machine position is stored in a Linear Attachment (`stokowski://state/{identifier}`) with metadata JSON
- Comments are human-readable only: rework, escalation, evaluation findings
- Crash recovery reads the attachment first, falls back to legacy comment scanning

- [ ] **Step 2: Update common pitfalls**

Remove or update pitfalls that reference HTML comment parsing. Add:
- Attachment metadata is the SoT for state; comments are the audit trail
- `_upsert_state()` is the single entry point for state persistence
- Migration: attachment-first with comment-scan fallback; once all issues cycle through, comment scanning can be removed

- [ ] **Step 3: Update tracking.py section**

Document the new functions: `build_attachment_metadata()`, `parse_attachment_state()`. Note that `make_*_comment()` now return `None` for silent events.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for attachment-based state tracking"
```
