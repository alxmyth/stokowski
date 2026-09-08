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

    def test_session_id_included(self):
        meta = build_attachment_metadata(
            state="implement", type="state", run=1, session_id="abc-123",
        )
        assert meta["session_id"] == "abc-123"


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
