"""Docker isolation reaches the subprocess — the claim the config guard rests on.

`docker.enabled: true` was refused for a while because the config parsed and
nothing consumed it: agents ran on the host while the operator believed they
were contained. Failing open on an isolation setting is the wrong default, so
the refusal only came off once this file proved the wrapping happens.

These assert the launch path itself rather than the helper in isolation, because
the helper was never the part that went missing — the wiring was.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from stokowski.config import ClaudeConfig, DockerConfig, HooksConfig
from stokowski.models import Issue, RunAttempt
from stokowski.runner import run_turn


def _issue() -> Issue:
    return Issue(
        id="uuid-1", identifier="ENG-1", title="t", description="d",
        state="In Progress", url="https://linear.app/x",
    )


def _attempt() -> RunAttempt:
    return RunAttempt(issue_id="uuid-1", issue_identifier="ENG-1")


class _FakeProc:
    """Enough of a process for the runner to give up on immediately."""
    pid = 4242

    def __init__(self):
        self.returncode = 0
        self.stdout = self._empty()
        self.stderr = self._empty()

    @staticmethod
    def _empty():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader

    async def wait(self):
        return 0

    def kill(self):
        pass


def _launch(docker_cfg, *, docker_image="", workspace_key="k", tmp_path=None):
    """Run one turn against a fake subprocess; return the argv it was given."""
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["argv"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        asyncio.run(run_turn(
            runner_type="claude",
            claude_cfg=ClaudeConfig(command="claude", model="claude-opus-5"),
            hooks_cfg=HooksConfig(),
            prompt="do the thing",
            workspace_path=tmp_path or Path("/tmp"),
            issue=_issue(),
            attempt=_attempt(),
            env={"LINEAR_API_KEY": "k"},
            docker_cfg=docker_cfg,
            docker_image=docker_image,
            workspace_key=workspace_key,
        ))
    return captured


def test_without_docker_the_agent_runs_directly(tmp_path):
    """The pass-through case must stay byte-identical to upstream's behaviour."""
    got = _launch(None, tmp_path=tmp_path)
    assert got["argv"][0] == "claude", f"expected a bare claude launch, got {got['argv'][:3]}"
    assert "docker" not in got["argv"]
    assert got["cwd"] == str(tmp_path), "non-docker runs must execute in the workspace"
    assert got["env"] == {"LINEAR_API_KEY": "k"}


def test_with_docker_the_agent_is_wrapped(tmp_path):
    """The whole point: the agent process is `docker run`, not `claude`."""
    cfg = DockerConfig(enabled=True, default_image="python:3.12")
    got = _launch(cfg, tmp_path=tmp_path)

    assert got["argv"][:2] == ["docker", "run"], (
        f"agent was not containerised; argv began {got['argv'][:4]}"
    )
    assert "python:3.12" in got["argv"], "configured image not passed to docker run"
    assert "claude" in got["argv"], "the inner agent command was lost"
    # docker run carries cwd and env itself; passing them again would leak the
    # operator's environment into the container.
    assert got["cwd"] is None
    assert got["env"] is None


def test_a_state_image_overrides_the_default(tmp_path):
    """Level 1 of the resolution order: state image beats docker.default_image."""
    cfg = DockerConfig(enabled=True, default_image="python:3.12")
    got = _launch(cfg, docker_image="node:22", tmp_path=tmp_path)
    assert "node:22" in got["argv"]
    assert "python:3.12" not in got["argv"]


def test_disabled_docker_config_is_still_a_pass_through(tmp_path):
    """`enabled: false` must behave exactly like no docker config at all."""
    got = _launch(DockerConfig(enabled=False, default_image="python:3.12"), tmp_path=tmp_path)
    assert got["argv"][0] == "claude"
    assert "docker" not in got["argv"]
    assert got["cwd"] == str(tmp_path)
