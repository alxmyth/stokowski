"""Main orchestration loop - polls Linear, dispatches agents, manages state."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import (
    Environment,
    StrictUndefined,
    TemplateSyntaxError,
    UndefinedError,
)

from .config import (
    ClaudeConfig,
    HooksConfig,
    ProjectConfig,
    RepoConfig,
    ServiceConfig,
    StateConfig,
    ParsedConfig,
    _resolve_linear_state_name,
    WorkflowConfig,
    _resolve_linear_state_name,
    merge_state_config,
    parse_workflow_file,
    validate_config,
)
from .docker_runner import (
    check_docker_available,
    cleanup_orphaned_containers,
    cleanup_orphaned_volumes,
    kill_container,
    pull_image,
)
from .linear import LinearClient
from .models import Issue, RetryEntry, RunAttempt
from .pool import ConcurrencyPool
from .prompt import (
    assemble_prompt,
    build_lifecycle_section,
    render_hook_template,
    render_hooks_for_dispatch,
)
from .runner import run_agent_turn, run_turn
from .tracking import (
    has_pending_rejection,
    make_gate_comment,
    make_migrated_comment,
    make_rejection_comment,
    make_state_comment,
    parse_latest_tracking,
)
from . import artifacts as artifacts_mod
from . import report as report_mod
from .events import summarise_tool_input
from .ledger import Ledger
from .workspace import ensure_workspace, remove_workspace, sanitize_key

logger = logging.getLogger("stokowski")


def _resolve_docker_image(
    state_cfg: StateConfig | None,
    repo: RepoConfig,
    platform_default: str,
) -> str:
    """3-level Docker image resolution (R8).

    Precedence, most-specific first:
    1. ``state_cfg.docker_image`` — team workflow stage declaration;
       wins when the stage is repo-agnostic and the team wants the same
       image everywhere.
    2. ``repo.docker_image`` — repo registry entry default; used for
       toolchain-bound stages on heterogeneous teams.
    3. ``platform_default`` — the configured ``docker.default_image``
       fallback.

    Returns an empty string when no image is configured at any level;
    the caller decides whether that's acceptable.
    """
    if state_cfg and state_cfg.docker_image:
        return state_cfg.docker_image
    if repo.docker_image:
        return repo.docker_image
    return platform_default or ""


def _render_hooks_best_effort(
    hooks: HooksConfig,
    repo: RepoConfig,
    synthesized: bool,
) -> HooksConfig:
    """Render hooks for teardown paths without aborting on UndefinedError.

    Used from remove_workspace call sites where a mid-flight config typo
    shouldn't prevent cleanup. The hook execution would fail anyway if the
    typo is real, but we prefer a clean cleanup path over re-raising here.
    """
    try:
        return render_hooks_for_dispatch(hooks, repo, synthesized)
    except UndefinedError as e:
        logger.warning(
            "Hook rendering failed during teardown for repo=%s: %s — "
            "passing raw hooks through (execution may fail)",
            repo.name, e,
        )
        return hooks


class Orchestrator:
    def __init__(
        self,
        workflow_path: str | Path,
        project_name: str | None = None,
        pool: ConcurrencyPool | None = None,
    ):
        """Run one project's dispatch loop.

        `project_name` selects which project block in the workflow file
        this orchestrator owns. If None, defaults to the first project
        (legacy single-project setups always have exactly one).

        `pool` is a shared ConcurrencyPool. When provided, slot decisions
        funnel through it so the global cap and per-project caps are
        honoured across all orchestrators. When None, falls back to the
        legacy behaviour of `agent.max_concurrent_agents - len(running)`.
        """
        self.workflow_path = Path(workflow_path)
        self.workflow: ParsedConfig | None = None
        self.project_name = project_name
        self.project: ProjectConfig | None = None
        self.pool = pool

        # Runtime state
        self.running: dict[str, RunAttempt] = {}  # issue_id -> RunAttempt
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}
        self.completed: set[str] = set()

        # Aggregate metrics
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_cache_creation_tokens: int = 0
        self.total_cache_read_tokens: int = 0
        self.total_tokens: int = 0
        self.total_cost_usd: float = 0.0
        self.total_tool_calls: int = 0
        self.total_seconds_running: float = 0
        # Most recent rate-limit window seen from any agent. Shared across the
        # whole account, so the latest observation is the accurate one.
        self.rate_limit: dict[str, Any] | None = None
        # Append-only record of runs and the human verdicts on them. In-memory
        # state is rebuilt from Linear on restart; this is the only thing that
        # remembers how previous work was judged.
        # Note: no `self.cfg` here — the workflow is not loaded until the
        # first tick, and `cfg` asserts on that. The ledger path is derived
        # from the workflow file alone, which is known at construction.
        self.ledger = Ledger.for_workflow(Path(self.workflow_path))

        # Internal
        self._linear: LinearClient | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._retry_timers: dict[str, asyncio.TimerHandle] = {}
        self._child_pids: set[int] = set()  # Track claude subprocess PIDs
        self._last_session_ids: dict[str, str] = {}  # issue_id -> last known session_id
        self._jinja = Environment(undefined=StrictUndefined)
        self._running = False
        self._last_issues: dict[str, Issue] = {}
        self._last_completed_at: dict[str, datetime] = {}  # issue_id -> last worker completion time

        # State machine tracking
        self._issue_current_state: dict[str, str] = {}
        # Which workflow each issue is running. Pinned on first dispatch and
        # kept for the issue's lifetime: editing labels mid-run must not switch
        # the pipeline under a working agent.
        self._issue_workflow: dict[str, str] = {}   # issue_id -> internal state name
        self._issue_state_runs: dict[str, int] = {}       # issue_id -> run number for current state
        # (issue_id, state, run) already announced in Linear. Two call sites
        # post the entering-a-state comment — the transition, and the worker
        # that picks the state up ~1s later — and a continuation can re-enter
        # the worker path with the same run. COG-368 got three identical
        # "Entering state: implement" comments in 1.7 seconds.
        self._announced_states: set[tuple[str, str, int]] = set()
        self._pending_gates: dict[str, str] = {}           # issue_id -> gate state name
        self._issue_workflow: dict[str, str] = {}          # issue_id -> workflow name
        self._issue_repo: dict[str, str] = {}              # issue_id -> repo name (multi-repo)

        # Eligible-but-not-dispatched (queue panel data, refreshed each tick)
        self._queued: list[dict] = []

        # Issues that currently hold a pool slot. Used to ensure each
        # try_claim has exactly one matching release, even when the
        # worker is cancelled mid-flight from multiple paths.
        self._slot_held: set[str] = set()

        # Cancellation tracking
        self._force_cancelled: set[str] = set()  # issue_ids cancelled by reconciliation
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks

        # R10 single-repo cap — the async rejection pre-pass populates this
        # set before the synchronous eligibility loop runs; _is_eligible
        # only observes membership (no I/O in the hot path). Label-change
        # invalidation clears entries so dispatch resumes when operators fix
        # the labels. Orchestrator restart loses the set, but the rejection
        # pre-pass repopulates it on the first tick by scanning tracking
        # comments via ``has_pending_rejection``.
        self._rejected_issues: set[str] = set()

        # Cold-start migration marker — tracks issue IDs for which we have
        # posted a one-time migrated-comment during this orchestrator run,
        # so a sequence of gate fetches on the same issue does not spam.
        self._migrated_issues: set[str] = set()

        # Config-error block set — populated when a dispatch fails due to a
        # permanent config problem (hook template typo, Jinja2 syntax error).
        # _is_eligible observes this to prevent retry loops on unrecoverable
        # errors. Cleared on every successful _load_workflow so dispatch
        # resumes when the operator fixes workflow.yaml. Also cleared on
        # issue-labels change (_last_issues diff) in case the issue was
        # moved to a different repo/workflow whose config is healthy.
        self._config_blocked: set[str] = set()

        # Rejection fetch-failure marker — when _process_rejections can't
        # fetch comments for a dual-label issue, we fail closed (add to
        # _rejected_issues) to prevent dispatching with arbitrary first-wins
        # repo routing. This companion set tracks "pessimistic" rejections
        # that should be re-evaluated on the next tick instead of sticking
        # until labels change. See ADV-003.
        self._rejection_fetch_pending: set[str] = set()

        # Prior-tick labels snapshot — captured BEFORE _last_issues is
        # updated on each tick. Consumed by _process_rejections to detect
        # label changes since the previous tick (see COR-001). Keyed by
        # issue id, holds the sorted lowercase label list from the prior
        # tick's Issue. Empty on the first tick / orchestrator restart
        # (which is correct: no prior tick to compare against).
        self._prev_issue_labels: dict[str, list[str]] = {}

    @property
    def cfg(self) -> ServiceConfig:
        assert self.workflow is not None
        return self.workflow.config

    def _project_view(self, full: WorkflowDefinition, project: ProjectConfig) -> WorkflowDefinition:
        """Build a per-project ServiceConfig view so existing self.cfg.X reads keep working."""
        project_cfg = ServiceConfig(
            tracker=project.tracker,
            polling=full.config.polling,
            workspace=project.workspace,
            hooks=project.hooks,
            claude=project.claude,
            agent=full.config.agent,
            server=full.config.server,
            linear_states=project.linear_states,
            prompts=project.prompts,
            states=project.states,
            # Without these the orchestrator sees no workflows at all, so every
            # issue silently falls back to the inline state machine and routing
            # never fires.
            workflows=project.workflows,
            routing=project.routing,
            projects=[project],
            workflow_dir=full.config.workflow_dir,
        )
        return WorkflowDefinition(config=project_cfg, prompt_template=full.prompt_template)

    def _load_workflow(self) -> list[str]:
        """Load/reload workflow file. Returns validation errors.

        On successful reload (no validation errors), clears
        ``_config_blocked`` — a config-error-blocked issue gets a fresh
        chance after the operator has updated workflow.yaml.
        """
        try:
            full = parse_workflow_file(self.workflow_path)
        except Exception as e:
            return [f"Workflow load error: {e}"]
        errors = validate_config(full.config)
        if errors:
            return errors

        # Resolve project
        if self.project_name is None:
            if not full.config.projects:
                return ["No projects defined"]
            project = full.config.projects[0]
            self.project_name = project.name
        else:
            project = next(
                (p for p in full.config.projects if p.name == self.project_name),
                None,
            )
            if project is None:
                return [f"Project '{self.project_name}' not found in workflow file"]

        self.workflow = self._project_view(full, project)
        self.project = project
        return []

    # ── Slot management ────────────────────────────────────────────────────

    # ── Workflow resolution ──────────────────────────────────────────────

    def _adopt_workflow_from_tracking(self, issue: Issue, tracking: dict | None) -> None:
        """Restore an issue's pinned workflow from its tracking comment.

        In-memory pins are lost on restart. Recovering from Linear keeps a
        long-running issue on the pipeline it started, rather than re-routing
        it from labels that may have changed since.
        """
        if not tracking:
            return
        name = tracking.get("workflow")
        if name and name in self.cfg.workflows:
            self._issue_workflow[issue.id] = name

    def _workflow_name_for(self, issue: Issue) -> str | None:
        """Resolve (and pin) the workflow an issue runs under.

        Pinned on first sight rather than re-read each tick, because labels are
        editable: a ticket relabelled halfway through implementation must not
        suddenly be evaluated against a different state machine.
        """
        pinned = self._issue_workflow.get(issue.id)
        if pinned and pinned in self.cfg.workflows:
            return pinned

        name = self.cfg.routing.resolve(issue.labels)
        if name and name in self.cfg.workflows:
            self._issue_workflow[issue.id] = name
            logger.info(
                f"Routed {issue.identifier} to workflow '{name}'"
                + (f" (labels: {', '.join(issue.labels)})" if issue.labels else " (no labels)"),
                extra={"linked_to": issue.identifier},
            )
            return name

        if name:
            logger.warning(
                f"{issue.identifier} routes to unknown workflow '{name}'",
                extra={"linked_to": issue.identifier},
            )
        return None

    def _workflow_for(self, issue: Issue):
        """The WorkflowSpec an issue runs under, or None."""
        name = self._workflow_name_for(issue)
        return self.cfg.workflows.get(name) if name else None

    def _entry_state_for(self, issue: Issue) -> str | None:
        """The entry state of the pipeline THIS issue runs under.

        `cfg.entry_state` answers a different question — the first agent state
        of the inline `states:` block. Every workflow but `bug-fix` happens to
        start with `investigate`, so using it looked correct until a
        bug-labelled ticket arrived and was started in a state `bug-fix` does
        not have.
        """
        workflow = self._workflow_for(issue)
        if workflow is not None:
            entry = workflow.entry_state
            if entry:
                return entry
        return self.cfg.entry_state

    def _states_for(self, issue: Issue) -> dict:
        """The state machine an issue runs under.

        Falls back to the config's inline states so a single-pipeline setup —
        and any issue whose routing failed — behaves exactly as before.
        """
        workflow = self._workflow_for(issue)
        return workflow.states if workflow else self.cfg.states


    def _has_slot(self) -> tuple[bool, str | None]:
        """Return (can_dispatch, reason_if_not). Considers pause + global cap."""
        name = self.project_name or ""
        if self.pool is not None:
            if self.pool.is_paused(name):
                return False, "paused"
            if self.pool.available_for(name) <= 0:
                return False, "no global slot"
            return True, None
        # Legacy single-project path (no shared pool)
        if max(self.cfg.agent.max_concurrent_agents - len(self.running), 0) <= 0:
            return False, "no global slot"
        return True, None

    def _claim_slot(self, issue_id: str) -> bool:
        """Claim a pool slot for this issue. No-op for legacy path."""
        if issue_id in self._slot_held:
            return True
        if self.pool is not None:
            if not self.pool.try_claim(self.project_name or ""):
                return False
        self._slot_held.add(issue_id)
        return True

    def _release_slot(self, issue_id: str) -> None:
        """Release a pool slot. Idempotent — safe to call from cancellation paths."""
        if issue_id not in self._slot_held:
            return
        self._slot_held.discard(issue_id)
        if self.pool is not None:
            self.pool.release(self.project_name or "")

    def _ensure_linear_client(self) -> LinearClient:
        if self._linear is None:
            self._linear = LinearClient(
                endpoint=self.cfg.tracker.endpoint,
                api_key=self.cfg.resolved_api_key(),
            )
        return self._linear

    # -- Kill / cleanup helpers --

    @staticmethod
    def _kill_pid(pid: int) -> None:
        """Kill a process by PID. Send SIGKILL to process group, fall back to individual kill.

        os.killpg(SIGKILL) is atomic for the process group. Claude Code runs
        with start_new_session=True (its own pgrp), so this kills the agent
        and any direct children.
        """
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    async def _kill_worker(self, issue_id: str, reason: str) -> None:
        """Kill a running worker: subprocess, container, then async task.

        Order matters: the subprocess must be killed BEFORE the task is
        cancelled, because CancelledError does not propagate to child processes.
        Handles missing RunAttempt gracefully (e.g., for gated issues).
        """
        attempt = self.running.get(issue_id)

        # 1. Kill subprocess PID (must happen first)
        if attempt and attempt.pid:
            self._kill_pid(attempt.pid)
            self._child_pids.discard(attempt.pid)

        # 2. Kill Docker container
        if attempt and attempt.container_name:
            try:
                await kill_container(attempt.container_name)
            except Exception as exc:
                logger.warning(
                    f"Failed to kill container {attempt.container_name}: {exc}"
                )

        # 3. Cancel async task last
        task = self._tasks.get(issue_id)
        if task and not task.done():
            task.cancel()

        logger.info(f"Killed worker issue={issue_id} reason={reason}")

    def _cleanup_issue_state(self, issue_id: str) -> None:
        """Remove all per-issue tracking state. Idempotent — safe to call multiple times.

        Any new per-issue state dict added to Orchestrator.__init__ must also
        be added here.
        """
        # -- Core state machine --
        self._issue_current_state.pop(issue_id, None)
        self._issue_state_runs.pop(issue_id, None)
        self._pending_gates.pop(issue_id, None)
        self._issue_workflow.pop(issue_id, None)
        self._issue_repo.pop(issue_id, None)
        self._rejected_issues.discard(issue_id)
        self._migrated_issues.discard(issue_id)
        self._config_blocked.discard(issue_id)
        self._rejection_fetch_pending.discard(issue_id)
        self._prev_issue_labels.pop(issue_id, None)
        # -- Session/timing --
        self._last_session_ids.pop(issue_id, None)
        self._last_completed_at.pop(issue_id, None)
        # -- Issue cache --
        self._last_issues.pop(issue_id, None)
        # -- Retry --
        timer = self._retry_timers.pop(issue_id, None)
        if timer is not None:
            timer.cancel()
        self.retry_attempts.pop(issue_id, None)
        # -- Dispatch --
        self.running.pop(issue_id, None)
        self._tasks.pop(issue_id, None)
        self.claimed.discard(issue_id)

    def _fire_and_forget(self, coro) -> None:
        """Schedule a coroutine without awaiting it. Prevents GC of the task."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _post_cancellation_comment(
        self, issue_id: str, state_name: str
    ) -> None:
        """Post a best-effort tracking comment when an issue is cancelled."""
        try:
            client = self._ensure_linear_client()
            await client.post_comment(
                issue_id,
                f"[Stokowski] Agent terminated — issue moved to {state_name}.",
            )
        except Exception as e:
            logger.debug(f"Failed to post cancellation comment for {issue_id}: {e}")

    async def _post_hook_error_comment(self, issue_id: str, detail: str) -> None:
        """Post a Linear comment when a hook template or hook execution fails.

        Hook-rendering errors are config typos (e.g. ``{{ repo.clne_url }}``);
        silent log-only errors leave operators staring at a stuck ticket with
        no visible cause. Posting the detail on the ticket surfaces the
        problem without requiring log access.
        """
        try:
            client = self._ensure_linear_client()
            await client.post_comment(
                issue_id, f"[Stokowski] {detail}"
            )
        except Exception as e:
            logger.debug(f"Failed to post hook-error comment for {issue_id}: {e}")

    async def _cleanup_logs(self) -> None:
        """Run log retention cleanup. Best-effort — errors logged and swallowed."""
        if not self.cfg.logging.enabled or not self.cfg.logging.log_dir:
            return
        try:
            log_dir = self.cfg.logging.resolved_log_dir(self.workflow_path.parent)
            if not log_dir.exists():
                return

            exempt = {
                self.running[iid].issue_identifier
                for iid in self.running
                if self.running[iid].issue_identifier
            }

            if self.cfg.logging.max_age_days > 0:
                cleanup_old_logs(log_dir, self.cfg.logging.max_age_days)
            if self.cfg.logging.max_total_size_mb > 0:
                enforce_size_limit(
                    log_dir, self.cfg.logging.max_total_size_mb, exempt
                )
        except Exception as e:
            logger.warning(f"Log retention cleanup failed: {e}")

    async def start(self):
        """Start the orchestration loop."""
        errors = self._load_workflow()
        if errors:
            for e in errors:
                logger.error(f"Config error: {e}")
            raise RuntimeError(f"Startup validation failed: {errors}")

        # Docker startup checks
        if self.cfg.docker.enabled:
            ok, msg = await check_docker_available()
            if not ok:
                raise RuntimeError(f"Docker mode enabled but: {msg}")
            # Pre-pull all configured images across the 3-level hybrid:
            # platform default + every state-level override + every
            # repo-level default. Missing an image here would force a cold
            # pull during the agent's first dispatch (no timeout wrapper),
            # so the union must stay in sync with resolve_docker_image.
            images: set[str] = set()
            if self.cfg.docker.default_image:
                images.add(self.cfg.docker.default_image)
            for sc in self.cfg.states.values():
                if sc.docker_image:
                    images.add(sc.docker_image)
            for repo in self.cfg.repos.values():
                if repo.docker_image:
                    images.add(repo.docker_image)
            for img in images:
                logger.info(f"Pulling Docker image: {img}")
                if not await pull_image(img):
                    logger.warning(f"Failed to pull image: {img} (may already be cached)")

        logger.info(
            f"Starting orchestrator "
            f"project={self.project_name} "
            f"slug={self.cfg.tracker.project_slug} "
            f"max_agents={self.cfg.agent.max_concurrent_agents} "
            f"poll_ms={self.cfg.polling.interval_ms}"
        )

        self._running = True
        self._stop_event = asyncio.Event()

        # Startup terminal cleanup
        await self._startup_cleanup()

        # Rebuild gates from Linear so the dashboard is accurate immediately
        # after restart, not only after Stokowski dispatches in-flight tickets
        await self._rebuild_gates_from_linear()

        # Main poll loop
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"Tick error: {e}")

            # Interruptible sleep
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.cfg.polling.interval_ms / 1000,
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # Normal poll interval elapsed

    async def stop(self):
        """Stop the orchestration loop and kill all running agents."""
        self._running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()

        # Kill all child claude processes first
        for pid in list(self._child_pids):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        self._child_pids.clear()

        # Kill Docker agent containers by label
        if self.cfg.docker.enabled:
            try:
                await cleanup_orphaned_containers()
            except Exception:
                pass

        # Cancel async tasks
        for issue_id, task in list(self._tasks.items()):
            task.cancel()
        # Give them a moment to finish
        if self._tasks:
            await asyncio.sleep(0.5)
        self._tasks.clear()

        if self._linear:
            await self._linear.close()

    async def _startup_cleanup(self):
        """Remove workspaces for issues already in terminal states."""
        if self.cfg.docker.enabled:
            count = await cleanup_orphaned_containers()
            if count:
                logger.info(f"Killed {count} orphaned agent containers")

        try:
            client = self._ensure_linear_client()
            terminal = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                self.cfg.terminal_linear_states(),
            )
            ws_root = self.cfg.workspace.resolved_root()
            # At startup we don't know which repo each terminal issue last ran
            # against (the _issue_repo cache is empty). Iterate every repo and
            # try to remove the composite-keyed workspace. remove_workspace is
            # idempotent — missing workspaces are a silent no-op. Per-repo hooks
            # are rendered best-effort so a hook template typo doesn't block
            # the cleanup sweep.
            for issue in terminal:
                for repo in self.cfg.repos.values():
                    rendered_hooks = _render_hooks_best_effort(
                        self.cfg.hooks, repo, self.cfg.repos_synthesized,
                    )
                    await remove_workspace(
                        ws_root, issue.identifier, repo.name, rendered_hooks,
                        docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                    )
        except Exception as e:
            logger.warning(f"Startup cleanup failed (continuing): {e}")

        # Prune orphaned Docker volumes that don't correspond to any currently
        # active issue. This catches (a) pre-upgrade flat-keyed volumes from
        # before the composite-key migration, and (b) stale volumes from
        # crashed orchestrator runs. The active_keys set contains the
        # composite key for every (issue, repo) pair an active issue could
        # currently occupy.
        if self.cfg.docker.enabled:
            try:
                active_keys: set[str] = set()
                # Seed with any currently-running or cached issues across all
                # configured repos. We don't know which repo each issue maps
                # to yet, so include the full cartesian product — false
                # negatives are safe, false positives are the bug we're
                # trying to avoid.
                for issue_id, issue_obj in self._last_issues.items():
                    for repo in self.cfg.repos.values():
                        from .workspace import compose_workspace_key
                        active_keys.add(
                            compose_workspace_key(issue_obj.identifier, repo.name).lower()
                        )
                removed = await cleanup_orphaned_volumes(self.cfg.docker, active_keys)
                if removed:
                    logger.info(
                        f"Pruned {removed} orphaned Docker workspace volume(s)"
                    )
            except Exception as e:
                logger.warning(f"Docker volume cleanup failed (continuing): {e}")

        await self._cleanup_logs()

    def _resolve_workflow(self, issue: Issue) -> WorkflowConfig:
        """Resolve which workflow applies to an issue and cache the result.

        Calls ``self.cfg.resolve_workflow(issue)`` (label matching + default
        fallback), then caches the workflow name in ``_issue_workflow``.
        """
        workflow = self.cfg.resolve_workflow(issue)
        self._issue_workflow[issue.id] = workflow.name
        return workflow

    def _get_issue_workflow_config(self, issue_id: str) -> WorkflowConfig:
        """Look up the cached workflow for an issue, with fallbacks.

        Resolution order:
        1. Cached name in ``_issue_workflow`` → look up via ``cfg.get_workflow``
        2. If cached name exists but workflow was removed (hot-reload) → re-resolve
           from the issue's labels (via ``_last_issues`` cache), or fall back to default
        3. If not cached at all → return the default workflow
        """
        cached_name = self._issue_workflow.get(issue_id)
        if cached_name is not None:
            wf = self.cfg.get_workflow(cached_name)
            if wf is not None:
                return wf
            # Workflow was removed by hot-reload — try to re-resolve from labels
            cached_issue = self._last_issues.get(issue_id)
            if cached_issue is not None:
                try:
                    return self._resolve_workflow(cached_issue)
                except ValueError:
                    pass  # No default — fall through
        # Not cached or resolution failed — return default workflow
        for wf in self.cfg.workflows.values():
            if wf.default:
                return wf
        # Last resort: return first workflow (should not happen with valid config)
        if self.cfg.workflows:
            return next(iter(self.cfg.workflows.values()))
        raise RuntimeError("No workflows defined in config")

    def _resolve_repo(self, issue: Issue) -> RepoConfig:
        """Resolve which repo applies to an issue and cache the result.

        Mirrors ``_resolve_workflow``. Calls ``self.cfg.resolve_repo(issue)``
        (label matching + default fallback), then caches the repo name in
        ``_issue_repo``.
        """
        repo = self.cfg.resolve_repo(issue)
        self._issue_repo[issue.id] = repo.name
        return repo

    def _repo_name_for_tracking(self, issue_id: str) -> str:
        """Return the repo name to carry in a tracking comment payload.

        Thin wrapper over ``_get_issue_repo_config`` that always returns a
        string (never None). Callers use this when building
        ``make_state_comment`` / ``make_gate_comment`` payloads.
        """
        return self._get_issue_repo_config(issue_id).name

    def _get_issue_repo_config(self, issue_id: str) -> RepoConfig:
        """Look up the cached repo for an issue, with fallbacks.

        Resolution order, mirroring ``_get_issue_workflow_config``:
        1. Cached name in ``_issue_repo`` → look up via ``cfg.repos``
        2. If cached name exists but repo was removed (hot-reload) → re-resolve
           from the issue's labels (via ``_last_issues`` cache)
        3. Not cached or resolution failed → return the default-marked repo
           (in legacy-synthesized mode this naturally resolves to the
           ``_default`` entry, since Unit 1's synthesis marks it ``default=True``)
        """
        cached_name = self._issue_repo.get(issue_id)
        if cached_name is not None:
            repo = self.cfg.repos.get(cached_name)
            if repo is not None:
                return repo
            # Repo was removed by hot-reload — try to re-resolve from labels
            cached_issue = self._last_issues.get(issue_id)
            if cached_issue is not None:
                try:
                    return self._resolve_repo(cached_issue)
                except ValueError:
                    pass  # No default — fall through
        # Not cached or resolution failed — return default-marked repo
        for repo in self.cfg.repos.values():
            if repo.default:
                return repo
        # Last resort: return first repo (should not happen with valid config)
        if self.cfg.repos:
            return next(iter(self.cfg.repos.values()))
        raise RuntimeError("No repos defined in config")

    def _resolve_gate_workflow(
        self, issue: Issue, tracking: dict | None
    ) -> WorkflowConfig:
        """Resolve workflow for a gated issue, handling cold-start recovery.

        Resolution order:
        1. Cached in ``_issue_workflow`` → use cached workflow
        2. Not cached (cold start after restart):
           a. If ``tracking`` has a ``workflow`` field and it exists in config
              → cache it and use it
           b. Otherwise → resolve from issue labels via ``_resolve_workflow()``

        This must be called before looking up transitions in gate handling,
        so that ``_issue_workflow`` is populated for downstream use.
        """
        # 1. Already cached — use it
        if issue.id in self._issue_workflow:
            return self._get_issue_workflow_config(issue.id)

        # 2. Cold start — try tracking comment's workflow field
        if tracking is not None:
            tracked_wf_name = tracking.get("workflow")
            if tracked_wf_name is not None:
                wf = self.cfg.get_workflow(tracked_wf_name)
                if wf is not None:
                    self._issue_workflow[issue.id] = wf.name
                    return wf

        # 3. Resolve from issue labels (default fallback)
        return self._resolve_workflow(issue)

    async def _resolve_current_state(self, issue: Issue) -> tuple[str, int]:
        """Resolve current state machine state for an issue.
        Returns (state_name, run).

        Workflow-aware: uses tracking comments to recover the workflow, then
        validates the tracked state against the resolved workflow's path.
        Respects cached ``_issue_workflow`` for claimed issues to prevent
        retry race conditions (a ``_tick()`` call must not overwrite the
        workflow while a ``call_later()`` retry is in-flight).
        """
        # Check state cache first
        if issue.id in self._issue_current_state:
            state_name = self._issue_current_state[issue.id]
            run = self._issue_state_runs.get(issue.id, 1)
            # Ensure _issue_workflow is populated (may be missing after restart)
            if issue.id not in self._issue_workflow:
                self._resolve_workflow(issue)
            return state_name, run

        # If the issue is in the Todo/pickup state, treat as fresh — ignore stale
        # tracking comments from a prior lifecycle (e.g., triage recycled the issue
        # back to Todo with a new label). Resolve workflow from labels directly.
        is_todo = (
            issue.state
            and issue.state.strip().lower() == self.cfg.linear_states.todo.strip().lower()
        )
        if is_todo:
            workflow = self._resolve_workflow(issue)
            entry = workflow.entry_state
            if not entry:
                raise RuntimeError(f"No entry state for workflow '{workflow.name}'")
            self._issue_current_state[issue.id] = entry
            self._issue_state_runs[issue.id] = 1
            return entry, 1

        # Fetch comments from Linear and parse latest tracking
        client = self._ensure_linear_client()
        comments = await client.fetch_comments(issue.id)
        tracking = parse_latest_tracking(comments)
        # Restore the pipeline this issue started on before interpreting its
        # state — a state name only means something inside its own workflow.
        self._adopt_workflow_from_tracking(issue, tracking)

        # --- Resolve workflow ---
        # Respect cached _issue_workflow for claimed issues (retry race prevention).
        # Only resolve fresh from labels for unclaimed issues.
        workflow: WorkflowConfig | None = None
        if issue.id in self._issue_workflow:
            workflow = self._get_issue_workflow_config(issue.id)
        elif tracking is not None:
            # Extract workflow field from tracking comment
            tracked_wf_name = tracking.get("workflow")
            if tracked_wf_name is not None:
                wf_from_tracking = self.cfg.get_workflow(tracked_wf_name)
                if wf_from_tracking is not None:
                    workflow = wf_from_tracking
                    self._issue_workflow[issue.id] = workflow.name
            # If tracking had no workflow field or the workflow no longer exists,
            # resolve from issue labels
            if workflow is None:
                workflow = self._resolve_workflow(issue)
        else:
            # No tracking at all — resolve from issue labels
            workflow = self._resolve_workflow(issue)

        entry = workflow.entry_state
        if not entry:
            raise RuntimeError(
                f"No entry state in workflow '{workflow.name}'"
            )

        # --- Cold-start repo recovery ---
        # If the issue was in-flight before this orchestrator started,
        # restore its repo assignment from tracking comments. Pre-migration
        # tracking comments have no `repo` field — post a one-time migrated
        # notice and fall back to the default-marked repo.
        await self._resolve_repo_for_coldstart(issue, tracking, comments)

        # No tracking → entry state, run 1
        if tracking is None:
            self._issue_current_state[issue.id] = entry
            self._issue_state_runs[issue.id] = 1
            return entry, 1

        # Helper: validate state exists in workflow path (not just states pool)
        workflow_path_set = set(workflow.path)

        if tracking["type"] == "state":
            state_name = tracking.get("state", entry)
            run = tracking.get("run", 1)
            if state_name in self.cfg.states and state_name in workflow_path_set:
                # State exists and is in workflow path — use it
                self._issue_current_state[issue.id] = state_name
                self._issue_state_runs[issue.id] = run
                return state_name, run
            # Unknown state → restart at this workflow's entry. Loud, because
            # a state that is not in the issue's own machine means the issue
            # was started on the wrong pipeline and has been going nowhere.
            logger.warning(
                f"Issue {issue.identifier} records state '{state_name}', which is "
                f"not in workflow '{self._workflow_name_for(issue)}' — restarting "
                f"at '{entry}'",
                extra={"linked_to": issue.identifier},
            )
            self._issue_current_state[issue.id] = entry
            self._issue_state_runs[issue.id] = 1
            return entry, 1

        if tracking["type"] == "gate":
            gate_state = tracking.get("state", "")
            status = tracking.get("status", "")
            run = tracking.get("run", 1)

            if status == "waiting":
                if gate_state in self.cfg.states and gate_state in workflow_path_set:
                    self._issue_current_state[issue.id] = gate_state
                    self._issue_state_runs[issue.id] = run
                    self._pending_gates[issue.id] = gate_state
                    return gate_state, run

            elif status == "approved":
                # Resolve approve transition using workflow transitions
                wf_transitions = workflow.transitions.get(gate_state, {})
                target = wf_transitions.get("approve")
                if not target:
                    # Fallback to StateConfig transitions for backward compat
                    gate_cfg = self.cfg.states.get(gate_state)
                    if gate_cfg:
                        target = gate_cfg.transitions.get("approve")
                if target and target in self.cfg.states:
                    self._issue_current_state[issue.id] = target
                    self._issue_state_runs[issue.id] = run
                    return target, run

            elif status == "rework":
                # Resolve rework_to using workflow transitions, then StateConfig fallback
                wf_transitions = workflow.transitions.get(gate_state, {})
                rework_to = tracking.get("rework_to", "")
                if not rework_to:
                    rework_to = wf_transitions.get("rework_to", "")
                if not rework_to:
                    gate_cfg = self.cfg.states.get(gate_state)
                    if gate_cfg:
                        rework_to = gate_cfg.rework_to or ""
                if rework_to and rework_to in self._states_for(issue):
                    self._issue_current_state[issue.id] = rework_to
                    self._issue_state_runs[issue.id] = run
                    return rework_to, run

        # Fallback to workflow entry state
        self._issue_current_state[issue.id] = entry
        self._issue_state_runs[issue.id] = 1
        return entry, 1

    async def _safe_enter_gate(self, issue: Issue, state_name: str):
        """Wrapper around _enter_gate that logs errors."""
        try:
            await self._enter_gate(issue, state_name)
        except Exception as e:
            logger.error(
                f"Enter gate failed issue={issue.identifier} "
                f"gate={state_name}: {e}",
                exc_info=True,
                extra={"linked_to": issue.identifier},
            )

    async def _enter_gate(self, issue: Issue, state_name: str):
        """Move issue to gate state and post tracking comment."""
        workflow = self._get_issue_workflow_config(issue.id)

        # Check for skip labels — auto-approve if any match
        if state_cfg and state_cfg.skip_labels:
            issue_labels_lower = [l.lower() for l in (issue.labels or [])]
            skip_labels_lower = [s.lower() for s in state_cfg.skip_labels]
            should_skip = any(sl in issue_labels_lower for sl in skip_labels_lower)

            # Resolve approve target from workflow transitions
            state_transitions = workflow.transitions.get(state_name, {})
            approve_target = state_transitions.get("approve")

            if should_skip and approve_target:
                target = approve_target
                run = self._issue_state_runs.get(issue.id, 1)

                client = self._ensure_linear_client()
                repo_name = self._repo_name_for_tracking(issue.id)
                comment = make_gate_comment(
                    state=state_name, status="approved", run=run,
                    workflow=workflow.name, repo=repo_name,
                )
                await client.post_comment(issue.id, comment)

                self._issue_current_state[issue.id] = target
                self._issue_state_runs[issue.id] = 1  # Reset for new state
                self.running.pop(issue.id, None)
                self._tasks.pop(issue.id, None)
                # Keep claimed — prevents double-dispatch race with concurrent tick
                self._pending_gates.pop(issue.id, None)

                # Post state-entry comment for audit trail
                state_comment = make_state_comment(
                    state=target, run=1, workflow=workflow.name,
                    repo=repo_name,
                )
                await client.post_comment(issue.id, state_comment)

                logger.info(
                    f"Gate auto-skipped issue={issue.identifier} "
                    f"gate={state_name} (label match) -> {target}"
                )
                self._schedule_retry(issue, attempt_num=0, delay_ms=1000)
                return

        state_cfg = self._states_for(issue).get(state_name)
        prompt = state_cfg.prompt if state_cfg else ""
        run = self._issue_state_runs.get(issue.id, 1)

        client = self._ensure_linear_client()

        comment = make_gate_comment(
            state=state_name,
            status="waiting",
            prompt=prompt or "",
            run=run,
            workflow=workflow.name,
            repo=self._repo_name_for_tracking(issue.id),
        )
        await client.post_comment(issue.id, comment)

        # Use the gate's own linear_state (per workflow.yaml), not a global one.
        # Previously this hardcoded `linear_states.review`, which forced every
        # gate entry — including `await_ci_and_review` — straight to "Human
        # Review", bypassing the CI+reviewer poller entirely.
        linear_key = state_cfg.linear_state if state_cfg else "review"
        target_linear_state = _resolve_linear_state_name(linear_key, self.cfg.linear_states)
        moved = await client.update_issue_state(issue.id, target_linear_state)
        if not moved:
            logger.error(
                f"Failed to move {issue.identifier} to gate linear state "
                f"'{target_linear_state}' (gate={state_name}) "
                f"— issue will remain claimed to prevent re-dispatch loop",
                extra={"linked_to": issue.identifier},
            )
            # Keep claimed so the issue doesn't get re-dispatched while
            # still in the active Linear state. Track the gate so
            # _handle_gate_responses can pick it up if the state is
            # changed manually.
            self._pending_gates[issue.id] = state_name
            self._issue_current_state[issue.id] = state_name
            self.running.pop(issue.id, None)
            self._tasks.pop(issue.id, None)
            self._release_slot(issue.id)
            # Schedule a retry to attempt the state move again
            self._schedule_retry(issue, attempt_num=0, delay_ms=10_000)
            return

        self._pending_gates[issue.id] = state_name
        self._issue_current_state[issue.id] = state_name
        # Release from running/claimed so it doesn't block slots
        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)
        self.claimed.discard(issue.id)
        self._release_slot(issue.id)

        logger.info(
            f"Gate entered issue={issue.identifier} gate={state_name} "
            f"run={run}",
            extra={"linked_to": issue.identifier},
        )

    async def _safe_transition(self, issue: Issue, transition_name: str):
        """Wrapper around _transition that logs errors instead of silently swallowing them."""
        try:
            await self._transition(issue, transition_name)
        except Exception as e:
            logger.error(
                f"Transition failed issue={issue.identifier} "
                f"transition={transition_name}: {e}",
                exc_info=True,
                extra={"linked_to": issue.identifier},
            )
            # Release claimed so the issue can be retried on next tick
            self.claimed.discard(issue.id)

    async def _transition(self, issue: Issue, transition_name: str):
        """Follow a transition from the current state.

        Uses workflow-specific transitions instead of StateConfig.transitions.

        Handles target types:
        - terminal → move to workflow-configured terminal state, clean workspace, release tracking
        - gate → enter gate
        - agent → post state comment, ensure active Linear state, schedule retry
        """
        current_state_name = self._issue_current_state.get(issue.id)
        if not current_state_name:
            self.claimed.discard(issue.id)
            logger.warning(f"No current state for {issue.identifier}, cannot transition", extra={"linked_to": issue.identifier})
            return

        current_cfg = self._states_for(issue).get(current_state_name)
        if not current_cfg:
            self.claimed.discard(issue.id)
            logger.warning(f"Unknown state '{current_state_name}' for {issue.identifier}", extra={"linked_to": issue.identifier})
            return

        # Resolve workflow-specific transitions for the current state
        workflow = self._get_issue_workflow_config(issue.id)
        state_transitions = workflow.transitions.get(current_state_name, {})

        target_name = state_transitions.get(transition_name)
        if not target_name:
            logger.warning(
                f"No '{transition_name}' transition from state '{current_state_name}' "
                f"for {issue.identifier}, falling back to 'complete'"
            )
            # Fall back to "complete" — the agent succeeded, don't lose its work
            target_name = state_transitions.get("complete")
            if not target_name:
                self.claimed.discard(issue.id)
                return
            transition_name = "complete"

        target_cfg = self._states_for(issue).get(target_name)
        if not target_cfg:
            logger.warning(f"Transition target '{target_name}' not found in config")
            self.claimed.discard(issue.id)
            return

        run = self._issue_state_runs.get(issue.id, 1)

        if target_cfg.type == "terminal":
            # Move issue to workflow-configured terminal Linear state
            terminal_key = workflow.terminal_state  # defaults to "terminal"
            terminal_state = _resolve_linear_state_name(terminal_key, self.cfg.linear_states)
            try:
                client = self._ensure_linear_client()
                moved = await client.update_issue_state(issue.id, terminal_state)
                if moved:
                    logger.info(f"Moved {issue.identifier} to terminal state '{terminal_state}'", extra={"linked_to": issue.identifier})
                else:
                    logger.warning(f"Failed to move {issue.identifier} to terminal state '{terminal_state}'", extra={"linked_to": issue.identifier})
            except Exception as e:
                logger.warning(f"Failed to move {issue.identifier} to terminal: {e}", extra={"linked_to": issue.identifier})
            self.ledger.record_terminal(
                project=self.project_name or "", issue_id=issue.id,
                issue=issue.identifier, state=terminal_state,
            )

            # Clean up workspace — resolve repo first (cache should be
            # populated from prior dispatch; falls back to default-marked
            # repo if the cache was cleared or never set).
            try:
                ws_root = self.cfg.workspace.resolved_root()
                repo = self._get_issue_repo_config(issue.id)
                rendered_hooks = _render_hooks_best_effort(
                    self.cfg.hooks, repo, self.cfg.repos_synthesized
                )
                await remove_workspace(
                    ws_root, issue.identifier, repo.name, rendered_hooks,
                    docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                )
            except Exception as e:
                logger.warning(f"Failed to remove workspace for {issue.identifier}: {e}", extra={"linked_to": issue.identifier})
            # Clean up tracking state
            self._issue_current_state.pop(issue.id, None)
            self._issue_state_runs.pop(issue.id, None)
            self._announced_states = {k for k in self._announced_states if k[0] != issue.id}
            self._pending_gates.pop(issue.id, None)
            self._last_session_ids.pop(issue.id, None)
            self.claimed.discard(issue.id)
            self.completed.add(issue.id)

        elif target_cfg.type == "gate":
            self._issue_current_state[issue.id] = target_name
            await self._enter_gate(issue, target_name)

        else:
            # Agent state — post state comment, ensure active Linear state, schedule retry
            self._issue_current_state[issue.id] = target_name

            # Run counter: increment for rework, reset for forward transitions
            if transition_name != "complete":
                run = run + 1
                self._issue_state_runs[issue.id] = run
            else:
                run = 1
                self._issue_state_runs[issue.id] = run

                workflow=workflow.name,
                repo=self._repo_name_for_tracking(issue.id),
            await self._announce_state(issue, target_name, run)

            # Ensure issue is in active Linear state
            client = self._ensure_linear_client()
            active_state = self.cfg.linear_states.active
            moved = await client.update_issue_state(issue.id, active_state)
            if not moved:
                logger.warning(f"Failed to move {issue.identifier} to active state '{active_state}'", extra={"linked_to": issue.identifier})

            self._schedule_retry(issue, attempt_num=0, delay_ms=1000)

    async def _announce_state(self, issue, state: str, run: int) -> None:
        """Post the entering-a-state comment, at most once per (state, run)."""
        key = (issue.id, state, run)
        if key in self._announced_states:
            return
        self._announced_states.add(key)
        client = self._ensure_linear_client()
        comment = make_state_comment(
            state=state,
            run=run,
            workflow=self._workflow_name_for(issue),
        )
        await client.post_comment(issue.id, comment)

    async def _handle_gate_responses(self):
        """Check for gate-approved and rework issues, handle transitions."""
        # Early return if no gate states in config
        has_gates = any(sc.type == "gate" for sc in self.cfg.all_states().values())
        if not has_gates:
            return

        client = self._ensure_linear_client()

        # Fetch gate-approved issues
        try:
            approved_issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                [self.cfg.linear_states.gate_approved],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch gate-approved issues: {e}")
            approved_issues = []

        for issue in approved_issues:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            gate_state = self._pending_gates.pop(issue.id, None)
            tracking: dict | None = None
            comments: list[dict] | None = None
            if not gate_state:
                # Cold-start fallback: fetch comments to find gate state
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_state = tracking.get("state", "")

            if gate_state:
                # Resolve workflow (cold-start recovery if needed)
                workflow = self._resolve_gate_workflow(issue, tracking)

                run = self._issue_state_runs.get(issue.id, 1)
                comment = make_gate_comment(
                    state=gate_state, status="approved", run=run,
                    workflow=workflow.name,
                )
                await client.post_comment(issue.id, comment)

                # Set current state to the gate so _transition can read FROM it,
                # then route through _transition. This dispatches the approve
                # transition through the existing target-type logic, which
                # correctly handles terminal targets (move to terminal Linear
                # state + clean up workspace), gate targets (enter the new
                # gate), and agent targets (post state comment + move to active
                # Linear state + schedule retry).
                #
                # Previously this branch unconditionally moved the Linear ticket
                # to `active` regardless of the approve target's type, which left
                # tickets stuck in `In Progress` forever for any workflow whose
                # gate transitions directly to a terminal state.
                self._issue_current_state[issue.id] = gate_state
                self._last_issues[issue.id] = issue
                # A gate decision IS the human verdict on the work. Recording
                # it here is what makes approval rate by classification and by
                # the agent's own stated confidence measurable later.
                self.ledger.record_gate(
                    workflow=self._workflow_name_for(issue),
                    project=self.project_name or "", issue_id=issue.id,
                    issue=issue.identifier, gate=gate_state,
                    verdict="approved", run=run,
                )
                await self._transition(issue, "approve")
                logger.info(f"Gate approved issue={issue.identifier} gate={gate_state}", extra={"linked_to": issue.identifier})
            else:
                logger.warning(
                    f"Issue {issue.identifier} is in "
                    f"'{self.cfg.linear_states.gate_approved}' but no gate could be "
                    f"resolved from its tracking comments — it will not advance",
                    extra={"linked_to": issue.identifier},
                )

        # Fetch rework issues
        try:
            rework_issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug,
                [self.cfg.linear_states.rework],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch rework issues: {e}")
            rework_issues = []

        for issue in rework_issues:
            if issue.id in self.running or issue.id in self.claimed:
                continue

            gate_state = self._pending_gates.pop(issue.id, None)
            tracking: dict | None = None
            comments: list[dict] | None = None
            if not gate_state:
                # Cold-start fallback: fetch comments to find gate state
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
                if tracking and tracking.get("type") == "gate" and tracking.get("status") == "waiting":
                    gate_state = tracking.get("state", "")

            if gate_state:
                # Resolve workflow (cold-start recovery if needed)
                workflow = self._resolve_gate_workflow(issue, tracking)

                gate_cfg = self._states_for(issue).get(gate_state)

                # Resolve rework_to from workflow transitions, then StateConfig fallback
                wf_transitions = workflow.transitions.get(gate_state, {})
                rework_to = wf_transitions.get("rework_to", "")
                if not rework_to:
                    rework_to = gate_cfg.rework_to if gate_cfg else ""
                if not rework_to:
                    logger.warning(f"Gate {gate_state} has no rework_to target, skipping")
                    continue

                # Check max_rework
                run = self._issue_state_runs.get(issue.id, 1)
                max_rework = gate_cfg.max_rework if gate_cfg else None
                if max_rework is not None and run >= max_rework:
                    # Exceeded max rework — post escalated comment, don't transition
                    comment = make_gate_comment(
                        state=gate_state, status="escalated", run=run,
                        workflow=workflow.name, repo=repo_name,
                    )
                    await client.post_comment(issue.id, comment)
                    logger.warning(
                        f"Max rework exceeded issue={issue.identifier} "
                        f"gate={gate_state} run={run} max={max_rework}",
                        extra={"linked_to": issue.identifier},
                    )
                    continue

                new_run = run + 1
                self._issue_state_runs[issue.id] = new_run

                # Recorded against the run that was rejected, not the retry, so
                # the verdict attaches to the work a human actually judged.
                self.ledger.record_gate(
                    workflow=self._workflow_name_for(issue),
                    project=self.project_name or "", issue_id=issue.id,
                    issue=issue.identifier, gate=gate_state,
                    verdict="rework", run=run,
                )

                comment = make_gate_comment(
                    state=gate_state, status="rework",
                    rework_to=rework_to, run=new_run,
                    workflow=workflow.name,
                )
                await client.post_comment(issue.id, comment)

                # Post state-entry comment for rework target
                state_comment = make_state_comment(
                    state=rework_to, run=new_run,
                    workflow=workflow.name,
                )
                await client.post_comment(issue.id, state_comment)

                self._issue_current_state[issue.id] = rework_to

                active_state = self.cfg.linear_states.active
                moved = await client.update_issue_state(issue.id, active_state)
                if moved:
                    issue.state = active_state
                else:
                    logger.warning(f"Failed to move {issue.identifier} to active after rework", extra={"linked_to": issue.identifier})
                self._last_issues[issue.id] = issue
                logger.info(
                    f"Rework issue={issue.identifier} gate={gate_state} "
                    f"rework_to={rework_to} run={new_run}",
                    extra={"linked_to": issue.identifier},
                )
            else:
                # A ticket parked in Rework with no resolvable gate goes nowhere
                # and nothing says so. Silence here is what let the comment
                # ordering bug sit unnoticed across every gate.
                logger.warning(
                    f"Issue {issue.identifier} is in '{self.cfg.linear_states.rework}' "
                    f"but no gate could be resolved from its tracking comments — "
                    f"it will not advance",
                    extra={"linked_to": issue.identifier},
                )

    async def _evict_terminal_gates(self):
        """Evict gate entries for tickets that have moved to terminal states.

        Runs each tick so stale Done/Canceled/Duplicate entries are removed
        within one poll cycle without requiring a manual POST /api/v1/refresh.
        This fixes the failure mode where tickets advanced to Done in Linear
        remain in the gates list indefinitely.
        """
        if not self._pending_gates:
            return

        gate_ids = list(self._pending_gates.keys())
        try:
            client = self._ensure_linear_client()
            states = await client.fetch_issue_states_by_ids(gate_ids)
        except Exception as e:
            logger.warning(f"Gate eviction state fetch failed: {e}")
            return

        terminal_lower = {s.strip().lower() for s in self.cfg.terminal_linear_states()}

        for issue_id in gate_ids:
            current_state = states.get(issue_id)
            if current_state is None:
                continue
            if current_state.strip().lower() in terminal_lower:
                gate_state = self._pending_gates.pop(issue_id, None)
                self._issue_current_state.pop(issue_id, None)
                self._issue_state_runs.pop(issue_id, None)
                self._announced_states = {k for k in self._announced_states if k[0] != issue_id}
                self._last_session_ids.pop(issue_id, None)
                self.claimed.discard(issue_id)
                ident = self._last_issues.get(
                    issue_id, Issue(id="", identifier=issue_id, title="")
                ).identifier
                logger.info(
                    f"Gate evicted issue={ident} was={gate_state} "
                    f"(moved to terminal: {current_state})",
                    extra={"linked_to": ident},
                )

    async def _rebuild_gates_from_linear(self):
        """Rebuild _pending_gates from Linear state on startup.

        Fetches all tickets currently in gate-flagged Linear states (Awaiting CI,
        Human Review, Rework) and reconstructs their gate tracking by reading the
        most recent stokowski tracking comment on each issue.  This ensures the
        dashboard is accurate immediately after a restart, not only after
        Stokowski itself dispatches them in the new process lifetime.

        Fallback: if no tracking comment is found, derives the gate state from
        the ticket's current Linear state by matching against configured gate
        states' linear_state keys.  This handles tickets that were moved to a
        gate-flagged state by an agent (via Linear MCP) before Stokowski's own
        gate-entry comment was posted.
        """
        gate_states = self.cfg.gate_linear_states()
        if not gate_states:
            return

        # Also include Rework — reworked tickets still hold pending gate context
        all_gate_states = list(gate_states)
        rework_state = self.cfg.linear_states.rework
        if rework_state and rework_state not in all_gate_states:
            all_gate_states.append(rework_state)

        try:
            client = self._ensure_linear_client()
            issues = await client.fetch_issues_by_states(
                self.cfg.tracker.project_slug, all_gate_states
            )
        except Exception as e:
            logger.warning(f"Gate rebuild from Linear failed: {e}")
            return

        rebuilt = 0
        for issue in issues:
            if issue.id in self._pending_gates:
                continue  # Already tracked

            # Store minimal issue record so the dashboard can show the identifier
            if issue.id not in self._last_issues:
                self._last_issues[issue.id] = issue

            # Try to recover gate state and run counter from tracking comments
            gate_state: str | None = None
            run = 1
            try:
                comments = await client.fetch_comments(issue.id)
                tracking = parse_latest_tracking(comments)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch comments for gate rebuild "
                    f"{issue.identifier}: {e}",
                    extra={"linked_to": issue.identifier},
                )
                tracking = None

            if tracking and tracking.get("type") == "gate":
                tracked_name = tracking.get("state", "")
                run = tracking.get("run", 1)
                if tracked_name in self._states_for(issue):
                    gate_state = tracked_name

            # Fallback: derive gate state from the current Linear state name by
            # matching against configured gate states' linear_state keys
            if not gate_state:
                current_linear = issue.state.strip().lower()
                for gname, gcfg in self._states_for(issue).items():
                    if gcfg.type == "gate":
                        gate_linear = _resolve_linear_state_name(
                            gcfg.linear_state, self.cfg.linear_states
                        )
                        if gate_linear.strip().lower() == current_linear:
                            gate_state = gname
                            run = 1
                            break

            if gate_state:
                self._pending_gates[issue.id] = gate_state
                self._issue_current_state[issue.id] = gate_state
                self._issue_state_runs[issue.id] = run
                rebuilt += 1
                logger.info(
                    f"Gate rebuilt issue={issue.identifier} "
                    f"gate={gate_state} run={run} (recovered from Linear)",
                    extra={"linked_to": issue.identifier},
                )

        if rebuilt:
            logger.info(f"Gate rebuild complete: {rebuilt} gate(s) recovered from Linear")

    async def _tick(self):
        """Single poll tick: reconcile, validate, fetch, dispatch."""
        # Reload workflow (supports hot-reload)
        errors = self._load_workflow()

        # Part 1: Reconcile running issues
        await self._reconcile()

        # Handle gate responses (approve / rework transitions)
        await self._handle_gate_responses()

        # Evict gate entries whose tickets have moved to terminal states
        await self._evict_terminal_gates()

        # Part 2: Validate config
        if errors:
            logger.warning(f"Config invalid, skipping dispatch: {errors}")
            return

        # Part 3: Fetch candidates
        try:
            client = self._ensure_linear_client()
            candidates = await client.fetch_candidate_issues(
                self.cfg.tracker.project_slug,
                self.cfg.active_linear_states(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch candidates: {e}")
            return

        # Snapshot prior-tick labels BEFORE updating _last_issues. The
        # rejection pre-pass needs the PREVIOUS tick's labels to detect a
        # label change (see COR-001: the stale-label invalidation in
        # _process_rejections was dead code when it compared against the
        # freshly-updated _last_issues).
        for issue in candidates:
            prior = self._last_issues.get(issue.id)
            if prior is not None:
                self._prev_issue_labels[issue.id] = sorted(
                    l.lower() for l in prior.labels
                )
            else:
                # No prior record — either the first tick we've seen this
                # issue, or the orchestrator just restarted. Either way,
                # there's no change to detect.
                self._prev_issue_labels.pop(issue.id, None)

        # Cache issues for retry lookup
        for issue in candidates:
            self._last_issues[issue.id] = issue

        # Part 4: Sort by priority
        candidates.sort(
            key=lambda i: (
                i.priority if i.priority is not None else 999,
                i.created_at or datetime.min.replace(tzinfo=timezone.utc),
                i.identifier,
            )
        )

        # Resolve state for new issues before dispatch
        for issue in candidates:
            if issue.id not in self._issue_current_state and issue.id not in self.running:
                try:
                    await self._resolve_current_state(issue)
                except Exception as e:
                    logger.warning(f"Failed to resolve state for {issue.identifier}: {e}", extra={"linked_to": issue.identifier})

        # Rejection pre-pass: R10 enforcement needs async I/O (Linear comment
        # fetch for dedup), but _is_eligible is synchronous. This pre-pass
        # populates _rejected_issues so the sync eligibility check is
        # I/O-free.
        await self._process_rejections(candidates)

        # Part 5: Dispatch
        self._queued = []  # reset per-tick queue snapshot

        for issue in candidates:
            if not self._is_eligible(issue):
                continue

            can, reason = self._has_slot()
            if not can:
                self._queued.append({
                    "issue_id": issue.id,
                    "issue_identifier": issue.identifier,
                    "title": issue.title,
                    "priority": issue.priority,
                    "state": issue.state,
                    "reason": reason or "blocked",
                })
                continue

            # Per-state concurrency check — use internal state machine name,
            # not Linear state name (multiple states share "In Progress")
            internal_state = self._issue_current_state.get(issue.id, "")
            state_limit = self.cfg.agent.max_concurrent_agents_by_state.get(internal_state)
            if state_limit is not None:
                state_count = sum(
                    1
                    for r in self.running.values()
                    if r.state_name == internal_state
                )
                if state_count >= state_limit:
                    self._queued.append({
                        "issue_id": issue.id,
                        "issue_identifier": issue.identifier,
                        "title": issue.title,
                        "priority": issue.priority,
                        "state": issue.state,
                        "reason": f"per-state cap ({state_key})",
                    })
                    continue

            # Reserve the global slot before _dispatch so the pool stays
            # consistent with what we believe is in flight.
            if not self._claim_slot(issue.id):
                self._queued.append({
                    "issue_id": issue.id,
                    "issue_identifier": issue.identifier,
                    "title": issue.title,
                    "priority": issue.priority,
                    "state": issue.state,
                    "reason": "no global slot",
                })
                continue

            self._dispatch(issue)

    async def _resolve_repo_for_coldstart(
        self,
        issue: Issue,
        tracking: dict | None,
        comments: list[dict],
    ) -> None:
        """Restore _issue_repo after a cold start, with migration handling.

        If the cache is already populated for this issue (fresh dispatch in
        this process), nothing to do.

        If tracking has a ``repo`` field (post-multi-repo tracking comment),
        use it — but verify the repo still exists in config. A hot-reload
        could have removed it; fall through to label resolution then.

        If tracking has no ``repo`` field (pre-multi-repo tracking comment,
        or the very first dispatch in this process), fall back to resolving
        via labels. If the result is the synthetic ``_default`` repo AND
        this is the first time we've processed this issue in the current
        process, post a one-time ``stokowski:migrated`` comment so the
        cold-start decision is recorded.
        """
        if issue.id in self._issue_repo:
            return

        # Defensive read — payload.get() never direct-subscript
        tracked_repo_name: str | None = None
        if tracking is not None:
            tracked_repo_name = tracking.get("repo")

        if tracked_repo_name is not None:
            repo = self.cfg.repos.get(tracked_repo_name)
            if repo is not None:
                self._issue_repo[issue.id] = repo.name
                return
            # Tracked repo no longer in config — fall through to labels.
            logger.info(
                f"Cold-start: tracked repo '{tracked_repo_name}' no longer "
                f"in config for {issue.identifier}; re-resolving from labels"
            )

        # Resolve from labels
        try:
            resolved = self.cfg.resolve_repo(issue)
        except ValueError:
            # Shouldn't happen with a validated config — log and bail
            logger.warning(
                f"Cold-start: resolve_repo failed for {issue.identifier}; "
                f"skipping repo cache population"
            )
            return

        self._issue_repo[issue.id] = resolved.name

        # Handle pre-multi-repo tracking (tracking exists but has no repo
        # field). Two post-migration actions, in priority order:
        #   (1) Post a one-time stokowski:migrated comment so the recovery
        #       decision is auditable on the ticket thread.
        #   (2) Log a warning if the issue also has a repo:* label that
        #       didn't match any configured repo (could indicate config
        #       drift since the issue was last dispatched).
        #
        # KP-06: the previous structure ran these as two independent blocks
        # with the same guard, which made the warning in (2) dead code on
        # the (1)-success path (because (1) added issue.id to
        # _migrated_issues, invalidating the "not in _migrated_issues"
        # guard in (2)). Restructured as an if/elif so both paths are
        # reachable: warning fires when the migration comment post fails.
        pre_migration = (
            tracking is not None
            and tracking.get("repo") is None
            and resolved.name == "_default"
        )
        if pre_migration and issue.id not in self._migrated_issues:
            try:
                client = self._ensure_linear_client()
                await client.post_comment(
                    issue.id, make_migrated_comment(resolved.name),
                )
                self._migrated_issues.add(issue.id)
            except Exception as e:
                logger.debug(
                    f"Failed to post migrated comment for "
                    f"{issue.identifier}: {e}"
                )
                # Fall through to the operator-facing warning below so at
                # least the log has a signal that we hit the migration
                # path. The warning is cheaper than the comment and costs
                # nothing on repeat ticks.

        if pre_migration:
            has_repo_label = any(
                l.lower().startswith("repo:") for l in (issue.labels or [])
            )
            if has_repo_label and issue.id not in self._migrated_issues:
                logger.warning(
                    f"Cold-start: {issue.identifier} has repo:* label(s) "
                    f"but fell back to _default — labels: {issue.labels}"
                )

    async def _process_rejections(self, issues: list[Issue]) -> None:
        """Async pre-pass: enforce R10 single-repo cap for each candidate.

        For each issue:
        1. Count ``repo:*`` labels on ``issue.labels``.
        2. If > 1 AND the issue was previously rejected with a DIFFERENT
           label set (labels were edited), discard the prior marker so we
           can re-evaluate against the new labels on this tick.
        3. If > 1 AND no marker exists, fetch the comment thread, check
           for an existing rejection sentinel via ``has_pending_rejection``
           against the current sorted label set, post a new sentinel iff
           none exists, then add ``issue.id`` to ``_rejected_issues``.
        4. If <= 1, ensure ``issue.id`` is NOT in ``_rejected_issues`` —
           operator fixed the labels, dispatch can proceed.

        Failures to fetch comments are logged but do not populate the set
        (err on the side of allowing dispatch rather than silently
        stalling the ticket).
        """
        if not issues:
            return

        client = self._ensure_linear_client()

        for issue in issues:
            current_labels = [l.lower() for l in (issue.labels or [])]
            repo_labels = [l for l in current_labels if l.startswith("repo:")]

            # Clear stale markers when labels changed since the prior tick.
            # _prev_issue_labels is captured at the top of _tick BEFORE
            # _last_issues is updated, so the comparison is against the
            # PREVIOUS tick's labels, not the current tick's (COR-001).
            prior_labels = self._prev_issue_labels.get(issue.id)
            if (
                issue.id in self._rejected_issues
                and prior_labels is not None
                and prior_labels != sorted(current_labels)
            ):
                self._rejected_issues.discard(issue.id)
                self._rejection_fetch_pending.discard(issue.id)

            # Re-evaluate pessimistic rejections (fetch failures from prior
            # ticks) regardless of label change — the Linear outage may have
            # cleared and we should re-attempt the dedup check.
            if issue.id in self._rejection_fetch_pending:
                self._rejected_issues.discard(issue.id)
                self._rejection_fetch_pending.discard(issue.id)

            if len(repo_labels) > 1:
                if issue.id in self._rejected_issues:
                    # Already handled on an earlier tick; nothing to do.
                    continue

                # Check existing comments to avoid duplicate rejection posts
                try:
                    comments = await client.fetch_comments(issue.id)
                except Exception as e:
                    # Fail-closed (ADV-003): without knowing whether a prior
                    # rejection comment exists, dispatching a dual-labeled
                    # ticket would commit an arbitrary first-wins repo
                    # routing to the tracking thread — worse than a
                    # temporary stall. Mark rejected AND flag for retry so
                    # the next tick re-attempts the fetch regardless of
                    # whether labels changed.
                    logger.error(
                        f"Rejection pre-pass: failed to fetch comments for "
                        f"{issue.identifier}: {e}. Failing closed — will "
                        f"retry on next tick."
                    )
                    self._rejected_issues.add(issue.id)
                    self._rejection_fetch_pending.add(issue.id)
                    continue

                if not has_pending_rejection(comments, current_labels):
                    # Detect triage origin: if the most recent state-tracking
                    # comment's workflow field is a triage workflow, attribute
                    # the conflict to triage rather than human labeling.
                    tracking = parse_latest_tracking(comments)
                    reason = "multi_repo"
                    if tracking and tracking.get("type") == "state":
                        wf_name = tracking.get("workflow")
                        if wf_name:
                            wf = self.cfg.get_workflow(wf_name)
                            if wf and wf.triage:
                                reason = "triage_multi_repo"
                    comment_body = make_rejection_comment(
                        current_labels, reason=reason
                    )
                    try:
                        await client.post_comment(issue.id, comment_body)
                        logger.info(
                            f"R10 rejection posted for {issue.identifier} "
                            f"(labels={repo_labels}, reason={reason})"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to post rejection comment for "
                            f"{issue.identifier}: {e}"
                        )
                        # Do NOT mark as rejected if the post failed —
                        # next tick will retry.
                        continue

                self._rejected_issues.add(issue.id)
            else:
                # <= 1 repo label: dispatch may proceed. Clear any stale
                # marker left over from a prior dual-label state.
                self._rejected_issues.discard(issue.id)

    def _is_eligible(self, issue: Issue) -> bool:
        """Check if an issue is eligible for dispatch."""
        if not issue.id or not issue.identifier or not issue.title or not issue.state:
            return False

        # R10: more than one repo:* label → ineligible. The async pre-pass
        # (see _process_rejections) has already done the Linear work of
        # posting a dedup'd rejection comment; we just observe the set here.
        if issue.id in self._rejected_issues:
            return False

        # Config-error block: hook template rendering failed on a prior
        # dispatch. Retrying won't fix a config typo; _load_workflow clears
        # this set when the operator reloads a valid workflow.yaml.
        if issue.id in self._config_blocked:
            return False

        state_lower = issue.state.strip().lower()
        active_lower = [s.strip().lower() for s in self.cfg.active_linear_states()]
        terminal_lower = [s.strip().lower() for s in self.cfg.terminal_linear_states()]

        if state_lower not in active_lower:
            return False
        if state_lower in terminal_lower:
            return False
        if issue.id in self.running:
            return False
        if issue.id in self.claimed:
            return False

        # Blocker check for Todo
        if state_lower == "todo":
            for blocker in issue.blocked_by:
                if blocker.state and blocker.state.strip().lower() not in terminal_lower:
                    return False

        return True

    def _dispatch(self, issue: Issue, attempt_num: int | None = None):
        """Dispatch a worker for an issue."""
        self.claimed.add(issue.id)

        state_name = self._issue_current_state.get(issue.id)
        if not state_name:
            state_name = self._get_issue_workflow_config(issue.id).entry_state or self.cfg.entry_state

        # If at a gate, enter it instead of dispatching a worker.
        # Release the slot we reserved — the gate path doesn't run an agent.
        state_cfg = self._states_for(issue).get(state_name) if state_name else None
        if state_cfg and state_cfg.type == "gate":
            self._release_slot(issue.id)
            asyncio.create_task(self._safe_enter_gate(issue, state_name))
            return

        attempt = RunAttempt(
            issue_id=issue.id,
            issue_identifier=issue.identifier,
            attempt=attempt_num,
            state_name=state_name,
        )

        # Session handling
        use_fresh_session = False
        if state_cfg and state_cfg.session == "fresh":
            use_fresh_session = True

        if not use_fresh_session:
            if issue.id in self.running:
                old = self.running[issue.id]
                if old.session_id:
                    attempt.session_id = old.session_id
            elif issue.id in self._last_session_ids:
                attempt.session_id = self._last_session_ids[issue.id]

        self.running[issue.id] = attempt
        task = asyncio.create_task(self._run_worker(issue, attempt))
        self._tasks[issue.id] = task

        runner = state_cfg.runner if state_cfg else "claude"
        logger.info(
            f"Dispatched issue={issue.identifier} "
            f"state={issue.state} "
            f"machine_state={state_name or 'entry'} "
            f"runner={runner} "
            f"session={'fresh' if use_fresh_session else 'inherit'} "
            f"attempt={attempt_num}",
            extra={"linked_to": issue.identifier},
        )

    async def _run_worker(self, issue: Issue, attempt: RunAttempt):
        """Worker coroutine: prepare workspace, run agent turns.

        Hot-reload race (ADV-005): ``self.cfg`` is mutated by
        ``_load_workflow`` at every tick. If a concurrent tick runs while
        this coroutine is awaiting, subsequent ``self.cfg`` reads inside
        this worker would see a fresh (possibly different) config. To pin
        the worker to the config it was dispatched under, we snapshot
        ``self.cfg`` into a local ``cfg`` before the first await and use
        it for all repo/workflow lookups below. ``self.cfg`` is still
        safe for pure state reads that tolerate hot-reload.
        """
        cfg = self.cfg  # snapshot — see ADV-005
        try:
            # Resolve state if not set
            if not attempt.state_name:
                state_name, run = await self._resolve_current_state(issue)
                attempt.state_name = state_name
                state_cfg = cfg.states.get(state_name)
                if state_cfg and state_cfg.type == "gate":
                    # Issue should be at a gate, not running
                    await self._enter_gate(issue, state_name)
                    return

            state_name = attempt.state_name
            state_cfg = self._states_for(issue).get(state_name) if state_name else None

            claude_cfg = self.cfg.claude
            hooks_cfg = self.cfg.hooks
            runner_type = "claude"

            if state_cfg:
                claude_cfg, hooks_cfg = merge_state_config(
                    state_cfg, self.cfg.claude, self.cfg.hooks
                )
                runner_type = state_cfg.runner

            ws_root = cfg.workspace.resolved_root()
            # Resolve repo using the pinned cfg snapshot (ADV-005). A
            # concurrent _load_workflow during our await could have swapped
            # self.cfg; resolving via `cfg.resolve_repo` guarantees the
            # repo corresponds to the config we started this dispatch
            # against. _issue_repo cache is still updated so subsequent
            # ticks see the resolution.
            try:
                repo = cfg.resolve_repo(issue)
                self._issue_repo[issue.id] = repo.name
            except ValueError as e:
                # No default repo configured — config got corrupted by a
                # hot-reload between dispatch and here, or the operator
                # removed the default. Treat as config_error.
                msg = (
                    f"Repo resolution failed for {issue.identifier}: {e}. "
                    f"No matching `repo:*` label and no default repo in "
                    f"the current config."
                )
                logger.error(msg)
                self._fire_and_forget(self._post_hook_error_comment(issue.id, msg))
                attempt.status = "config_error"
                attempt.error = str(e)
                self._on_worker_exit(issue, attempt)
                return
            docker_image = ""
            if self.cfg.docker.enabled:
                docker_image = _resolve_docker_image(
                    state_cfg, repo, self.cfg.docker.default_image,
                )
            # Render root hooks with repo metadata when the config has an
            # explicit repos: section. Legacy configs (repos_synthesized)
            # bypass rendering — preserves R19 backward compat for hook
            # bodies containing literal {}/${ shell syntax.
            try:
                rendered_hooks = render_hooks_for_dispatch(
                    self.cfg.hooks, repo, self.cfg.repos_synthesized
                )
            except (UndefinedError, TemplateSyntaxError) as render_err:
                # Config-level problem in a hook template:
                # - UndefinedError: a variable reference like {{ repo.clne_url }} (typo)
                # - TemplateSyntaxError: a `{{ ` / `{% ` that Jinja2 can't parse
                #   (common when migrating a legacy config with Jinja2-conflicting
                #   shell syntax from 1:1 to multi-repo mode).
                # Surface on Linear + mark status='config_error' so _on_worker_exit
                # routes to _config_blocked (no retry loop) instead of scheduling
                # exponential backoff against a broken template.
                if isinstance(render_err, UndefinedError):
                    hint = (
                        "This typically means a typo in a Jinja2 variable "
                        "reference (e.g. `{{ repo.clne_url }}` instead of "
                        "`{{ repo.clone_url }}`)."
                    )
                else:
                    hint = (
                        "Jinja2 could not parse the hook body. Common cause: "
                        "migrating a legacy 1:1 config with shell syntax like "
                        "`!f() {{ ...; }}; f` or literal `{{ `/`{% ` tokens to "
                        "multi-repo mode, which activates Jinja2 rendering. "
                        "Either escape the tokens (`{{ '{{' }}`) or rework the "
                        "hook body."
                    )
                msg = (
                    f"Hook template rendering failed for "
                    f"{issue.identifier} (repo={repo.name}): "
                    f"{type(render_err).__name__}: {render_err}. {hint}"
                )
                logger.error(msg)
                self._fire_and_forget(self._post_hook_error_comment(issue.id, msg))
                attempt.status = "config_error"
                attempt.error = f"hook template error: {render_err}"
                self._on_worker_exit(issue, attempt)
                return
            ws = await ensure_workspace(
                ws_root, issue.identifier, repo.name, rendered_hooks,
                docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                docker_image=docker_image,
            )
            attempt.workspace_path = str(ws.path)

            # Evidence directory, created fresh each turn and excluded from git
            # locally so agent screenshots can never reach the project repo.
            artifact_path = artifacts_mod.prepare(ws.path)
            # A stale report from a previous stage would otherwise be re-posted
            # verbatim as if it described this one.
            report_mod.discard(ws.path)

            # Move issue from Todo to In Progress if needed
            todo_state = self.cfg.linear_states.todo
            if todo_state and issue.state.strip().lower() == todo_state.strip().lower():
                try:
                    client = self._ensure_linear_client()
                    active_state = self.cfg.linear_states.active
                    moved = await client.update_issue_state(issue.id, active_state)
                    if moved:
                        issue.state = active_state
                        logger.info(
                            f"Moved {issue.identifier} from '{todo_state}' to '{active_state}'",
                            extra={"linked_to": issue.identifier},
                        )
                    else:
                        logger.warning(
                            f"Failed to move {issue.identifier} from '{todo_state}' to '{active_state}' "
                            f"— Linear API returned failure",
                            extra={"linked_to": issue.identifier},
                        )
                except Exception as e:
                    logger.warning(f"Failed to move {issue.identifier} to active: {e}", extra={"linked_to": issue.identifier})

            # Post state tracking comment (only for first dispatch of a state)
            if state_name:
                run = self._issue_state_runs.get(issue.id, 1)
                if attempt.attempt is None or attempt.attempt == 0:
                    await self._announce_state(issue, state_name, run)

            # Run on_stage_enter hook if defined. Render over repo metadata
            # (same gate as root hooks — only when repos: section is explicit).
            if state_cfg and state_cfg.hooks and state_cfg.hooks.on_stage_enter:
                from .workspace import run_hook
                try:
                    stage_enter_script = (
                        state_cfg.hooks.on_stage_enter
                        if self.cfg.repos_synthesized
                        else render_hook_template(
                            state_cfg.hooks.on_stage_enter, repo
                        )
                    )
                except (UndefinedError, TemplateSyntaxError) as render_err:
                    msg = (
                        f"on_stage_enter hook template rendering failed for "
                        f"{issue.identifier} state={state_name} repo={repo.name}: "
                        f"{type(render_err).__name__}: {render_err}"
                    )
                    logger.error(msg)
                    self._fire_and_forget(
                        self._post_hook_error_comment(issue.id, msg)
                    )
                    attempt.status = "config_error"
                    attempt.error = f"hook template error: {render_err}"
                    self._on_worker_exit(issue, attempt)
                    return
                ok = await run_hook(
                    stage_enter_script,
                    ws.path,
                    (state_cfg.hooks.timeout_ms if state_cfg.hooks else self.cfg.hooks.timeout_ms),
                    f"on_stage_enter:{state_name}",
                    docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                    docker_image=docker_image,
                    workspace_key=ws.workspace_key,
                )
                if not ok:
                    attempt.status = "failed"
                    attempt.error = f"on_stage_enter hook failed for state {state_name}"
                    self._on_worker_exit(issue, attempt)
                    return

            prompt = await self._render_prompt_async(issue, attempt.attempt, state_name)

            # Build env vars for the agent subprocess from workflow.yaml config
            if self.cfg.docker.enabled:
                agent_env = self.cfg.docker_env()
            else:
                agent_env = self.cfg.agent_env()
            agent_env["STOKOWSKI_ARTIFACTS"] = str(artifact_path)
            agent_env["STOKOWSKI_ISSUE"] = issue.identifier
            if state_name:
                agent_env["STOKOWSKI_STATE"] = state_name
            agent_env["STOKOWSKI_ISSUE_IDENTIFIER"] = issue.identifier
            # Per-dispatch repo env vars (R16). STOKOWSKI_REPO_NAME is always
            # set (it carries the sentinel "_default" for legacy configs);
            # STOKOWSKI_REPO_CLONE_URL is set only when non-empty so hooks
            # can test its presence to distinguish "no repo metadata" from
            # "repo with empty URL".
            agent_env["STOKOWSKI_REPO_NAME"] = repo.name
            if repo.clone_url:
                agent_env["STOKOWSKI_REPO_CLONE_URL"] = repo.clone_url
            # Triage workflow dispatch: inject the tenant-scoped repo list so
            # the triage agent can emit repo:<name> labels (R17, R18a).
            # Non-triage dispatches do not receive the list — agents on
            # specific repos don't need the full registry.
            workflow_for_env = self._get_issue_workflow_config(issue.id)
            if workflow_for_env.triage:
                repo_list = [
                    {
                        "name": r.name,
                        "label": r.label or "",
                        "clone_url": r.clone_url or "",
                    }
                    for r in self.cfg.repos.values()
                    if r.name != "_default"
                ]
                agent_env["STOKOWSKI_REPOS_JSON"] = json.dumps(repo_list)

            # Build log path if logging enabled
            log_path = None
            if self.cfg.logging.enabled and self.cfg.logging.log_dir:
                log_dir = self.cfg.logging.resolved_log_dir(self.workflow_path.parent)
                issue_log_dir = log_dir / sanitize_key(issue.identifier)
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                ext = ".log" if runner_type == "codex" else ".ndjson"
                log_path = issue_log_dir / f"{timestamp}-turn-{attempt.turn_count + 1}{ext}"

            # State machine mode: single turn per dispatch. The state
            # machine handles continuation via _transition after each
            # turn completes — multi-turn loops would bypass gate
            # transitions and cause the agent to blow past stage
            # boundaries.
            if state_name and state_cfg:
                attempt = await run_turn(
                    runner_type=runner_type,
                    claude_cfg=claude_cfg,
                    hooks_cfg=hooks_cfg,
                    prompt=prompt,
                    workspace_path=ws.path,
                    issue=issue,
                    attempt=attempt,
                    on_event=self._on_agent_event,
                    on_pid=self._on_child_pid,
                    env=agent_env,
                    docker_cfg=self.cfg.docker,
                    workspace_key=ws.workspace_key,
                    docker_image=docker_image,
                    log_path=log_path,
                )
            else:
                # Legacy mode: multi-turn loop
                max_turns = claude_cfg.max_turns
                for turn in range(max_turns):
                    if turn > 0:
                        current_state = issue.state
                        try:
                            client = self._ensure_linear_client()
                            states = await client.fetch_issue_states_by_ids([issue.id])
                            current_state = states.get(issue.id, issue.state)
                            state_lower = current_state.strip().lower()
                            active_lower = [
                                s.strip().lower() for s in self.cfg.active_linear_states()
                            ]
                            if state_lower not in active_lower:
                                logger.info(
                                    f"Issue {issue.identifier} no longer active "
                                    f"(state={current_state}), stopping",
                                    extra={"linked_to": issue.identifier},
                                )
                                break
                        except Exception as e:
                            logger.warning(f"State check failed, continuing: {e}")

                        prompt = (
                            f"Continue working on {issue.identifier}. "
                            f"The issue is still in '{current_state}' state. "
                            f"Check your progress and continue the task."
                        )

                    # Recalculate log path per turn in legacy mode
                    turn_log_path = None
                    if self.cfg.logging.enabled and self.cfg.logging.log_dir:
                        log_dir = self.cfg.logging.resolved_log_dir(self.workflow_path.parent)
                        issue_log_dir = log_dir / sanitize_key(issue.identifier)
                        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                        ext = ".log" if runner_type == "codex" else ".ndjson"
                        turn_log_path = issue_log_dir / f"{timestamp}-turn-{attempt.turn_count + 1}{ext}"

                    attempt = await run_turn(
                        runner_type=runner_type,
                        claude_cfg=claude_cfg,
                        hooks_cfg=hooks_cfg,
                        prompt=prompt,
                        workspace_path=ws.path,
                        issue=issue,
                        attempt=attempt,
                        on_event=self._on_agent_event,
                        on_pid=self._on_child_pid,
                        env=agent_env,
                        docker_cfg=self.cfg.docker,
                        workspace_key=ws.workspace_key,
                        docker_image=docker_image,
                        log_path=turn_log_path,
                    )

                    if attempt.status != "succeeded":
                        break

            await self._publish_run_report(issue, attempt, ws.path, state_name)

            self._on_worker_exit(issue, attempt)

        except asyncio.CancelledError:
            logger.info(f"Worker cancelled issue={issue.identifier}", extra={"linked_to": issue.identifier})
            attempt.status = "canceled"
            self._on_worker_exit(issue, attempt)
        except Exception as e:
            logger.error(f"Worker error issue={issue.identifier}: {e}", extra={"linked_to": issue.identifier})
            attempt.status = "failed"
            attempt.error = str(e)
            self._on_worker_exit(issue, attempt)

    async def _render_prompt_async(
        self, issue: Issue, attempt_num: int | None, state_name: str | None = None
    ) -> str:
        """Render prompt using state machine prompt assembly (async — fetches comments)."""
        states = self._states_for(issue)
        if state_name and state_name in states:
            state_cfg = states[state_name]
            run = self._issue_state_runs.get(issue.id, 1)
            last_completed = self._last_completed_at.get(issue.id)
            last_run_at = last_completed.isoformat() if last_completed else None

            # Fetch comments for lifecycle context
            comments: list[dict] | None = None
            try:
                client = self._ensure_linear_client()
                comments = await client.fetch_comments(issue.id)
            except Exception as e:
                logger.warning(f"Failed to fetch comments for prompt: {e}", extra={"linked_to": issue.identifier})

            # Only flag as rework for inherit-session states. Fresh-session
            # states (like review) start with zero context each time.
            state_cfg_for_rework = self.cfg.states.get(state_name)
            is_rework = (
                run > 1
                and (not state_cfg_for_rework or state_cfg_for_rework.session != "fresh")
            )

            # Resolve workflow-specific transitions for lifecycle section
            workflow = self._get_issue_workflow_config(issue.id)
            wf_transitions = workflow.transitions.get(state_name)

            # Resolve repo for prompt context (cache-first; three-tier fallback)
            repo = self._get_issue_repo_config(issue.id)

            return assemble_prompt(
                cfg=self.cfg,
                workflow_dir=str(self.workflow_path.parent),
                issue=issue,
                state_name=state_name,
                state_cfg=state_cfg,
                run=run,
                is_rework=is_rework,
                attempt=attempt_num or 1,
                last_run_at=last_run_at,
                comments=comments,
                global_prompt=(wf.global_prompt if (wf := self._workflow_for(issue)) else None),
                transitions=wf_transitions,
                repo=repo,
            )

        # Legacy fallback
        return self._render_prompt(issue, attempt_num, state_name)

    def _render_prompt(
        self, issue: Issue, attempt_num: int | None, state_name: str | None = None
    ) -> str:
        """Render the prompt template with issue context (legacy/sync fallback)."""
        assert self.workflow is not None

        # State machine mode: call assemble_prompt without comments
        states = self._states_for(issue)
        if state_name and state_name in states:
            state_cfg = states[state_name]
            run = self._issue_state_runs.get(issue.id, 1)
            last_completed = self._last_completed_at.get(issue.id)
            last_run_at = last_completed.isoformat() if last_completed else None

            # Resolve workflow-specific transitions for lifecycle section
            workflow = self._get_issue_workflow_config(issue.id)
            wf_transitions = workflow.transitions.get(state_name)

            # Resolve repo for prompt context
            repo = self._get_issue_repo_config(issue.id)

            return assemble_prompt(
                cfg=self.cfg,
                workflow_dir=str(self.workflow_path.parent),
                issue=issue,
                state_name=state_name,
                state_cfg=state_cfg,
                run=run,
                is_rework=False,
                attempt=attempt_num or 1,
                last_run_at=last_run_at,
                comments=None,
                global_prompt=(wf.global_prompt if (wf := self._workflow_for(issue)) else None),
                transitions=wf_transitions,
                repo=repo,
            )

        # Legacy mode: use workflow prompt_template with Jinja2
        template_str = self.workflow.prompt_template

        if not template_str:
            return f"You are working on an issue from Linear: {issue.identifier} - {issue.title}"

        last_completed = self._last_completed_at.get(issue.id)
        last_run_at = last_completed.isoformat() if last_completed else ""

        try:
            template = self._jinja.from_string(template_str)
            return template.render(
                issue={
                    "id": issue.id,
                    "identifier": issue.identifier,
                    "title": issue.title,
                    "description": issue.description or "",
                    "priority": issue.priority,
                    "state": issue.state,
                    "branch_name": issue.branch_name,
                    "url": issue.url,
                    "labels": issue.labels,
                    "blocked_by": [
                        {"id": b.id, "identifier": b.identifier, "state": b.state}
                        for b in issue.blocked_by
                    ],
                    "created_at": str(issue.created_at) if issue.created_at else "",
                    "updated_at": str(issue.updated_at) if issue.updated_at else "",
                },
                attempt=attempt_num,
                last_run_at=last_run_at,
                stage=state_name,
            )
        except TemplateSyntaxError as e:
            raise RuntimeError(f"Template syntax error: {e}")

    async def _publish_run_report(
        self,
        issue: Issue,
        attempt: RunAttempt,
        workspace_path: Path,
        state_name: str | None,
    ) -> None:
        """Upload evidence, post the run report, and apply the classification label.

        Runs after every agent turn, including failed ones — a run that fell
        over having captured three screenshots is exactly when you want to see
        them. Nothing here is allowed to break the run: a Linear outage should
        cost you the report, not the work.
        """
        if attempt.status == "canceled":
            return

        try:
            client = self._ensure_linear_client()
        except Exception as e:
            logger.warning(f"No Linear client for report: {e}", extra={"linked_to": issue.identifier})
            return

        state = state_name or attempt.state_name or "run"
        run = self._issue_state_runs.get(issue.id, 1)

        # ── Evidence ────────────────────────────────────────────────────────
        uploaded: dict[str, str] = {}
        try:
            files = artifacts_mod.collect(workspace_path)
        except Exception as e:
            logger.warning(f"Artifact sweep failed: {e}", extra={"linked_to": issue.identifier})
            files = []

        for path in files:
            try:
                data = path.read_bytes()
            except OSError as e:
                logger.warning(f"Could not read artifact {path}: {e}", extra={"linked_to": issue.identifier})
                continue
            url = await client.upload_file(
                path.name, artifacts_mod.content_type_for(path), data
            )
            if url:
                uploaded[path.name] = url
                attempt.artifacts.append(path.name)

        if files and not uploaded:
            logger.warning(
                f"{len(files)} artifact(s) found but none uploaded",
                extra={"linked_to": issue.identifier},
            )

        # ── Report ──────────────────────────────────────────────────────────
        try:
            report = report_mod.load(workspace_path, attempt.result_text)
        except Exception as e:
            logger.warning(f"Could not load report: {e}", extra={"linked_to": issue.identifier})
            report = None

        if report is None and not uploaded and attempt.status != "succeeded":
            # A run that failed with nothing to show gets the normal retry
            # machinery rather than a comment saying so on every attempt.
            return

        body = report_mod.render(
            report,
            state=state,
            run=run,
            uploaded=uploaded,
            usage={
                "total_tokens": attempt.total_tokens,
                "cost_usd": attempt.cost_usd,
                "tool_calls": attempt.tool_call_count,
            },
            fallback_text=attempt.result_text,
        )

        duration = None
        if attempt.started_at:
            duration = (datetime.now(timezone.utc) - attempt.started_at).total_seconds()
        self.ledger.record_stage(
            workflow=self._workflow_name_for(issue),
            project=self.project_name or "",
            issue_id=issue.id,
            issue=issue.identifier,
            title=issue.title,
            state=state,
            run=run,
            status=attempt.status,
            report=report,
            tokens=attempt.total_tokens,
            cost_usd=attempt.cost_usd,
            tool_calls=attempt.tool_call_count,
            tool_errors=attempt.tool_error_count,
            artifacts=len(uploaded),
            model=attempt.model,
            duration_s=duration,
        )

        posted = await client.post_comment(issue.id, body)
        if posted:
            logger.info(
                f"Posted {state} report ({len(uploaded)} artifacts)",
                extra={"linked_to": issue.identifier},
            )
        else:
            logger.warning("Failed to post run report", extra={"linked_to": issue.identifier})

        # ── Classification label ────────────────────────────────────────────
        await self._apply_classification_label(client, issue, report)

        # Consume both so the next stage starts clean.
        artifacts_mod.clear(workspace_path)
        report_mod.discard(workspace_path)

    async def _apply_classification_label(
        self, client: LinearClient, issue: Issue, report: dict | None
    ) -> None:
        """Tag the issue with what the work was, and how sure the agent is.

        Both are recorded in the ledger too; the labels are so a human can
        filter the board without leaving Linear.
        """
        labels = report_mod.labels_for(report)
        if not labels:
            return

        try:
            team_id, existing_labels, on_issue = await client.fetch_team_labels(issue.id)
        except Exception as e:
            logger.warning(f"Could not read labels: {e}", extra={"linked_to": issue.identifier})
            return

        for name, colour in labels:
            label_id = existing_labels.get(name.lower())

            if label_id is None:
                if not team_id:
                    continue
                label_id = await client.create_label(team_id, name, colour)
                if not label_id:
                    logger.warning(f"Could not create label '{name}'", extra={"linked_to": issue.identifier})
                    continue

            if label_id in on_issue:
                continue

            if await client.add_label(issue.id, label_id):
                logger.info(f"Labelled {issue.identifier} '{name}'", extra={"linked_to": issue.identifier})


    def _on_child_pid(self, pid: int, is_register: bool):
        """Track child claude process PIDs for cleanup on shutdown."""
        if is_register:
            self._child_pids.add(pid)
        else:
            self._child_pids.discard(pid)

    def _on_agent_event(self, identifier: str, event_type: str, event: dict):
        """Mirror notable agent activity into the log buffer.

        Tool calls and text both arrive inside `assistant` events as content
        blocks — there is no separate tool_use event type — so this walks the
        blocks rather than switching on the top-level type.
        """
        extra = {"linked_to": identifier}

        if event_type == "assistant":
            content = (event.get("message") or {}).get("content")
            if isinstance(content, str):
                if content.strip():
                    logger.info(f"[{identifier}] {content.strip()[:160]}", extra=extra)
                return
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    detail = summarise_tool_input(name, block.get("input"))
                    suffix = f" {detail}" if detail else ""
                    logger.info(f"[{identifier}] {name}{suffix}", extra=extra)
                elif block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        logger.info(f"[{identifier}] {text[:160]}", extra=extra)

        elif event_type == "user":
            # Only failures are worth a log line; successes are the norm.
            content = (event.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        logger.warning(f"[{identifier}] tool error", extra=extra)

        elif event_type == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                # Limits are account-wide, so the newest reading is the truth
                # for every project this process is running.
                self.rate_limit = {
                    "status": info.get("status"),
                    "type": info.get("rateLimitType"),
                    "resets_at": info.get("resetsAt"),
                    "overage_status": info.get("overageStatus"),
                    "using_overage": info.get("isUsingOverage"),
                }
                if info.get("status") and info.get("status") != "allowed":
                    logger.warning(
                        f"[{identifier}] rate limit {info.get('status')} "
                        f"({info.get('rateLimitType')})",
                        extra=extra,
                    )

        elif event_type == "result":
            result_text = event.get("result", "")
            if isinstance(result_text, str) and result_text:
                logger.info(f"[{identifier}] result: {result_text[:160]}", extra=extra)

    def _on_worker_exit(self, issue: Issue, attempt: RunAttempt):
        """Handle worker completion.

        _reconcile() owns cleanup for externally-cancelled workers (via
        _kill_worker + _cleanup_issue_state). This method owns cleanup for
        naturally-completed workers. The _force_cancelled guard prevents
        double-processing (token aggregation is not idempotent).
        """
        # If reconciliation already killed and cleaned up this worker, skip
        # all post-exit logic — no transitions, retries, or token aggregation.
        if issue.id in self._force_cancelled:
            self._force_cancelled.discard(issue.id)
            return

        self.total_input_tokens += attempt.input_tokens
        self.total_output_tokens += attempt.output_tokens
        self.total_cache_creation_tokens += attempt.cache_creation_tokens
        self.total_cache_read_tokens += attempt.cache_read_tokens
        self.total_tokens += attempt.total_tokens
        self.total_cost_usd += attempt.cost_usd
        self.total_tool_calls += attempt.tool_call_count
        if attempt.started_at:
            elapsed = (datetime.now(timezone.utc) - attempt.started_at).total_seconds()
            self.total_seconds_running += elapsed

        if attempt.session_id:
            # Only persist session IDs for inherit-mode states.
            # Fresh sessions must not overwrite the stored ID, or the
            # next inherit-mode state resumes the wrong session.
            should_persist = True
            state_cfg = self.cfg.states.get(attempt.state_name or "")
            if state_cfg and state_cfg.session == "fresh":
                should_persist = False
            if should_persist:
                self._last_session_ids[issue.id] = attempt.session_id

        completed_at = datetime.now(timezone.utc)
        attempt.completed_at = completed_at
        if attempt.status != "canceled":
            self._last_completed_at[issue.id] = completed_at

        self.running.pop(issue.id, None)
        self._tasks.pop(issue.id, None)
        self._release_slot(issue.id)

        if attempt.status == "succeeded":
            if attempt.state_name and attempt.state_name in self._states_for(issue):
                # State machine mode: use agent-requested transition or default
                transition_name = attempt.requested_transition or "complete"
                if not attempt.requested_transition:
                    logger.info(
                        f"No transition directive, defaulting to complete "
                        f"for {issue.identifier}"
                    )

                # Agent self-cancellation via reserved "cancel" transition
                if transition_name == "cancel":
                    logger.info(
                        f"Agent self-cancelled {issue.identifier}: "
                        f"{attempt.last_message[:200]}"
                    )
                    self._cleanup_issue_state(issue.id)
                    return

                # Safety cap: enforce max_rework for agent-initiated rework
                if transition_name != "complete":
                    sc = self.cfg.states[attempt.state_name]
                    if sc.max_rework is not None:
                        run = self._issue_state_runs.get(issue.id, 1)
                        if run > sc.max_rework:
                            logger.warning(
                                f"Max rework ({sc.max_rework}) exceeded "
                                f"for {issue.identifier} in state "
                                f"{attempt.state_name}, forcing complete"
                            )
                            transition_name = "complete"

                asyncio.create_task(
                    self._safe_transition(issue, transition_name)
                )
            else:
                # Legacy mode
                self._schedule_retry(issue, attempt_num=1, delay_ms=1000)
        elif attempt.status == "config_error":
            # Unrecoverable config problem (hook template typo, Jinja2
            # syntax error, etc). A retry loop can't fix a config typo, so
            # mark the issue blocked and release the claim. On successful
            # hot-reload of workflow.yaml, _load_workflow clears
            # _config_blocked and the issue dispatches again.
            self._config_blocked.add(issue.id)
            self.claimed.discard(issue.id)
            self.running.pop(issue.id, None)
            self._tasks.pop(issue.id, None)
            logger.warning(
                f"Config error blocking issue={issue.identifier}: "
                f"{attempt.error}. Dispatch will resume after "
                f"workflow.yaml is reloaded with a valid config."
            )
        elif attempt.status in ("failed", "timed_out", "stalled"):
            current_attempt = (attempt.attempt or 0) + 1
            delay = min(
                10_000 * (2 ** (current_attempt - 1)),
                self.cfg.agent.max_retry_backoff_ms,
            )
            self._schedule_retry(
                issue,
                attempt_num=current_attempt,
                delay_ms=delay,
                error=attempt.error,
            )
        else:
            self.claimed.discard(issue.id)

        # Best-effort log retention cleanup after worker completes
        self._fire_and_forget(self._cleanup_logs())

    def _schedule_retry(
        self,
        issue: Issue,
        attempt_num: int,
        delay_ms: int,
        error: str | None = None,
    ):
        """Schedule a retry for an issue."""
        # Cancel existing retry
        if issue.id in self._retry_timers:
            self._retry_timers[issue.id].cancel()

        entry = RetryEntry(
            issue_id=issue.id,
            identifier=issue.identifier,
            attempt=attempt_num,
            due_at_ms=time.monotonic() * 1000 + delay_ms,
            error=error,
        )
        self.retry_attempts[issue.id] = entry

        loop = asyncio.get_running_loop()
        handle = loop.call_later(
            delay_ms / 1000,
            lambda: loop.create_task(self._handle_retry(issue.id)),
        )
        self._retry_timers[issue.id] = handle

        logger.info(
            f"Retry scheduled issue={issue.identifier} "
            f"attempt={attempt_num} delay={delay_ms}ms "
            f"error={error or 'continuation'}",
            extra={"linked_to": issue.identifier},
        )

    async def _handle_retry(self, issue_id: str):
        """Handle a retry timer firing."""
        entry = self.retry_attempts.pop(issue_id, None)
        self._retry_timers.pop(issue_id, None)

        if entry is None:
            return

        # Fetch fresh candidates to check eligibility
        try:
            client = self._ensure_linear_client()
            candidates = await client.fetch_candidate_issues(
                self.cfg.tracker.project_slug,
                self.cfg.active_linear_states(),
            )
        except Exception as e:
            logger.warning(f"Retry candidate fetch failed: {e}")
            self.claimed.discard(issue_id)
            return

        issue = None
        for c in candidates:
            if c.id == issue_id:
                issue = c
                break

        if issue is None:
            # No longer active
            self.claimed.discard(issue_id)
            logger.info(f"Retry: issue {entry.identifier} no longer active, releasing", extra={"linked_to": entry.identifier})
            return

        # Check slots via the same path as the dispatch loop
        can, reason = self._has_slot()
        if not can:
            self._schedule_retry(
                issue,
                attempt_num=entry.attempt,
                delay_ms=10_000,
                error=reason or "no available orchestrator slots",
            )
            return
        if not self._claim_slot(issue.id):
            self._schedule_retry(
                issue,
                attempt_num=entry.attempt,
                delay_ms=10_000,
                error="no available orchestrator slots",
            )
            return

        self._dispatch(issue, attempt_num=entry.attempt)

    async def _reconcile(self):
        """Reconcile running and gated issues against current Linear state."""
        ids_to_check = set(self.running) | set(self._pending_gates)
        if not ids_to_check:
            return

        try:
            client = self._ensure_linear_client()
            states = await client.fetch_issue_states_by_ids(list(ids_to_check))
        except Exception as e:
            logger.warning(f"Reconciliation state fetch failed: {e}")
            return

        terminal_lower = [
            s.strip().lower() for s in self.cfg.terminal_linear_states()
        ]
        active_lower = [
            s.strip().lower() for s in self.cfg.active_linear_states()
        ]
        review_lower = self.cfg.linear_states.review.strip().lower()

        for issue_id in list(ids_to_check):
            current_state = states.get(issue_id)

            if current_state is None:
                # Issue not found in Linear — may be deleted/archived.
                # Clean up gated issues that have no running worker.
                if issue_id in self._pending_gates and issue_id not in self.running:
                    logger.info(
                        f"Reconciliation: gated issue {issue_id} not found in Linear, cleaning up"
                    )
                    # Try to remove workspace using cached identifier
                    cached = self._last_issues.get(issue_id)
                    if cached:
                        ws_root = self.cfg.workspace.resolved_root()
                        repo = self._get_issue_repo_config(issue_id)
                        rendered_hooks = _render_hooks_best_effort(
                            self.cfg.hooks, repo, self.cfg.repos_synthesized
                        )
                        await remove_workspace(
                            ws_root, cached.identifier, repo.name, rendered_hooks,
                            docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                        )
                    self._cleanup_issue_state(issue_id)
                continue

            state_lower = current_state.strip().lower()

            if state_lower in terminal_lower:
                # Terminal - kill worker, clean workspace, clean all state
                _ident = self.running[issue_id].issue_identifier if issue_id in self.running else issue_id
                logger.info(
                    f"Reconciliation: {_ident} is terminal ({current_state}), stopping",
                    extra={"linked_to": _ident},
                )
                self._force_cancelled.add(issue_id)

                if issue_id in self.running:
                    await self._kill_worker(issue_id, reason=f"terminal state {current_state}")

                    attempt = self.running.get(issue_id)
                    if attempt:
                        ws_root = self.cfg.workspace.resolved_root()
                        repo = self._get_issue_repo_config(issue_id)
                        rendered_hooks = _render_hooks_best_effort(
                            self.cfg.hooks, repo, self.cfg.repos_synthesized
                        )
                        await remove_workspace(
                            ws_root, attempt.issue_identifier, repo.name, rendered_hooks,
                            docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                        )
                else:
                    # Gated issue — no process to kill, just clean up state
                    cached = self._last_issues.get(issue_id)
                    if cached:
                        ws_root = self.cfg.workspace.resolved_root()
                        repo = self._get_issue_repo_config(issue_id)
                        rendered_hooks = _render_hooks_best_effort(
                            self.cfg.hooks, repo, self.cfg.repos_synthesized
                        )
                        await remove_workspace(
                            ws_root, cached.identifier, repo.name, rendered_hooks,
                            docker_cfg=self.cfg.docker if self.cfg.docker.enabled else None,
                        )

                self._cleanup_issue_state(issue_id)
                self._fire_and_forget(
                    self._post_cancellation_comment(issue_id, current_state)
                )
                self._release_slot(issue_id)
                # Clean up state caches so stale entries don't accumulate
                self._issue_current_state.pop(issue_id, None)
                self._issue_state_runs.pop(issue_id, None)
                self._announced_states = {k for k in self._announced_states if k[0] != issue_id}
                self._pending_gates.pop(issue_id, None)
                self._last_session_ids.pop(issue_id, None)

            elif state_lower == review_lower:
                # In review/gate state — kill worker subprocess but keep gate tracking
                if issue_id in self.running:
                    self._force_cancelled.add(issue_id)
                    await self._kill_worker(issue_id, reason=f"review state {current_state}")
                    self.running.pop(issue_id, None)
                    self._tasks.pop(issue_id, None)
                self._release_slot(issue_id)

            elif state_lower not in active_lower:
                # Neither active nor terminal nor review - kill and clean up
                if issue_id in self.running:
                    logger.info(
                        f"Reconciliation: {issue_id} not active ({current_state}), stopping"
                    )
                    self._force_cancelled.add(issue_id)
                    await self._kill_worker(issue_id, reason=f"non-active state {current_state}")
                    self._cleanup_issue_state(issue_id)

    def get_state_snapshot(self) -> dict[str, Any]:
        """Get current runtime state for observability."""
        now = datetime.now(timezone.utc)
        active_seconds = sum(
            (now - r.started_at).total_seconds()
            for r in self.running.values()
            if r.started_at
        )
        project_name = self.project_name or ""

        # Totals are banked when a worker exits, so in-flight agents must be
        # added on top — otherwise the dashboard reads zero cost for the whole
        # of a long run and only jumps at the end.
        live = self.running.values()
        live_input = sum(r.input_tokens for r in live)
        live_output = sum(r.output_tokens for r in live)
        live_cache_creation = sum(r.cache_creation_tokens for r in live)
        live_cache_read = sum(r.cache_read_tokens for r in live)
        live_total = sum(r.total_tokens for r in live)
        live_cost = sum(r.cost_usd for r in live)
        live_tools = sum(r.tool_call_count for r in live)

        return {
            "generated_at": now.isoformat(),
            "project_name": project_name,
            "paused": self.pool.is_paused(project_name) if self.pool is not None else False,
            "counts": {
                "running": len(self.running),
                "retrying": len(self.retry_attempts),
                "gates": len(self._pending_gates),
                "queued": len(self._queued),
            },
            "running": [
                {
                    "project_name": project_name,
                    "issue_id": r.issue_id,
                    "issue_identifier": r.issue_identifier,
                    "session_id": r.session_id,
                    "turn_count": r.turn_count,
                    "status": r.status,
                    "last_event": r.last_event,
                    "last_message": r.last_message,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "last_event_at": (
                        r.last_event_at.isoformat() if r.last_event_at else None
                    ),
                    "tokens": {
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                        "cache_creation_tokens": r.cache_creation_tokens,
                        "cache_read_tokens": r.cache_read_tokens,
                        "total_tokens": r.total_tokens,
                    },
                    "cost_usd": round(r.cost_usd, 4),
                    "model": r.model,
                    "state_name": r.state_name,
                    "tool_call_count": r.tool_call_count,
                    "tool_error_count": r.tool_error_count,
                    "tool_counts": dict(r.tool_counts),
                    "agent_turns": r.agent_turns,
                    "compaction_count": r.compaction_count,
                    "permission_denials": r.permission_denials,
                    "artifact_count": len(r.artifacts),
                    # The dashboard renders a timeline from this; cap the wire
                    # payload so a long-running agent cannot bloat every poll.
                    "activity": [e.to_dict() for e in list(r.activity)[-40:]],
                    "container_name": r.container_name,
                    "workflow": self._issue_workflow.get(r.issue_id),
                }
                for r in self.running.values()
            ],
            "retrying": [
                {
                    "project_name": project_name,
                    "issue_id": e.issue_id,
                    "issue_identifier": e.identifier,
                    "attempt": e.attempt,
                    "error": e.error,
                }
                for e in self.retry_attempts.values()
            ],
            "gates": [
                {
                    "project_name": project_name,
                    "issue_id": issue_id,
                    "issue_identifier": self._last_issues.get(issue_id, Issue(id="", identifier=issue_id, title="")).identifier,
                    "gate_state": gate_state,
                    "run": self._issue_state_runs.get(issue_id, 1),
                    "workflow": self._issue_workflow.get(issue_id),
                }
                for issue_id, gate_state in self._pending_gates.items()
            ],
            "queued": [
                {**q, "project_name": project_name} for q in self._queued
            ],
            "rate_limit": self.rate_limit,
            "totals": {
                "input_tokens": self.total_input_tokens + live_input,
                "output_tokens": self.total_output_tokens + live_output,
                "cache_creation_tokens": self.total_cache_creation_tokens + live_cache_creation,
                "cache_read_tokens": self.total_cache_read_tokens + live_cache_read,
                "total_tokens": self.total_tokens + live_total,
                "cost_usd": round(self.total_cost_usd + live_cost, 4),
                "tool_calls": self.total_tool_calls + live_tools,
                "seconds_running": round(
                    self.total_seconds_running + active_seconds, 1
                ),
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Multi-project coordinator
# ─────────────────────────────────────────────────────────────────────────────


class MultiOrchestrator:
    """Owns N per-project Orchestrators sharing one ConcurrencyPool.

    Both single-project and multi-project workflows are run through this
    coordinator — single-project just means N=1. The coordinator handles
    shared concurrency, aggregated dashboard state, pause/resume,
    keyboard handler context, and cooperative startup/shutdown.
    """

    def __init__(self, workflow_path: str | Path):
        self.workflow_path = Path(workflow_path)
        self.pool = ConcurrencyPool()
        self.orchestrators: dict[str, Orchestrator] = {}  # project_name -> Orchestrator
        self._tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        self.config: "ServiceConfig | None" = None
        # Eagerly parse config so callers can read server.port etc. before start()
        self._initial_load()

    # ── Config wiring ──────────────────────────────────────────────────────

    def _initial_load(self) -> tuple[list[ProjectConfig], list[str]]:
        """Parse the workflow file. Returns (projects, errors)."""
        try:
            full = parse_workflow_file(self.workflow_path)
        except Exception as e:
            return [], [f"Workflow load error: {e}"]
        errors = validate_config(full.config)
        if errors:
            return [], errors
        self.config = full.config
        return list(full.config.projects), []

    def _refresh_pool_caps(self) -> None:
        """Pull global cap + per-project caps from the latest workflow file."""
        try:
            full = parse_workflow_file(self.workflow_path)
        except Exception:
            return
        self.config = full.config
        agent = full.config.agent
        self.pool.global_cap = agent.max_concurrent_agents
        # Per-project caps: project block override wins, then agent map
        caps: dict[str, int] = {}
        for p in full.config.projects:
            if p.max_concurrent is not None:
                caps[p.name] = int(p.max_concurrent)
        for name, val in agent.max_concurrent_per_project.items():
            caps.setdefault(str(name), int(val))
        self.pool.per_project_caps = caps
        # Initial pause states (only applied for newly-seen projects;
        # don't clobber a runtime toggle by re-reading workflow.yaml)
        for p in full.config.projects:
            if p.paused and p.name not in self.pool.running_per_project:
                self.pool.pause(p.name)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def start(self):
        projects, errors = self._initial_load()
        if errors:
            for e in errors:
                logger.error(f"Config error: {e}")
            raise RuntimeError(f"Startup validation failed: {errors}")

        self._refresh_pool_caps()

        for project in projects:
            orch = Orchestrator(
                workflow_path=self.workflow_path,
                project_name=project.name,
                pool=self.pool,
            )
            self.orchestrators[project.name] = orch

        logger.info(
            f"Starting MultiOrchestrator "
            f"projects=[{', '.join(self.orchestrators.keys())}] "
            f"global_cap={self.pool.global_cap}"
        )

        self._stop_event = asyncio.Event()

        # Start a periodic pool refresher so global cap and per-project
        # caps track hot-reloaded workflow.yaml changes.
        self._tasks.append(asyncio.create_task(self._pool_refresh_loop()))

        # Run all per-project orchestrators concurrently
        for orch in self.orchestrators.values():
            self._tasks.append(asyncio.create_task(orch.start()))

        # Block until stop()
        await self._stop_event.wait()

    async def _pool_refresh_loop(self):
        """Re-read workflow.yaml periodically to pick up cap changes."""
        try:
            while True:
                await asyncio.sleep(30)
                self._refresh_pool_caps()
        except asyncio.CancelledError:
            return

    async def stop(self):
        """Stop all orchestrators in parallel."""
        if self._stop_event is not None:
            self._stop_event.set()
        # Stop all per-project orchestrators
        await asyncio.gather(
            *(o.stop() for o in self.orchestrators.values()),
            return_exceptions=True,
        )
        for t in self._tasks:
            t.cancel()

    # ── Pause / resume ─────────────────────────────────────────────────────

    @property
    def project_names(self) -> list[str]:
        return list(self.orchestrators.keys())

    def is_paused(self, project_name: str) -> bool:
        return self.pool.is_paused(project_name)

    def pause(self, project_name: str) -> bool:
        if project_name not in self.orchestrators:
            return False
        self.pool.pause(project_name)
        return True

    def resume(self, project_name: str) -> bool:
        if project_name not in self.orchestrators:
            return False
        self.pool.resume(project_name)
        return True

    def toggle(self, project_name: str) -> bool:
        if project_name not in self.orchestrators:
            return False
        return self.pool.toggle(project_name)

    # ── Aggregated state for dashboard / status table ──────────────────────

    async def force_tick(self):
        """Trigger an immediate tick on every orchestrator."""
        await asyncio.gather(
            *(o._tick() for o in self.orchestrators.values()),
            return_exceptions=True,
        )

    def get_state_snapshot(self) -> dict[str, Any]:
        """Aggregate all orchestrator snapshots into one combined view."""
        now = datetime.now(timezone.utc)
        per_project: list[dict[str, Any]] = []
        running: list[dict] = []
        retrying: list[dict] = []
        gates: list[dict] = []
        queued: list[dict] = []
        total_input = 0
        total_output = 0
        total_cache_creation = 0
        total_cache_read = 0
        total_tokens = 0
        total_cost = 0.0
        total_tools = 0
        total_seconds = 0.0
        rate_limit: dict[str, Any] | None = None
        for name, orch in self.orchestrators.items():
            snap = orch.get_state_snapshot()
            per_project.append({
                "name": name,
                "paused": snap["paused"],
                "counts": snap["counts"],
                "totals": snap["totals"],
            })
            running.extend(snap["running"])
            retrying.extend(snap["retrying"])
            gates.extend(snap["gates"])
            queued.extend(snap["queued"])
            total_input += snap["totals"]["input_tokens"]
            total_output += snap["totals"]["output_tokens"]
            total_cache_creation += snap["totals"]["cache_creation_tokens"]
            total_cache_read += snap["totals"]["cache_read_tokens"]
            total_tokens += snap["totals"]["total_tokens"]
            total_cost += snap["totals"]["cost_usd"]
            total_tools += snap["totals"]["tool_calls"]
            total_seconds += snap["totals"]["seconds_running"]
            # Rate limits are per-account, not per-project — any project's
            # reading describes the same shared window.
            rate_limit = snap.get("rate_limit") or rate_limit
        return {
            "generated_at": now.isoformat(),
            "projects": per_project,
            "pool": self.pool.snapshot(),
            "counts": {
                "running": len(running),
                "retrying": len(retrying),
                "gates": len(gates),
                "queued": len(queued),
                "projects": len(per_project),
            },
            "running": running,
            "retrying": retrying,
            "gates": gates,
            "queued": queued,
            "rate_limit": rate_limit,
            "totals": {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "cache_creation_tokens": total_cache_creation,
                "cache_read_tokens": total_cache_read,
                "total_tokens": total_tokens,
                "cost_usd": round(total_cost, 4),
                "tool_calls": total_tools,
                "seconds_running": round(total_seconds, 1),
            },
        }


# ---------------------------------------------------------------------------
# Log retention helpers (module-level pure functions for testability)
# ---------------------------------------------------------------------------


def cleanup_old_logs(log_dir: Path, max_age_days: int) -> int:
    """Delete log files older than max_age_days. Returns count of deleted files."""
    cutoff = time.time() - (max_age_days * 86400)
    deleted = 0
    for issue_dir in log_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        for log_file in issue_dir.iterdir():
            if not log_file.is_file():
                continue
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    deleted += 1
            except OSError:
                pass
        # Remove empty issue directory
        try:
            if issue_dir.is_dir() and not any(issue_dir.iterdir()):
                issue_dir.rmdir()
        except OSError:
            pass
    if deleted:
        logger.info(f"Log retention: deleted {deleted} old log files")
    return deleted


def enforce_size_limit(
    log_dir: Path,
    max_total_size_mb: int,
    exempt_identifiers: set[str] | None = None,
) -> int:
    """Delete oldest log files when total size exceeds limit. Returns count deleted.

    Files in directories matching exempt_identifiers are skipped (active agents).
    """
    exempt = exempt_identifiers or set()
    max_bytes = max_total_size_mb * 1024 * 1024

    # Collect all log files with their sizes and mtimes
    files: list[tuple[Path, float, int]] = []  # (path, mtime, size)
    total_size = 0
    for issue_dir in log_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        for log_file in issue_dir.iterdir():
            if not log_file.is_file():
                continue
            try:
                stat = log_file.stat()
                files.append((log_file, stat.st_mtime, stat.st_size))
                total_size += stat.st_size
            except OSError:
                pass

    if total_size <= max_bytes:
        return 0

    # Sort oldest first
    files.sort(key=lambda x: x[1])

    deleted = 0
    for path, mtime, size in files:
        if total_size <= max_bytes:
            break
        # Skip files for actively running agents
        if path.parent.name in exempt:
            continue
        try:
            path.unlink()
            total_size -= size
            deleted += 1
        except OSError:
            pass

    # Clean up empty directories
    for issue_dir in log_dir.iterdir():
        if not issue_dir.is_dir():
            continue
        try:
            if not any(issue_dir.iterdir()):
                issue_dir.rmdir()
        except OSError:
            pass

    if deleted:
        logger.info(f"Log retention: deleted {deleted} files to enforce size limit")
    if total_size > max_bytes:
        logger.warning(
            f"Log retention: still over size limit after cleanup "
            f"({total_size // (1024*1024)}MB > {max_total_size_mb}MB) — "
            f"remaining files may belong to active agents"
        )
    return deleted
