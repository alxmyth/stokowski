"""Agent runner - launches Claude Code in headless mode and streams results."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import ClaudeConfig, DockerConfig, HooksConfig
from .docker_runner import build_docker_run_args, container_name_for, kill_container
from .events import EventCallback, process_event
from .models import Issue, RunAttempt

logger = logging.getLogger("stokowski.runner")

# Pattern for agent-requested transition directives in result text
TRANSITION_PATTERN = re.compile(r"<!--\s*transition:(\w[\w-]*)\s*-->")

# Callback for registering/unregistering child PIDs
PidCallback = Callable[[int, bool], None]  # (pid, is_register)

# Appended to the system prompt on the first turn of every run.
#
# The constraint here is *interactivity*, not tooling. Slash commands and
# skills run fine headlessly (a project command invoked via `claude -p
# "/review-changes"` returns normally), and for most repos they are the best
# work available — banning them outright cuts the agent off from the project's
# own review, doc and codegen tooling. What actually breaks an unattended run
# is anything that stops to wait for a human.
HEADLESS_CONTEXT = (
    "You are running unattended via the Stokowski orchestrator. No human will "
    "see your output until you finish, and nothing can answer a question "
    "mid-run.\n"
    "You MAY use slash commands, skills and subagents — they work normally "
    "here, and the project's own commands are usually the best way to do a "
    "job.\n"
    "You MUST NOT use anything that waits for a human: plan mode, "
    "brainstorming or other clarification-first workflows, or any prompt that "
    "asks the user to choose, confirm or approve. Never end a turn waiting for "
    "an answer.\n"
    "When you hit an ambiguity, decide it yourself using the best available "
    "evidence, state the assumption you made in your final summary, and keep "
    "going. Stop early only for a true blocker you cannot work around, such as "
    "missing credentials — and say exactly what is missing."
)


def _prepare_docker_args(
    docker_cfg: DockerConfig | None,
    args: list[str],
    workspace_path: Path,
    workspace_key: str,
    issue: Issue,
    attempt: RunAttempt,
    env: dict[str, str] | None,
    docker_image: str = "",
) -> tuple[list[str], str | None, str | None, dict[str, str] | None]:
    """Wrap CLI args in docker run if Docker is enabled.

    Returns (args, container_name, cwd, env) -- when Docker is enabled,
    cwd and env are None (handled by docker run args).
    """
    if not (docker_cfg and docker_cfg.enabled):
        return args, None, str(workspace_path), env

    container_name = container_name_for(
        issue.identifier, attempt.turn_count + 1, attempt.attempt
    )
    attempt.container_name = container_name
    image = docker_image or docker_cfg.default_image
    docker_args = build_docker_run_args(
        docker_cfg=docker_cfg,
        image=image,
        command=args,
        workspace_key=workspace_key,
        env=env or {},
        container_name=container_name,
    )
    return docker_args, container_name, None, None


SCOPE_RESTRICTION_SYSTEM = (
    "Do NOT use Linear tools to modify, comment on, or transition any Linear issue "
    "other than {issue_identifier}. You may read other issues for context, but do "
    "not take any write action on them."
)


def build_claude_args(
    claude_cfg: ClaudeConfig,
    prompt: str,
    workspace_path: Path,
    session_id: str | None = None,
    issue_identifier: str | None = None,
) -> list[str]:
    """Build the claude CLI argument list."""
    args = [claude_cfg.command]

    if session_id:
        # Continuation turn
        args.extend(["-p", prompt, "--resume", session_id])
    else:
        # First turn
        args.extend(["-p", prompt])

    args.extend(["--verbose", "--output-format", "stream-json"])

    # Permission mode
    if claude_cfg.permission_mode == "auto":
        args.append("--dangerously-skip-permissions")
    elif claude_cfg.permission_mode == "allowedTools" and claude_cfg.allowed_tools:
        args.extend(["--allowedTools", ",".join(claude_cfg.allowed_tools)])

    # Model override
    if claude_cfg.model:
        args.extend(["--model", claude_cfg.model])

    # Fall back to another model when the primary is overloaded or unavailable.
    # Worth setting when runs collide with a rate-limit window.
    if claude_cfg.fallback_model:
        args.extend(["--fallback-model", claude_cfg.fallback_model])

    # Reasoning effort. Cheap stages (a merge) rarely need more than low;
    # a grounding check earns high or above.
    if claude_cfg.effort:
        args.extend(["--effort", claude_cfg.effort])

    # System prompt - always include headless context, plus any user additions
    if not session_id:
        # Scope restriction guardrail — prohibit writing to other Linear issues
        if issue_identifier:
            guardrail = SCOPE_RESTRICTION_SYSTEM.format(
                issue_identifier=issue_identifier
            )
            headless_context = f"{headless_context}\n{guardrail}"

        extra = claude_cfg.append_system_prompt or ""
        combined = f"{HEADLESS_CONTEXT}\n{extra}".strip()
        args.extend(["--append-system-prompt", combined])

    return args


def build_codex_args(
    model: str | None,
    prompt: str,
    workspace_path: Path,
) -> list[str]:
    """Build the codex CLI argument list."""
    args = ["codex", "--quiet"]
    if model:
        args.extend(["--model", model])
    args.extend(["--prompt", prompt])
    return args


async def run_codex_turn(
    model: str | None,
    hooks_cfg: HooksConfig,
    prompt: str,
    workspace_path: Path,
    issue: Issue,
    attempt: RunAttempt,
    on_pid: PidCallback | None = None,
    turn_timeout_ms: int = 3_600_000,
    stall_timeout_ms: int = 300_000,
    env: dict[str, str] | None = None,
    docker_cfg: DockerConfig | None = None,
    workspace_key: str = "",
    docker_image: str = "",
) -> RunAttempt:
    """Run a single Codex turn. Returns updated RunAttempt.

    Codex doesn't support session resumption or stream-json output.
    We capture stdout/stderr and use exit code for status.
    """
    args = build_codex_args(model, prompt, workspace_path)

    # Docker wrapping
    args, container_name, sub_cwd, sub_env = _prepare_docker_args(
        docker_cfg, args, workspace_path, workspace_key, issue, attempt, env, docker_image
    )

    logger.info(
        f"Launching codex issue={issue.identifier} "
        f"turn={attempt.turn_count + 1}",
        extra={"linked_to": issue.identifier},
    )

    # Run before_run hook
    if hooks_cfg.before_run:
        from .workspace import run_hook

        ok = await run_hook(
            hooks_cfg.before_run, workspace_path, hooks_cfg.timeout_ms, "before_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )
        if not ok:
            attempt.status = "failed"
            attempt.error = "before_run hook failed"
            return attempt

    attempt.status = "streaming"
    attempt.started_at = attempt.started_at or datetime.now(timezone.utc)
    attempt.turn_count += 1
    attempt.last_event_at = datetime.now(timezone.utc)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=sub_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=10 * 1024 * 1024,  # 10MB line buffer (default 64KB)
            env=sub_env,
        )
        if on_pid and proc.pid:
            on_pid(proc.pid, True)
        attempt.pid = proc.pid
    except FileNotFoundError:
        attempt.status = "failed"
        attempt.error = "Docker command not found" if container_name else "Codex command not found: codex"
        logger.error(attempt.error, extra={"linked_to": issue.identifier})
        return attempt

    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    stall_timeout_s = stall_timeout_ms / 1000
    turn_timeout_s = turn_timeout_ms / 1000

    async def read_stream():
        nonlocal last_activity
        output_lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            last_activity = loop.time()
            attempt.last_event_at = datetime.now(timezone.utc)
            line_str = line.decode().strip()
            if line_str:
                output_lines.append(line_str)
                attempt.last_message = line_str[:200]
        return output_lines

    async def stall_monitor():
        while proc.returncode is None:
            await asyncio.sleep(min(stall_timeout_s / 4, 30))
            elapsed = loop.time() - last_activity
            if stall_timeout_s > 0 and elapsed > stall_timeout_s:
                logger.warning(
                    f"Codex stall detected issue={issue.identifier} "
                    f"elapsed={elapsed:.0f}s",
                    extra={"linked_to": issue.identifier},
                )
                proc.kill()
                if container_name:
                    asyncio.create_task(kill_container(container_name))
                attempt.status = "stalled"
                attempt.error = f"No output for {elapsed:.0f}s"
                return

    try:
        reader = asyncio.create_task(read_stream())
        monitor = asyncio.create_task(stall_monitor())

        done, pending = await asyncio.wait(
            {reader, monitor},
            timeout=turn_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            logger.warning(f"Codex turn timeout issue={issue.identifier}", extra={"linked_to": issue.identifier})
            proc.kill()
            if container_name:
                asyncio.create_task(kill_container(container_name))
            attempt.status = "timed_out"
            attempt.error = f"Turn exceeded {turn_timeout_s}s"
        else:
            await asyncio.wait_for(proc.wait(), timeout=30)

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error(f"Codex runner error issue={issue.identifier}: {e}", extra={"linked_to": issue.identifier})
        proc.kill()
        attempt.status = "failed"
        attempt.error = str(e)
        # Still need to run after_run hook and unregister PID below

    # Determine final status from exit code if not already set
    if attempt.status == "streaming":
        stderr_output = ""
        if proc.stderr:
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                stderr_output = stderr_bytes.decode()[:500]
            except (asyncio.TimeoutError, Exception):
                pass
        if proc.returncode == 0:
            attempt.status = "succeeded"
        else:
            attempt.status = "failed"
            attempt.error = f"Codex exit code {proc.returncode}: {stderr_output}"

    # Run after_run hook
    if hooks_cfg.after_run:
        from .workspace import run_hook

        await run_hook(
            hooks_cfg.after_run, workspace_path, hooks_cfg.timeout_ms, "after_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )

    # Unregister PID
    if on_pid and proc.pid:
        on_pid(proc.pid, False)

    logger.info(
        f"Codex turn complete issue={issue.identifier} "
        f"status={attempt.status}",
        extra={"linked_to": issue.identifier},
    )

    return attempt


async def run_agent_turn(
    claude_cfg: ClaudeConfig,
    hooks_cfg: HooksConfig,
    prompt: str,
    workspace_path: Path,
    issue: Issue,
    attempt: RunAttempt,
    on_event: EventCallback | None = None,
    on_pid: PidCallback | None = None,
    env: dict[str, str] | None = None,
    docker_cfg: DockerConfig | None = None,
    workspace_key: str = "",
    docker_image: str = "",
) -> RunAttempt:
    """Run a single Claude Code turn. Returns updated RunAttempt."""
    args = build_claude_args(
        claude_cfg, prompt, workspace_path, attempt.session_id,
        issue_identifier=issue.identifier,
    )

    # Docker wrapping
    args, container_name, sub_cwd, sub_env = _prepare_docker_args(
        docker_cfg, args, workspace_path, workspace_key, issue, attempt, env, docker_image
    )

    logger.info(
        f"Launching claude issue={issue.identifier} "
        f"session={attempt.session_id or 'new'} "
        f"turn={attempt.turn_count + 1}",
        extra={"linked_to": issue.identifier},
    )

    # Run before_run hook
    if hooks_cfg.before_run:
        from .workspace import run_hook

        ok = await run_hook(
            hooks_cfg.before_run, workspace_path, hooks_cfg.timeout_ms, "before_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )
        if not ok:
            attempt.status = "failed"
            attempt.error = "before_run hook failed"
            return attempt

    attempt.status = "streaming"
    attempt.started_at = attempt.started_at or datetime.now(timezone.utc)
    attempt.turn_count += 1
    attempt.last_event_at = datetime.now(timezone.utc)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=sub_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=10 * 1024 * 1024,  # 10MB line buffer (default 64KB)
            env=sub_env,
        )
        if on_pid and proc.pid:
            on_pid(proc.pid, True)
        attempt.pid = proc.pid
    except FileNotFoundError:
        attempt.status = "failed"
        attempt.error = "Docker command not found" if container_name else f"Claude command not found: {claude_cfg.command}"
        logger.error(attempt.error, extra={"linked_to": issue.identifier})
        return attempt

    # Stream stdout (NDJSON events)
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    stall_timeout_s = claude_cfg.stall_timeout_ms / 1000
    turn_timeout_s = claude_cfg.turn_timeout_ms / 1000

    async def read_stream():
        nonlocal last_activity
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            last_activity = loop.time()
            attempt.last_event_at = datetime.now(timezone.utc)

            line_str = line.decode().strip()
            if not line_str:
                continue

            try:
                event = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            process_event(event, attempt, on_event, issue.identifier)

    async def stall_monitor():
        while proc.returncode is None:
            await asyncio.sleep(min(stall_timeout_s / 4, 30))
            elapsed = loop.time() - last_activity
            if stall_timeout_s > 0 and elapsed > stall_timeout_s:
                logger.warning(
                    f"Stall detected issue={issue.identifier} "
                    f"elapsed={elapsed:.0f}s",
                    extra={"linked_to": issue.identifier},
                )
                proc.kill()
                if container_name:
                    asyncio.create_task(kill_container(container_name))
                attempt.status = "stalled"
                attempt.error = f"No output for {elapsed:.0f}s"
                return

    try:
        reader = asyncio.create_task(read_stream())
        monitor = asyncio.create_task(stall_monitor())

        # Overall turn timeout
        done, pending = await asyncio.wait(
            {reader, monitor},
            timeout=turn_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            # Turn timeout
            logger.warning(f"Turn timeout issue={issue.identifier}", extra={"linked_to": issue.identifier})
            proc.kill()
            if container_name:
                asyncio.create_task(kill_container(container_name))
            attempt.status = "timed_out"
            attempt.error = f"Turn exceeded {turn_timeout_s}s"
        else:
            # Wait for process to finish
            await asyncio.wait_for(proc.wait(), timeout=30)

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error(f"Runner error issue={issue.identifier}: {e}", extra={"linked_to": issue.identifier})
        proc.kill()
        attempt.status = "failed"
        attempt.error = str(e)
        return attempt

    # Determine final status from exit code if not already set by stall/timeout
    if attempt.status == "streaming":
        if proc.returncode == 0:
            attempt.status = "succeeded"
        else:
            stderr_output = ""
            if proc.stderr:
                try:
                    stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=5)
                    stderr_output = stderr_bytes.decode()[:500]
                except (asyncio.TimeoutError, Exception):
                    pass
            attempt.status = "failed"
            attempt.error = f"Exit code {proc.returncode}: {stderr_output}"

    # Run after_run hook
    if hooks_cfg.after_run:
        from .workspace import run_hook

        await run_hook(
            hooks_cfg.after_run, workspace_path, hooks_cfg.timeout_ms, "after_run",
            docker_cfg=docker_cfg, docker_image=docker_image, workspace_key=workspace_key,
        )

    # Unregister PID
    if on_pid and proc.pid:
        on_pid(proc.pid, False)

    logger.info(
        f"Turn complete issue={issue.identifier} "
        f"status={attempt.status} "
        f"tokens={attempt.total_tokens:,} "
        f"cost=${attempt.cost_usd:.2f} "
        f"tools={attempt.tool_call_count}",
        extra={"linked_to": issue.identifier},
    )

    return attempt


async def run_turn(
    runner_type: str,
    claude_cfg: ClaudeConfig,
    hooks_cfg: HooksConfig,
    prompt: str,
    workspace_path: Path,
    issue: Issue,
    attempt: RunAttempt,
    on_event: EventCallback | None = None,
    on_pid: PidCallback | None = None,
    env: dict[str, str] | None = None,
    docker_cfg: DockerConfig | None = None,
    workspace_key: str = "",
    docker_image: str = "",
) -> RunAttempt:
    """Route to the correct runner based on runner_type."""
    if runner_type == "codex":
        return await run_codex_turn(
            model=claude_cfg.model,
            hooks_cfg=hooks_cfg,
            prompt=prompt,
            workspace_path=workspace_path,
            issue=issue,
            attempt=attempt,
            on_pid=on_pid,
            turn_timeout_ms=claude_cfg.turn_timeout_ms,
            stall_timeout_ms=claude_cfg.stall_timeout_ms,
            env=env,
            docker_cfg=docker_cfg,
            workspace_key=workspace_key,
            docker_image=docker_image,
        )
    elif runner_type == "claude":
        return await run_agent_turn(
            claude_cfg=claude_cfg,
            hooks_cfg=hooks_cfg,
            prompt=prompt,
            workspace_path=workspace_path,
            issue=issue,
            attempt=attempt,
            on_event=on_event,
            on_pid=on_pid,
            env=env,
            docker_cfg=docker_cfg,
            workspace_key=workspace_key,
            docker_image=docker_image,
        )
    else:
        raise ValueError(f"Unknown runner type: {runner_type}")
