"""A triage pipeline gets the repo registry; everything else does not.

Triage classifies an unlabelled ticket and applies the `repo:` and `workflow:`
labels the real pipelines route on. It cannot do that without knowing which
repos exist, so dispatch injects STOKOWSKI_REPOS_JSON — and only for a workflow
marked `triage: true`, because handing every agent the registry invites one to
act on a repo it was not routed to.

Rewritten from the fork's pre-convergence version, which declared workflows
inline with `path:` lists. Upstream loads them from `workflows/*.yaml`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stokowski.config import parse_workflow_file, validate_config

REPO = Path(__file__).resolve().parent.parent

_MAIN = """
tracker:
  api_key: test-key
  project_slug: abc123

prompts:
  global: g.md

linear_states:
  todo: Todo
  active: In Progress
  review: Human Review
  done: Done

routing:
  default: standard
  rules:
    - label: "workflow:intake"
      workflow: intake

repos:
  api:
    label: "repo:api"
    clone_url: "git@github.com:org/api.git"
  web:
    label: "repo:web"
    clone_url: "git@github.com:org/web.git"
"""

_PIPELINE = """
states:
  work:
    type: agent
    prompt: p.md
    transitions:
      complete: done
  done:
    type: terminal
"""


def _write(tmp_path: Path, *, with_triage: bool, with_repos: bool = True) -> Path:
    # Each call gets its own directory so one test can build two configs.
    tmp_path = tmp_path / ("triage" if with_triage else "no-triage")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "p.md").write_text("stage prompt\n")
    (tmp_path / "g.md").write_text("global prompt\n")
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(exist_ok=True)
    (wf_dir / "standard.yaml").write_text(_PIPELINE)
    if with_triage:
        (wf_dir / "intake.yaml").write_text("triage: true\n" + _PIPELINE)

    main = _MAIN if with_repos else _MAIN[: _MAIN.index("repos:")]
    path = tmp_path / "workflow.yaml"
    path.write_text(main)
    return path


def _cfg(tmp_path: Path, **kw):
    return parse_workflow_file(str(_write(tmp_path, **kw))).config


def test_the_triage_flag_is_read_from_the_workflow_file(tmp_path):
    cfg = _cfg(tmp_path, with_triage=True)
    assert cfg.workflows["intake"].triage is True
    assert cfg.workflows["standard"].triage is False, (
        "triage must default off; a pipeline that did not ask for the registry "
        "should not receive it"
    )


def _orch(path: Path):
    """A real Orchestrator loaded from the config on disk."""
    from stokowski.orchestrator import Orchestrator

    orch = Orchestrator(str(path))
    errors = orch._load_workflow()
    assert not errors, f"config errors: {errors}"
    return orch


def _repos_json(path: Path, *, labels: list[str] | None = None) -> list[dict]:
    """What dispatch would actually inject — asked of the orchestrator itself.

    Replicating the payload here instead would test this file against itself
    and keep passing even if dispatch stopped injecting anything.
    """
    from stokowski.models import Issue

    issue = Issue(
        id="i", identifier="ENG-1", title="t", description="", state="Todo",
        url="https://linear.app/x",
        labels=labels if labels is not None else ["workflow:intake"],
    )
    raw = _orch(path)._triage_env_for(issue).get("STOKOWSKI_REPOS_JSON")
    return json.loads(raw) if raw is not None else []


def test_the_payload_matches_the_shape_the_prompt_documents(tmp_path):
    entries = _repos_json(_write(tmp_path, with_triage=True))
    assert {e["name"] for e in entries} == {"api", "web"}
    for entry in entries:
        assert set(entry) == {"name", "label", "clone_url"}, (
            f"prompts/triage.example.md documents exactly these keys; got {sorted(entry)}"
        )


def test_the_synthetic_default_repo_is_never_offered(tmp_path):
    """A legacy single-repo config has only `_default`, which has no label."""
    path = _write(tmp_path, with_triage=True, with_repos=False)
    assert parse_workflow_file(str(path)).config.repos_synthesized is True
    assert _repos_json(path) == [], (
        "the synthetic _default carries no label for an agent to apply, so "
        "offering it to triage would invite an unusable classification"
    )


def test_multi_repo_without_a_default_needs_exactly_one_triage_workflow(tmp_path):
    """Otherwise an unlabelled ticket has nowhere to go, silently."""
    with_triage = validate_config(_cfg(tmp_path, with_triage=True))
    assert not [e for e in with_triage if "triage" in e], (
        f"a triage workflow should satisfy the rule; got {with_triage}"
    )

    without = validate_config(_cfg(tmp_path, with_triage=False))
    assert any("triage: true" in e for e in without), (
        "a multi-repo config with no default repo and no triage workflow must "
        "be rejected rather than dropping unlabelled tickets"
    )


def test_the_shipped_triage_prompt_states_the_env_contract():
    """The prompt is the agent's only description of this variable."""
    text = (REPO / "prompts" / "triage.example.md").read_text()
    assert "STOKOWSKI_REPOS_JSON" in text, (
        "the triage prompt never tells the agent where the repo list comes from"
    )


def test_a_non_triage_pipeline_is_given_nothing(tmp_path):
    """Only triage gets the registry.

    Handing the full repo list to every agent invites one to act on a repo it
    was not routed to, so the absence matters as much as the presence.
    """
    path = _write(tmp_path, with_triage=True)
    assert _repos_json(path, labels=["workflow:intake"]), "triage got nothing"
    assert _repos_json(path, labels=[]) == [], (
        "a ticket routed to the default (non-triage) pipeline was handed the "
        "repo registry anyway"
    )
