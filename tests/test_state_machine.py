"""Pure-function tests for CE workflow state machine extensions.

No mocks, no network, no Linear/Docker. Tests the most fragile logic paths:
- Transition directive regex parsing
- max_rework boundary conditions
- skip_labels matching
- Lifecycle section output for multi-transition states
- Config parsing and validation
"""

from __future__ import annotations

import re

import pytest

from stokowski.config import (
    LinearStatesConfig,
    StateConfig,
    WorkflowConfig,
    _coerce_list,
    _parse_state_config,
    derive_workflow_transitions,
)
from stokowski.models import RunAttempt
from stokowski.prompt import build_lifecycle_section
from stokowski.runner import TRANSITION_PATTERN
from stokowski.tracking import (
    make_gate_comment,
    make_state_comment,
    parse_latest_tracking,
)


# ---------------------------------------------------------------------------
# Transition directive regex
# ---------------------------------------------------------------------------


class TestTransitionPattern:
    def test_valid_directive(self):
        text = "Review complete. <!-- transition:rework -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["rework"]

    def test_complete_directive(self):
        text = "All good. <!-- transition:complete -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["complete"]

    def test_hyphenated_name(self):
        text = "<!-- transition:merge-review -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["merge-review"]

    def test_whitespace_tolerance(self):
        text = "<!--  transition:rework  -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["rework"]

    def test_no_directive(self):
        text = "Just regular output with no directive."
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == []

    def test_multiple_directives_takes_last(self):
        text = (
            "Example: <!-- transition:rework -->\n"
            "But actually: <!-- transition:complete -->"
        )
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["rework", "complete"]
        assert matches[-1] == "complete"

    def test_directive_in_code_block(self):
        text = "```\n<!-- transition:rework -->\n```"
        matches = TRANSITION_PATTERN.findall(text)
        # Regex still matches inside code blocks — the LAST match strategy
        # means the agent's real directive at the end wins
        assert matches == ["rework"]

    def test_invalid_chars_no_match(self):
        text = "<!-- transition:not valid -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == []  # space breaks the match

    def test_empty_name_no_match(self):
        text = "<!-- transition: -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == []


# ---------------------------------------------------------------------------
# max_rework boundary conditions
# ---------------------------------------------------------------------------


class TestMaxReworkBoundary:
    """Test the logic: if run > max_rework, force complete."""

    @staticmethod
    def should_force_complete(run: int, max_rework: int | None) -> bool:
        """Extracted pure function matching _on_worker_exit logic."""
        if max_rework is None:
            return False
        return run > max_rework

    def test_first_run_allows_rework(self):
        assert not self.should_force_complete(run=1, max_rework=3)

    def test_at_limit_allows_rework(self):
        assert not self.should_force_complete(run=3, max_rework=3)

    def test_past_limit_forces_complete(self):
        assert self.should_force_complete(run=4, max_rework=3)

    def test_no_limit_always_allows(self):
        assert not self.should_force_complete(run=100, max_rework=None)

    def test_limit_of_one(self):
        assert not self.should_force_complete(run=1, max_rework=1)
        assert self.should_force_complete(run=2, max_rework=1)


# ---------------------------------------------------------------------------
# skip_labels matching
# ---------------------------------------------------------------------------


class TestSkipLabelsMatching:
    @staticmethod
    def should_skip(issue_labels: list[str], skip_labels: list[str]) -> bool:
        """Extracted pure function matching _enter_gate logic."""
        issue_labels_lower = [l.lower() for l in issue_labels]
        skip_labels_lower = [s.lower() for s in skip_labels]
        return any(sl in issue_labels_lower for sl in skip_labels_lower)

    def test_yolo_matches(self):
        assert self.should_skip(["yolo", "bug"], ["yolo"])

    def test_case_insensitive(self):
        assert self.should_skip(["Yolo"], ["yolo"])
        assert self.should_skip(["yolo"], ["YOLO"])

    def test_no_match(self):
        assert not self.should_skip(["bug", "feature"], ["yolo"])

    def test_empty_issue_labels(self):
        assert not self.should_skip([], ["yolo"])

    def test_empty_skip_labels(self):
        assert not self.should_skip(["yolo"], [])

    def test_multiple_skip_labels(self):
        assert self.should_skip(["skip-plan-review"], ["yolo", "skip-plan-review"])


# ---------------------------------------------------------------------------
# Lifecycle section output
# ---------------------------------------------------------------------------


class TestLifecycleSection:
    def _make_issue(self):
        from stokowski.models import Issue
        return Issue(id="test-id", identifier="TEST-1", title="Test issue", url="https://linear.app/test")

    def test_single_transition_no_directive(self):
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue(),
            state_name="implement",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "<!-- transition:TRANSITION_NAME -->" not in section
        assert "complete" in section.lower()
        assert "automatically" in section.lower()

    def test_multi_transition_includes_directive(self):
        state = StateConfig(
            name="review",
            transitions={"complete": "merge-review", "rework": "implement"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue(),
            state_name="review",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "<!-- transition:TRANSITION_NAME -->" in section
        assert "rework" in section
        assert "complete" in section


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_parse_skip_labels_list(self):
        state = _parse_state_config("gate", {
            "type": "gate",
            "linear_state": "review",
            "skip_labels": ["yolo", "skip-plan-review"],
            "rework_to": "plan",
            "transitions": {"approve": "implement"},
        })
        assert state.skip_labels == ["yolo", "skip-plan-review"]

    def test_parse_skip_labels_comma_string(self):
        state = _parse_state_config("gate", {
            "type": "gate",
            "linear_state": "review",
            "skip_labels": "yolo, skip-plan-review",
            "rework_to": "plan",
            "transitions": {"approve": "implement"},
        })
        assert state.skip_labels == ["yolo", "skip-plan-review"]

    def test_parse_skip_labels_missing(self):
        state = _parse_state_config("gate", {
            "type": "gate",
            "linear_state": "review",
            "rework_to": "plan",
            "transitions": {"approve": "implement"},
        })
        assert state.skip_labels == []

    def test_parse_max_rework_on_agent(self):
        state = _parse_state_config("review", {
            "type": "agent",
            "prompt": "prompts/review.md",
            "max_rework": 3,
            "transitions": {"complete": "merge-review", "rework": "implement"},
        })
        assert state.max_rework == 3


# ---------------------------------------------------------------------------
# Guardrail text in prompts
# ---------------------------------------------------------------------------


class TestGuardrailPromptText:
    def _make_issue(self, identifier="FOO-123"):
        from stokowski.models import Issue
        return Issue(id="test-id", identifier=identifier, title="Test issue")

    def test_lifecycle_section_contains_scope_restriction(self):
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue("FOO-123"),
            state_name="implement",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "Scope Restriction" in section
        assert "FOO-123" in section
        assert "Do not modify" in section

    def test_lifecycle_guardrail_allows_reads(self):
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue(),
            state_name="implement",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "may read" in section.lower()

    def test_lifecycle_guardrail_present_on_every_turn(self):
        """Scope restriction must appear regardless of run number or rework status."""
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        for run, is_rework in [(1, False), (3, True)]:
            section = build_lifecycle_section(
                issue=self._make_issue(),
                state_name="implement",
                state_cfg=state,
                linear_states=LinearStatesConfig(),
                run=run,
                is_rework=is_rework,
            )
            assert "Scope Restriction" in section

    def test_system_prompt_guardrail_interpolation(self):
        from pathlib import Path
        from stokowski.config import ClaudeConfig
        from stokowski.runner import build_claude_args

        cfg = ClaudeConfig(command="claude")
        args = build_claude_args(
            cfg, "test prompt", Path("/tmp"),
            session_id=None, issue_identifier="BAR-456",
        )
        # Find the --append-system-prompt value
        idx = args.index("--append-system-prompt")
        system_prompt = args[idx + 1]
        assert "BAR-456" in system_prompt
        assert "Do NOT" in system_prompt
        assert "modify" in system_prompt

    def test_system_prompt_no_guardrail_without_identifier(self):
        from pathlib import Path
        from stokowski.config import ClaudeConfig
        from stokowski.runner import build_claude_args

        cfg = ClaudeConfig(command="claude")
        args = build_claude_args(
            cfg, "test prompt", Path("/tmp"),
            session_id=None, issue_identifier=None,
        )
        idx = args.index("--append-system-prompt")
        system_prompt = args[idx + 1]
        # Guardrail should NOT be present without identifier
        assert "Do NOT use Linear" not in system_prompt

    def test_system_prompt_not_on_continuation(self):
        from pathlib import Path
        from stokowski.config import ClaudeConfig
        from stokowski.runner import build_claude_args

        cfg = ClaudeConfig(command="claude")
        args = build_claude_args(
            cfg, "test prompt", Path("/tmp"),
            session_id="existing-session", issue_identifier="FOO-123",
        )
        # No --append-system-prompt on continuation turns
        assert "--append-system-prompt" not in args


# ---------------------------------------------------------------------------
# Workflow transition derivation
# ---------------------------------------------------------------------------


class TestWorkflowTransitionDerivation:
    """Tests for derive_workflow_transitions() and WorkflowConfig."""

    def _make_states(self, specs: dict[str, str]) -> dict[str, StateConfig]:
        """Build a states dict from {name: type} mapping."""
        states: dict[str, StateConfig] = {}
        for name, stype in specs.items():
            states[name] = StateConfig(name=name, type=stype)
        return states

    def test_linear_agent_path(self):
        """Linear path [a, b, c] with all agent states produces complete transitions."""
        states = self._make_states({"a": "agent", "b": "agent", "c": "agent"})
        transitions = derive_workflow_transitions(["a", "b", "c"], states)
        assert transitions["a"] == {"complete": "b"}
        assert transitions["b"] == {"complete": "c"}
        # Last agent state has no successor
        assert transitions["c"] == {}

    def test_path_with_gate(self):
        """Path with gate state gets approve and rework_to transitions."""
        states = self._make_states({
            "plan": "agent",
            "review": "gate",
            "implement": "agent",
        })
        transitions = derive_workflow_transitions(
            ["plan", "review", "implement"], states
        )
        assert transitions["plan"] == {"complete": "review"}
        assert transitions["review"] == {"approve": "implement", "rework_to": "plan"}
        assert transitions["implement"] == {}

    def test_gate_explicit_rework_to_wins(self):
        """Gate with explicit rework_to on StateConfig overrides path-derived."""
        states = self._make_states({
            "plan": "agent",
            "implement": "agent",
            "review": "gate",
            "done": "terminal",
        })
        # Explicitly set rework_to on the gate to a non-adjacent state
        states["review"].rework_to = "plan"
        transitions = derive_workflow_transitions(
            ["plan", "implement", "review", "done"], states
        )
        # Explicit rework_to="plan" wins over path-derived "implement"
        assert transitions["review"]["rework_to"] == "plan"
        assert transitions["review"]["approve"] == "done"

    def test_gate_derives_previous_agent(self):
        """Gate without explicit rework_to derives nearest prior agent in path."""
        states = self._make_states({
            "plan": "agent",
            "implement": "agent",
            "review": "gate",
            "done": "terminal",
        })
        # No explicit rework_to on gate
        assert states["review"].rework_to is None
        transitions = derive_workflow_transitions(
            ["plan", "implement", "review", "done"], states
        )
        # Should derive rework_to as "implement" (nearest prior agent)
        assert transitions["review"]["rework_to"] == "implement"

    def test_terminal_at_end_of_path(self):
        """Terminal state at end of path gets empty transitions."""
        states = self._make_states({
            "implement": "agent",
            "done": "terminal",
        })
        transitions = derive_workflow_transitions(["implement", "done"], states)
        assert transitions["implement"] == {"complete": "done"}
        assert transitions["done"] == {}

    def test_single_agent_plus_terminal(self):
        """Single-state path [a, done] produces a->done."""
        states = self._make_states({"a": "agent", "done": "terminal"})
        transitions = derive_workflow_transitions(["a", "done"], states)
        assert transitions["a"] == {"complete": "done"}
        assert transitions["done"] == {}

    def test_entry_state_is_first_agent(self):
        """WorkflowConfig.entry_state should be first agent, not first state overall."""
        states = self._make_states({
            "done": "terminal",
            "plan": "agent",
            "implement": "agent",
        })
        path = ["plan", "implement", "done"]
        transitions = derive_workflow_transitions(path, states)
        # Simulate what config parsing would do: find first agent in path
        entry = ""
        for name in path:
            if states[name].type == "agent":
                entry = name
                break
        assert entry == "plan"

        # Also verify WorkflowConfig can hold this correctly
        wf = WorkflowConfig(
            name="test",
            path=path,
            transitions=transitions,
            entry_state=entry,
        )
        assert wf.entry_state == "plan"

    def test_gate_at_start_no_prior_agent(self):
        """Gate at path start without prior agent has no rework_to (no crash)."""
        states = self._make_states({
            "gate": "gate",
            "implement": "agent",
            "done": "terminal",
        })
        transitions = derive_workflow_transitions(
            ["gate", "implement", "done"], states
        )
        # No prior agent exists, so only approve should be set
        assert transitions["gate"] == {"approve": "implement"}
        assert "rework_to" not in transitions["gate"]

    def test_workflow_config_defaults(self):
        """WorkflowConfig has sensible defaults."""
        wf = WorkflowConfig()
        assert wf.name == ""
        assert wf.label is None
        assert wf.default is False
        assert wf.path == []
        assert wf.terminal_state == "terminal"
        assert wf.transitions == {}
        assert wf.entry_state == ""

    def test_workflow_config_with_fields(self):
        """WorkflowConfig can be constructed with all fields."""
        wf = WorkflowConfig(
            name="full-ce",
            label="workflow:full-ce",
            default=False,
            path=["plan", "review", "implement", "done"],
            terminal_state="terminal",
            transitions={"plan": {"complete": "review"}},
            entry_state="plan",
        )
        assert wf.name == "full-ce"
        assert wf.label == "workflow:full-ce"
        assert wf.default is False
        assert len(wf.path) == 4
        assert wf.terminal_state == "terminal"
        assert wf.transitions["plan"]["complete"] == "review"
        assert wf.entry_state == "plan"

    def test_triage_workflow_terminal_todo(self):
        """Triage workflow can set terminal_state to 'todo' for recycling."""
        wf = WorkflowConfig(
            name="triage",
            default=True,
            terminal_state="todo",
            path=["classify", "done"],
        )
        assert wf.terminal_state == "todo"
        assert wf.default is True
        assert wf.label is None


# ---------------------------------------------------------------------------
# Tracking comments — workflow field
# ---------------------------------------------------------------------------


class TestTrackingWorkflowField:
    def test_state_comment_with_workflow_includes_field(self):
        comment = make_state_comment("implement", run=1, workflow="quick-fix")
        assert '"workflow": "quick-fix"' in comment

    def test_state_comment_without_workflow_omits_field(self):
        comment = make_state_comment("implement", run=1)
        assert '"workflow"' not in comment

    def test_state_comment_with_workflow_human_text(self):
        comment = make_state_comment("implement", run=2, workflow="full-ce")
        assert "(workflow: full-ce, run 2)" in comment

    def test_state_comment_without_workflow_human_text(self):
        comment = make_state_comment("implement", run=1)
        assert "(run 1)" in comment
        assert "workflow" not in comment.split("\n\n")[1]  # human-readable part

    def test_parse_extracts_workflow_from_state_comment(self):
        comment = make_state_comment("implement", run=1, workflow="quick-fix")
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] == "quick-fix"
        assert result["type"] == "state"
        assert result["state"] == "implement"

    def test_parse_returns_none_workflow_from_old_format(self):
        comment = make_state_comment("implement", run=1)
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] is None
        assert result["state"] == "implement"

    def test_gate_comment_with_workflow(self):
        comment = make_gate_comment(
            "review-gate", "waiting", prompt="Review the PR",
            rework_to="implement", run=1, workflow="full-ce",
        )
        assert '"workflow": "full-ce"' in comment
        assert "Awaiting human review" in comment

    def test_gate_comment_without_workflow_omits_field(self):
        comment = make_gate_comment(
            "review-gate", "waiting", run=1,
        )
        assert '"workflow"' not in comment

    def test_gate_comment_parse_extracts_workflow(self):
        comment = make_gate_comment(
            "review-gate", "approved", run=1, workflow="full-ce",
        )
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["type"] == "gate"
        assert result["workflow"] == "full-ce"

    def test_gate_comment_parse_old_format(self):
        comment = make_gate_comment("review-gate", "approved", run=1)
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["type"] == "gate"
        assert result["workflow"] is None

    def test_round_trip_state_comment(self):
        """Create a state comment with workflow, parse it back, verify workflow."""
        comment = make_state_comment("plan", run=3, workflow="quick-fix")
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] == "quick-fix"
        assert result["state"] == "plan"
        assert result["run"] == 3

    def test_round_trip_gate_comment(self):
        """Create a gate comment with workflow, parse it back, verify workflow."""
        comment = make_gate_comment(
            "review", "rework", rework_to="implement",
            run=2, workflow="full-ce",
        )
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] == "full-ce"
        assert result["state"] == "review"
        assert result["status"] == "rework"
        assert result["rework_to"] == "implement"

    def test_latest_comment_wins(self):
        """When multiple tracking comments exist, the latest one wins."""
        old_comment = make_state_comment("plan", run=1, workflow="triage")
        new_comment = make_state_comment("implement", run=1, workflow="quick-fix")
        result = parse_latest_tracking([
            {"body": old_comment},
            {"body": new_comment},
        ])
        assert result is not None
        assert result["workflow"] == "quick-fix"
        assert result["state"] == "implement"
