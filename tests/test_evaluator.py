"""Tests for evaluator state type — config, transitions, tracking, prompt."""

from __future__ import annotations

import pytest

from stokowski.config import (
    LinearStatesConfig,
    PromptsConfig,
    ServiceConfig,
    StateConfig,
    TrackerConfig,
    WorkflowConfig,
    _parse_state_config,
    derive_workflow_transitions,
    validate_config,
)
from stokowski.models import Issue


# ---------------------------------------------------------------------------
# Shared test helper
# ---------------------------------------------------------------------------

def make_test_cfg(states, workflows=None, prompts=None):
    """Build a ServiceConfig for testing."""
    if workflows is None:
        workflows = {
            "_default": WorkflowConfig(
                name="_default", default=True,
                path=list(states.keys()),
                transitions=derive_workflow_transitions(
                    list(states.keys()), states
                ),
            )
        }
    return ServiceConfig(
        tracker=TrackerConfig(
            api_key="lin_api_test", project_slug="abc123"
        ),
        linear_states=LinearStatesConfig(),
        states=states,
        workflows=workflows,
        prompts=prompts or PromptsConfig(),
    )


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestEvaluatorConfigParsing:
    def test_parse_evaluator_state(self):
        raw = {
            "type": "evaluator",
            "prompt": "prompts/eval.md",
            "linear_state": "active",
            "model": "claude-sonnet-4-6",
            "max_turns": 5,
            "session": "fresh",
            "auto_approve": True,
        }
        sc = _parse_state_config("eval-merge", raw)
        assert sc.type == "evaluator"
        assert sc.auto_approve is True
        assert sc.session == "fresh"
        assert sc.max_turns == 5

    def test_auto_approve_defaults_false(self):
        raw = {"type": "evaluator", "prompt": "prompts/eval.md"}
        sc = _parse_state_config("eval-merge", raw)
        assert sc.auto_approve is False

    def test_auto_approve_on_non_evaluator(self):
        raw = {"type": "agent", "prompt": "prompts/impl.md", "auto_approve": True}
        sc = _parse_state_config("implement", raw)
        assert sc.auto_approve is True

    def test_evaluator_defaults_session_to_fresh(self):
        raw = {"type": "evaluator", "prompt": "prompts/eval.md"}
        sc = _parse_state_config("eval-merge", raw)
        assert sc.session == "fresh"

    def test_prompts_config_evaluator_prompt(self):
        pc = PromptsConfig(global_prompt="g.md", evaluator_prompt="eval.md")
        assert pc.evaluator_prompt == "eval.md"

    def test_prompts_config_evaluator_prompt_default_none(self):
        pc = PromptsConfig()
        assert pc.evaluator_prompt is None


class TestEvaluatorActiveStates:
    def test_evaluator_included_in_active_states(self):
        states = {
            "implement": StateConfig(name="implement", type="agent", linear_state="active"),
            "eval-merge": StateConfig(name="eval-merge", type="evaluator", linear_state="active"),
            "review": StateConfig(name="review", type="gate", linear_state="review"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = make_test_cfg(states)
        active = cfg.active_linear_states()
        assert "In Progress" in active


class TestEvaluatorValidation:
    def test_evaluator_is_valid_type(self):
        states = {
            "implement": StateConfig(name="implement", type="agent", prompt="p.md", linear_state="active"),
            "eval-merge": StateConfig(name="eval-merge", type="evaluator", prompt="e.md", linear_state="active", session="fresh"),
            "merge-review": StateConfig(name="merge-review", type="gate", linear_state="review", rework_to="implement", transitions={"approve": "done"}),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = make_test_cfg(states)
        errors = validate_config(cfg)
        assert not errors, errors

    def test_evaluator_not_followed_by_gate_errors(self):
        states = {
            "implement": StateConfig(name="implement", type="agent", prompt="p.md", linear_state="active"),
            "eval-merge": StateConfig(name="eval-merge", type="evaluator", prompt="e.md", linear_state="active", session="fresh"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = make_test_cfg(states)
        errors = validate_config(cfg)
        assert any("must be immediately followed by a gate" in e for e in errors)

    def test_evaluator_no_prompt_no_global_errors(self):
        states = {
            "implement": StateConfig(name="implement", type="agent", prompt="p.md", linear_state="active"),
            "eval-merge": StateConfig(name="eval-merge", type="evaluator", linear_state="active", session="fresh"),
            "merge-review": StateConfig(name="merge-review", type="gate", linear_state="review", rework_to="implement"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = make_test_cfg(states)
        errors = validate_config(cfg)
        assert any("no prompt" in e for e in errors)

    def test_evaluator_uses_global_evaluator_prompt(self):
        states = {
            "implement": StateConfig(name="implement", type="agent", prompt="p.md", linear_state="active"),
            "eval-merge": StateConfig(name="eval-merge", type="evaluator", linear_state="active", session="fresh"),
            "merge-review": StateConfig(name="merge-review", type="gate", linear_state="review", rework_to="implement"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = make_test_cfg(states, prompts=PromptsConfig(evaluator_prompt="prompts/eval.md"))
        errors = validate_config(cfg)
        assert not any("no prompt" in e for e in errors)
