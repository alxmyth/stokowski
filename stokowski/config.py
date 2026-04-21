"""Workflow loader and typed configuration."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stokowski.models import Issue

import yaml

logger = logging.getLogger("stokowski.config")

log = logging.getLogger(__name__)


@dataclass
class TrackerConfig:
    kind: str = "linear"
    endpoint: str = "https://api.linear.app/graphql"
    api_key: str = ""
    project_slug: str = ""


@dataclass
class PollingConfig:
    interval_ms: int = 30_000


@dataclass
class WorkspaceConfig:
    root: str = ""

    def resolved_root(self) -> Path:
        if self.root:
            return Path(os.path.expandvars(os.path.expanduser(self.root)))
        return Path(tempfile.gettempdir()) / "stokowski_workspaces"


@dataclass
class HooksConfig:
    after_create: str | None = None
    before_run: str | None = None
    after_run: str | None = None
    before_remove: str | None = None
    on_stage_enter: str | None = None
    timeout_ms: int = 60_000


@dataclass
class ClaudeConfig:
    command: str = "claude"
    permission_mode: str = "auto"  # "auto" or "allowedTools"
    allowed_tools: list[str] = field(
        default_factory=lambda: ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    )
    model: str | None = None
    # NOTE: max_turns applies to LEGACY multi-turn mode only. In state machine
    # mode each dispatch is exactly one `claude -p` invocation, so this value is
    # unused — the state machine controls continuation, and the CLI has no
    # --max-turns flag to pass it to. A run is bounded by turn_timeout_ms and
    # stall_timeout_ms below, which is the right axis on a subscription: there
    # is no dollar meter to cap, only wall-clock and the rate-limit window.
    max_turns: int = 20
    turn_timeout_ms: int = 3_600_000
    stall_timeout_ms: int = 300_000
    append_system_prompt: str | None = None
    # Reasoning effort (`--effort`): low | medium | high | xhigh | max.
    effort: str | None = None
    # Comma-separated models to fall back to when the primary is overloaded or
    # unavailable (`--fallback-model`) — useful when a run hits a rate limit.
    fallback_model: str | None = None


@dataclass
class WorkflowSpec:
    """One named pipeline: a state machine plus the prompt that frames it.

    Deliberately narrow. Everything else in a workflow file — tracker,
    workspace, hooks, concurrency — is runtime configuration shared by every
    pipeline, and duplicating it per workflow would mean three places to rotate
    an API key. Only the state machine and its global prompt vary by the kind
    of work being done.

    The global prompt is what removes `if this is a bug…` branching from stage
    prompts: it states what kind of work this is once, so a shared `review.md`
    needs no conditional.
    """

    name: str
    states: dict[str, StateConfig] = field(default_factory=dict)
    global_prompt: str | list[str] | None = None
    description: str = ""

    @property
    def entry_state(self) -> str | None:
        """The first agent state of THIS pipeline.

        Project-level `entry_state` reads the inline `states:` block, which is
        one workflow among several. Routing an issue to `bug-fix` and then
        starting it in `default`'s entry state drops it into a state its own
        machine does not contain, and it can never transition out.
        """
        for name, sc in self.states.items():
            if sc.type == "agent":
                return name
        return None


@dataclass
class RoutingRule:
    label: str
    workflow: str


@dataclass
class RoutingConfig:
    """Maps a Linear label onto a workflow. First match wins.

    Order is significant and explicit: a ticket labelled both `bug` and `spike`
    resolves the same way every time, and which way is readable from the config
    rather than from dict iteration order.
    """

    default: str | None = None
    rules: list[RoutingRule] = field(default_factory=list)

    def resolve(self, labels: list[str] | None) -> str | None:
        """Return the workflow name for a set of issue labels."""
        present = {label.strip().lower() for label in (labels or []) if label}
        for rule in self.rules:
            if rule.label.strip().lower() in present:
                return rule.workflow
        return self.default



@dataclass
class AgentConfig:
    max_concurrent_agents: int = 5
    max_retry_backoff_ms: int = 300_000
    max_concurrent_agents_by_state: dict[str, int] = field(default_factory=dict)
    # Optional per-project cap. Keys are project names; values cap how many
    # of the global pool a project may hold at once. A project may also set
    # `max_concurrent` in its own block — that takes precedence over this map.
    max_concurrent_per_project: dict[str, int] = field(default_factory=dict)


@dataclass
class DockerConfig:
    """Docker isolation settings for agent containers."""
    enabled: bool = False
    default_image: str = ""
    inherit_claude_config: bool = True
    host_claude_dir: str = "~/.claude"
    host_claude_dir_mount: str = ""
    plugin_shim_host_path: str = ""
    plugin_shim_container_path: str = ""
    extra_env: list[str] = field(default_factory=list)
    extra_volumes: list[str] = field(default_factory=list)
    volume_prefix: str = "stokowski-ws"
    sessions_volume: str = "stokowski-sessions"
    init: bool = True


@dataclass
class ServerConfig:
    port: int | None = None
    host: str | None = None


@dataclass
class LoggingConfig:
    """Agent run log retention configuration."""
    enabled: bool = False
    log_dir: str = ""
    max_age_days: int = 14
    max_total_size_mb: int = 500

    def resolved_log_dir(self, base: Path | None = None) -> Path:
        """Resolve ~ and $VAR in log_dir.

        Args:
            base: Base directory for resolving relative paths (e.g. workflow dir).
        """
        expanded = os.path.expanduser(os.path.expandvars(self.log_dir))
        p = Path(expanded)
        if not p.is_absolute() and base:
            p = base / p
        return p


@dataclass
class LinearStatesConfig:
    """Maps logical state names to actual Linear state names."""
    todo: str = "Todo"
    active: str = "In Progress"
    awaiting_ci: str = "Awaiting CI"
    review: str = "Human Review"
    gate_approved: str = "Gate Approved"
    rework: str = "Rework"
    terminal: list[str] = field(default_factory=lambda: ["Done", "Closed", "Cancelled"])


@dataclass
class PromptsConfig:
    """Prompt file references."""
    global_prompt: str | list[str] | None = None


@dataclass
class StateConfig:
    """A single state in the state machine."""
    name: str = ""
    type: str = "agent"              # "agent", "gate", "terminal"
    prompt: str | None = None        # path to prompt .md file
    linear_state: str = "active"     # key into LinearStatesConfig
    runner: str = "claude"
    model: str | None = None
    max_turns: int | None = None
    effort: str | None = None
    fallback_model: str | None = None
    turn_timeout_ms: int | None = None
    stall_timeout_ms: int | None = None
    session: str = "inherit"
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    rework_to: str | None = None     # gate only
    max_rework: int | None = None    # gate only; also used for agent-initiated rework cap
    skip_labels: list[str] = field(default_factory=list)  # labels that auto-approve this gate
    transitions: dict[str, str] = field(default_factory=dict)
    hooks: HooksConfig | None = None
    docker_image: str | None = None           # Override default_image for this state


@dataclass
class ProjectConfig:
    """A single project's resolved config — flattens global defaults + per-project overrides.

    Each project gets its own Linear tracker, workspace, hooks, prompts,
    state machine, and (optionally) Linear-state and Claude overrides.
    Multi-project setups use one ProjectConfig per `projects:` entry;
    legacy single-project setups synthesize one ProjectConfig from the
    top-level fields.
    """
    name: str = ""
    paused: bool = False
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    states: dict[str, StateConfig] = field(default_factory=dict)
    workflows: dict[str, WorkflowSpec] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    linear_states: LinearStatesConfig = field(default_factory=LinearStatesConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    workflow_dir: Path = field(default_factory=lambda: Path("."))
    # Per-project cap (overrides AgentConfig.max_concurrent_per_project[name]).
    max_concurrent: int | None = None
    workflows: dict[str, WorkflowConfig] = field(default_factory=dict)
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    # Set by the parser: True when the repos: section was absent from the
    # source YAML and a synthetic `_default` entry was generated for backward
    # compatibility. Operators should never set this field directly.
    repos_synthesized: bool = False

    def resolved_api_key(self) -> str:
        key = self.tracker.api_key
        if not key:
            return os.environ.get("LINEAR_API_KEY", "")
        if key.startswith("$"):
            return os.environ.get(key[1:], "")
        return key

    def agent_env(self) -> dict[str, str]:
        """Build env vars to pass to agent subprocesses for this project."""
        env = dict(os.environ)
        api_key = self.resolved_api_key()
        if api_key:
            env["LINEAR_API_KEY"] = api_key
        if self.tracker.project_slug:
            env["LINEAR_PROJECT_SLUG"] = self.tracker.project_slug
        if self.tracker.endpoint:
            env["LINEAR_ENDPOINT"] = self.tracker.endpoint
        env["STOKOWSKI_PROJECT"] = self.name
        return env

    def docker_env(self) -> dict[str, str]:
        """Build minimal env vars for Docker agent containers.

        Unlike agent_env() which inherits the full parent environment,
        Docker mode only forwards explicitly declared variables to
        maintain container isolation.
        """
        env: dict[str, str] = {}
        api_key = self.resolved_api_key()
        if api_key:
            env["LINEAR_API_KEY"] = api_key
        if self.tracker.project_slug:
            env["LINEAR_PROJECT_SLUG"] = self.tracker.project_slug
        if self.tracker.endpoint:
            env["LINEAR_ENDPOINT"] = self.tracker.endpoint
        if not self.docker.inherit_claude_config:
            ak = os.environ.get("ANTHROPIC_API_KEY", "")
            if ak:
                env["ANTHROPIC_API_KEY"] = ak
        for var_name in self.docker.extra_env:
            val = os.environ.get(var_name, "")
            if val:
                env[var_name] = val
        return env

    @property
    def entry_state(self) -> str | None:
        """Return the first agent state.

        If workflows are defined, delegates to the default workflow's
        entry_state. Otherwise falls back to scanning the states dict.
        """
        if self.workflows:
            for wf in self.workflows.values():
                if wf.default:
                    return wf.entry_state or None
            # No default workflow — fall through to legacy scan
        for name, sc in self.states.items():
            if sc.type == "agent":
                return name
        return None

    def resolve_workflow(self, issue: Issue) -> WorkflowConfig:
        """Resolve which workflow applies to an issue based on its labels.

        Iterates workflows and checks if the workflow's label matches any
        of the issue's labels (case-insensitive). First match wins.
        Falls back to the workflow marked ``default=True``.
        Raises ValueError if no default workflow is configured.
        """
        issue_labels_lower = [l.lower() for l in issue.labels]
        for wf in self.workflows.values():
            if wf.label is not None and wf.label.lower() in issue_labels_lower:
                return wf
        # No label match — return default
        for wf in self.workflows.values():
            if wf.default:
                return wf
        raise ValueError("No default workflow configured")

    def resolve_repo(self, issue: Issue) -> RepoConfig:
        """Resolve which repo applies to an issue based on its labels.

        Mirrors ``resolve_workflow``: iterate repos, case-insensitive label
        match (first wins), then fall back to the repo marked ``default=True``.

        For legacy configs (``repos_synthesized=True``) there is exactly one
        repo (``_default``) marked ``default=True``, so any issue resolves
        to it.
        """
        issue_labels_lower = [l.lower() for l in issue.labels]
        for repo in self.repos.values():
            if repo.label is not None and repo.label.lower() in issue_labels_lower:
                return repo
        for repo in self.repos.values():
            if repo.default:
                return repo
        raise ValueError("No default repo configured")

    def get_workflow(self, name: str) -> WorkflowConfig | None:
        """Look up a workflow by name. Returns None if not found."""
        return self.workflows.get(name)

    def all_states(self) -> dict[str, StateConfig]:
        """Every state across every workflow.

        Used for questions that are about the whole project rather than one
        issue — which Linear states to poll, whether any pipeline has a gate.
        Same-named states in different workflows collapse to one entry, which
        is what those callers want.
        """
        merged: dict[str, StateConfig] = dict(self.states)
        for wf in self.workflows.values():
            merged.update(wf.states)
        return merged

    def workflow_for(self, labels: list[str] | None) -> WorkflowSpec | None:
        """Pick the workflow a set of issue labels routes to."""
        name = self.routing.resolve(labels)
        return self.workflows.get(name) if name else None

    def active_linear_states(self) -> list[str]:
        ls = self.linear_states
        seen: list[str] = []
        if ls.todo and ls.todo not in seen:
            seen.append(ls.todo)
        for sc in self.all_states().values():
            if sc.type == "agent":
                linear_name = _resolve_linear_state_name(sc.linear_state, ls)
                if linear_name and linear_name not in seen:
                    seen.append(linear_name)
        return seen

    def gate_linear_states(self) -> list[str]:
        ls = self.linear_states
        seen: list[str] = []
        for sc in self.states.values():
            if sc.type == "gate":
                linear_name = _resolve_linear_state_name(sc.linear_state, ls)
                if linear_name and linear_name not in seen:
                    seen.append(linear_name)
        return seen

    def terminal_linear_states(self) -> list[str]:
        return list(self.linear_states.terminal)


@dataclass
class WorkflowDefinition:
    config: ServiceConfig
    prompt_template: str


@dataclass
class ServiceConfig:
    """Top-level config. `projects` is the authoritative project list.

    Top-level `tracker`, `workspace`, `hooks`, `prompts`, `states`,
    `linear_states`, `claude` remain populated for backward compat with
    single-project workflows — they mirror `projects[0]` in that case.
    """
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    polling: PollingConfig = field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    linear_states: LinearStatesConfig = field(default_factory=LinearStatesConfig)
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    states: dict[str, StateConfig] = field(default_factory=dict)
    workflows: dict[str, WorkflowSpec] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    projects: list[ProjectConfig] = field(default_factory=list)
    workflow_dir: Path = field(default_factory=lambda: Path("."))

    def resolved_api_key(self) -> str:
        # Legacy passthrough — delegates to first project.
        if self.projects:
            return self.projects[0].resolved_api_key()
        key = self.tracker.api_key
        if not key:
            return os.environ.get("LINEAR_API_KEY", "")
        if key.startswith("$"):
            return os.environ.get(key[1:], "")
        return key

    def agent_env(self) -> dict[str, str]:
        if self.projects:
            return self.projects[0].agent_env()
        env = dict(os.environ)
        if self.tracker.api_key:
            env["LINEAR_API_KEY"] = self.resolved_api_key()
        if self.tracker.project_slug:
            env["LINEAR_PROJECT_SLUG"] = self.tracker.project_slug
        if self.tracker.endpoint:
            env["LINEAR_ENDPOINT"] = self.tracker.endpoint
        return env

    @property
    def entry_state(self) -> str | None:
        if self.projects:
            return self.projects[0].entry_state
        for name, sc in self.states.items():
            if sc.type == "agent":
                return name
        return None

    def all_states(self) -> dict[str, StateConfig]:
        """Every state across every workflow (see ProjectConfig.all_states)."""
        if self.projects:
            return self.projects[0].all_states()
        merged: dict[str, StateConfig] = dict(self.states)
        for wf in self.workflows.values():
            merged.update(wf.states)
        return merged

    def workflow_for(self, labels: list[str] | None) -> WorkflowSpec | None:
        """Pick the workflow a set of issue labels routes to."""
        if self.projects:
            return self.projects[0].workflow_for(labels)
        name = self.routing.resolve(labels)
        return self.workflows.get(name) if name else None

    def active_linear_states(self) -> list[str]:
        if self.projects:
            return self.projects[0].active_linear_states()
        ls = self.linear_states
        seen: list[str] = []
        if ls.todo and ls.todo not in seen:
            seen.append(ls.todo)
        for sc in self.states.values():
            if sc.type == "agent":
                linear_name = _resolve_linear_state_name(sc.linear_state, ls)
                if linear_name and linear_name not in seen:
                    seen.append(linear_name)
        return seen

    def gate_linear_states(self) -> list[str]:
        if self.projects:
            return self.projects[0].gate_linear_states()
        ls = self.linear_states
        seen: list[str] = []
        for sc in self.states.values():
            if sc.type == "gate":
                linear_name = _resolve_linear_state_name(sc.linear_state, ls)
                if linear_name and linear_name not in seen:
                    seen.append(linear_name)
        return seen

    def terminal_linear_states(self) -> list[str]:
        if self.projects:
            return self.projects[0].terminal_linear_states()
        return list(self.linear_states.terminal)


def _resolve_linear_state_name(key: str, ls: LinearStatesConfig) -> str:
    """Resolve a logical state key to the actual Linear state name."""
    mapping: dict[str, str] = {
        "active": ls.active,
        "awaiting_ci": ls.awaiting_ci,
        "review": ls.review,
        "gate_approved": ls.gate_approved,
        "rework": ls.rework,
        "todo": ls.todo,
        "terminal": ls.terminal[0] if ls.terminal else "Done",
    }
    return mapping.get(key, key)


def _resolve_env(val: str) -> str:
    if isinstance(val, str) and val.startswith("$"):
        return os.environ.get(val[1:], "")
    return val


def _coerce_int(val: Any, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _coerce_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        return [s.strip() for s in val.split(",") if s.strip()]
    return []


def global_prompt_paths(val: str | list[str] | None) -> list[str]:
    """Normalise `prompts.global_prompt` to an ordered list of paths.

    A workflow may name one global prompt or several. Several exist because a
    specialised global (`global-bug-fix.md`) is a *supplement* to the base one,
    not a replacement: saying "everything in global.md applies" in prose is a
    file the agent never loads, so the shared ground rules simply went missing
    from every bug-fix run. Listing both loads both, in order.
    """
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val.strip() else []
    return [str(v) for v in val if str(v).strip()]


def _parse_hooks(raw: dict[str, Any] | None) -> HooksConfig | None:
    """Parse a hooks dict into HooksConfig, returning None if empty."""
    if not raw:
        return None
    return HooksConfig(
        after_create=raw.get("after_create"),
        before_run=raw.get("before_run"),
        after_run=raw.get("after_run"),
        before_remove=raw.get("before_remove"),
        on_stage_enter=raw.get("on_stage_enter"),
        timeout_ms=_coerce_int(raw.get("timeout_ms"), 60_000),
    )


def _parse_state_config(name: str, raw: dict[str, Any]) -> StateConfig:
    """Parse a single state entry from YAML into StateConfig."""
    allowed = raw.get("allowed_tools")
    hooks_raw = raw.get("hooks")

    return StateConfig(
        name=name,
        type=str(raw.get("type", "agent")),
        prompt=raw.get("prompt"),
        linear_state=str(raw.get("linear_state", "active")),
        runner=str(raw.get("runner", "claude")),
        model=raw.get("model"),
        max_turns=raw.get("max_turns"),
        effort=raw.get("effort"),
        fallback_model=raw.get("fallback_model"),
        turn_timeout_ms=raw.get("turn_timeout_ms"),
        stall_timeout_ms=raw.get("stall_timeout_ms"),
        session=str(raw.get("session", "inherit")),
        permission_mode=raw.get("permission_mode"),
        allowed_tools=_coerce_list(allowed) if allowed is not None else None,
        rework_to=raw.get("rework_to"),
        max_rework=raw.get("max_rework"),
        skip_labels=_coerce_list(raw.get("skip_labels")),
        transitions=raw.get("transitions") or {},
        hooks=_parse_hooks(hooks_raw) if hooks_raw else None,
        docker_image=raw.get("docker_image") or (raw.get("docker", {}) or {}).get("image"),
    )


def merge_state_config(
    state: StateConfig, root_claude: ClaudeConfig, root_hooks: HooksConfig
) -> tuple[ClaudeConfig, HooksConfig]:
    """Merge state overrides with root defaults. Returns (claude_cfg, hooks_cfg)."""
    claude = ClaudeConfig(
        command=root_claude.command,
        permission_mode=state.permission_mode or root_claude.permission_mode,
        allowed_tools=state.allowed_tools if state.allowed_tools is not None else root_claude.allowed_tools,
        model=state.model or root_claude.model,
        max_turns=state.max_turns if state.max_turns is not None else root_claude.max_turns,
        effort=state.effort if state.effort is not None else root_claude.effort,
        fallback_model=(
            state.fallback_model if state.fallback_model is not None
            else root_claude.fallback_model
        ),
        turn_timeout_ms=state.turn_timeout_ms if state.turn_timeout_ms is not None else root_claude.turn_timeout_ms,
        stall_timeout_ms=state.stall_timeout_ms if state.stall_timeout_ms is not None else root_claude.stall_timeout_ms,
        append_system_prompt=root_claude.append_system_prompt,
    )
    hooks = state.hooks if state.hooks is not None else root_hooks
    return claude, hooks


# ── Helpers for parsing the per-project block ───────────────────────────────

def _parse_tracker(raw: dict[str, Any]) -> TrackerConfig:
    return TrackerConfig(
        kind=str(raw.get("kind", "linear")),
        endpoint=str(raw.get("endpoint", "https://api.linear.app/graphql")),
        api_key=str(raw.get("api_key", "")),
        project_slug=str(raw.get("project_slug", "")),
    )


def _parse_workspace(raw: dict[str, Any]) -> WorkspaceConfig:
    return WorkspaceConfig(root=str(raw.get("root", "")))


def _parse_full_hooks(raw: dict[str, Any]) -> HooksConfig:
    return HooksConfig(
        after_create=raw.get("after_create"),
        before_run=raw.get("before_run"),
        after_run=raw.get("after_run"),
        before_remove=raw.get("before_remove"),
        on_stage_enter=raw.get("on_stage_enter"),
        timeout_ms=_coerce_int(raw.get("timeout_ms"), 60_000),
    )


def _parse_claude(raw: dict[str, Any]) -> ClaudeConfig:
    return ClaudeConfig(
        command=str(raw.get("command", "claude")),
        permission_mode=str(raw.get("permission_mode", "auto")),
        allowed_tools=_coerce_list(raw.get("allowed_tools"))
        or ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
        model=raw.get("model"),
        max_turns=_coerce_int(raw.get("max_turns"), 20),
        effort=raw.get("effort"),
        fallback_model=raw.get("fallback_model"),
        turn_timeout_ms=_coerce_int(raw.get("turn_timeout_ms"), 3_600_000),
        stall_timeout_ms=_coerce_int(raw.get("stall_timeout_ms"), 300_000),
        append_system_prompt=raw.get("append_system_prompt"),
    )


def _parse_linear_states(raw: dict[str, Any]) -> LinearStatesConfig:
    return LinearStatesConfig(
        todo=str(raw.get("todo", "Todo")),
        active=str(raw.get("active", "In Progress")),
        awaiting_ci=str(raw.get("awaiting_ci", "Awaiting CI")),
        review=str(raw.get("review", "Human Review")),
        gate_approved=str(raw.get("gate_approved", "Gate Approved")),
        rework=str(raw.get("rework", "Rework")),
        terminal=_coerce_list(raw.get("terminal")) or ["Done", "Closed", "Cancelled"],
    )


def _parse_prompts(raw: dict[str, Any]) -> PromptsConfig:
    return PromptsConfig(global_prompt=raw.get("global_prompt"))


def _parse_states(raw: dict[str, Any]) -> dict[str, StateConfig]:
    out: dict[str, StateConfig] = {}
    for state_name, state_data in raw.items():
        sd = state_data or {}
        out[state_name] = _parse_state_config(state_name, sd)
    return out


def _merge_dict(default: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    """Shallow merge two YAML dicts; override wins on conflict."""
    out: dict[str, Any] = dict(default or {})
    out.update(override or {})
    return out


def _parse_routing(raw: dict[str, Any] | None) -> RoutingConfig:
    raw = raw or {}
    rules: list[RoutingRule] = []
    for entry in raw.get("rules") or []:
        if not isinstance(entry, dict):
            continue
        label, workflow = entry.get("label"), entry.get("workflow")
        if label and workflow:
            rules.append(RoutingRule(label=str(label), workflow=str(workflow)))
    return RoutingConfig(default=raw.get("default"), rules=rules)


def _load_workflow_dir(workflow_dir: Path) -> dict[str, WorkflowSpec]:
    """Load every `workflows/*.yaml` beside the config file.

    A workflow's name is its filename stem, so `workflows/bug-fix.yaml` is
    routed to as `bug-fix`. Files that fail to parse are skipped with a warning
    rather than taking the whole config down — one malformed pipeline should not
    stop the others from running.
    """
    found: dict[str, WorkflowSpec] = {}
    directory = Path(workflow_dir) / "workflows"
    if not directory.is_dir():
        return found

    # Real files are loaded after examples so an operator's `bug-fix.yaml`
    # shadows the shipped `bug-fix.example.yaml` of the same name — mirroring
    # how prompts work, and letting a fresh clone route out of the box.
    paths = sorted(directory.glob("*.y*ml"), key=lambda q: (".example." not in q.name, q.name))
    for path in paths:
        if path.name.startswith("."):
            continue
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.warning(f"Skipping unreadable workflow {path.name}: {e}")
            continue
        if not isinstance(raw, dict):
            logger.warning(f"Skipping workflow {path.name}: not a mapping")
            continue

        # `bug-fix.example.yaml` and `bug-fix.yaml` are both the `bug-fix`
        # workflow — routing names should not carry a packaging suffix.
        name = path.stem
        if name.endswith(".example"):
            name = name[: -len(".example")]

        prompts_raw = raw.get("prompts") or {}
        found[name] = WorkflowSpec(
            name=name,
            states=_parse_states(raw.get("states") or {}),
            global_prompt=prompts_raw.get("global_prompt"),
            description=str(raw.get("description") or ""),
        )
    return found


def _resolve_workflows(
    workflow_dir: Path,
    states: dict[str, StateConfig],
    prompts: PromptsConfig,
    routing_raw: dict[str, Any] | None,
) -> tuple[dict[str, WorkflowSpec], RoutingConfig]:
    """Combine workflow files with any inline `states:` block.

    An inline state machine stays valid and becomes a workflow named `default`,
    so an existing single-pipeline config keeps working untouched.
    """
    workflows = _load_workflow_dir(workflow_dir)

    if states:
        workflows.setdefault(
            "default",
            WorkflowSpec(
                name="default",
                states=states,
                global_prompt=prompts.global_prompt,
                description="Inline state machine from the main config file.",
            ),
        )

    routing = _parse_routing(routing_raw)
    if not routing.default:
        # Prefer an explicit `default` workflow, else the only one, else nothing
        # — validation reports the ambiguity rather than picking arbitrarily.
        if "default" in workflows:
            routing.default = "default"
        elif len(workflows) == 1:
            routing.default = next(iter(workflows))
    return workflows, routing


def _build_project(
    name: str,
    raw: dict[str, Any],
    defaults: dict[str, Any],
    workflow_dir: Path,
) -> ProjectConfig:
    """Build a ProjectConfig by merging top-level defaults with per-project overrides."""
    # tracker / workspace / hooks / prompts / states are project-scoped;
    # they may inherit nothing from top-level when `projects:` is used,
    # so use the project block directly. linear_states / claude inherit
    # from top-level defaults and are overlaid with per-project values.
    tracker_raw = raw.get("tracker", {}) or defaults.get("tracker", {}) or {}
    workspace_raw = raw.get("workspace", {}) or defaults.get("workspace", {}) or {}
    hooks_raw = raw.get("hooks", {}) or defaults.get("hooks", {}) or {}
    prompts_raw = raw.get("prompts", {}) or defaults.get("prompts", {}) or {}
    states_raw = raw.get("states") or defaults.get("states") or {}

    linear_states_raw = _merge_dict(defaults.get("linear_states"), raw.get("linear_states"))
    claude_raw = _merge_dict(defaults.get("claude"), raw.get("claude"))

    workflows, routing = _resolve_workflows(
        workflow_dir,
        _parse_states(states_raw),
        _parse_prompts(prompts_raw),
        _merge_dict(defaults.get("routing"), raw.get("routing")),
    )

    return ProjectConfig(
        name=name,
        paused=bool(raw.get("paused", False)),
        tracker=_parse_tracker(tracker_raw),
        workspace=_parse_workspace(workspace_raw),
        hooks=_parse_full_hooks(hooks_raw),
        prompts=_parse_prompts(prompts_raw),
        states=_parse_states(states_raw),
        workflows=workflows,
        routing=routing,
        linear_states=_parse_linear_states(linear_states_raw),
        claude=_parse_claude(claude_raw),
        workflow_dir=workflow_dir,
        max_concurrent=raw.get("max_concurrent"),
    )


def _legacy_project_name(tracker_raw: dict[str, Any], workflow_path: Path) -> str:
    """Derive a project name for a legacy single-project workflow.

    Prefer the workflow file's parent directory name (usually the repo
    name, which is what the operator thinks of the project as).
    Fall back to the project_slug prefix if there's no usable directory.
    """
    parent = workflow_path.parent.resolve().name
    if parent and parent not in (".", ""):
        return parent
    slug = str(tracker_raw.get("project_slug", "")).strip()
    if slug:
        return f"project-{slug[:8]}"
    return "default"


def parse_workflow_file(path: str | Path) -> ParsedConfig:
    """Parse a workflow file (.yaml/.yml or .md with front matter) into config."""
    path = Path(path)
    workflow_dir = path.parent
    if not path.exists():
        raise FileNotFoundError(f"Workflow file not found: {path}")

    content = path.read_text()
    config_raw: dict[str, Any] = {}
    prompt_body = ""

    # Detect format: pure YAML or markdown with front matter
    if path.suffix in (".yaml", ".yml"):
        config_raw = yaml.safe_load(content) or {}
    elif content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            config_raw = yaml.safe_load(parts[1]) or {}
            prompt_body = parts[2]
    else:
        # Try parsing as pure YAML
        config_raw = yaml.safe_load(content) or {}

    if not isinstance(config_raw, dict):
        raise ValueError("Workflow file must contain a YAML mapping")

    prompt_template = prompt_body.strip()

    # Global defaults that don't go per-project
    polling = PollingConfig(
        interval_ms=_coerce_int((config_raw.get("polling") or {}).get("interval_ms"), 30_000),
    )
    a = config_raw.get("agent", {}) or {}
    agent = AgentConfig(
        max_concurrent_agents=_coerce_int(a.get("max_concurrent_agents"), 5),
        max_retry_backoff_ms=_coerce_int(a.get("max_retry_backoff_ms"), 300_000),
        max_concurrent_agents_by_state=a.get("max_concurrent_agents_by_state") or {},
        max_concurrent_per_project=a.get("max_concurrent_per_project") or {},
    )
    s = config_raw.get("server", {}) or {}
    server = ServerConfig(port=s.get("port"), host=s.get("host"))

    # Parse logging
    lg = config_raw.get("logging", {}) or {}
    logging_cfg = LoggingConfig(
        enabled=bool(lg.get("enabled", False)),
        log_dir=str(lg.get("log_dir", "")),
        max_age_days=_coerce_int(lg.get("max_age_days"), 14),
        max_total_size_mb=_coerce_int(lg.get("max_total_size_mb"), 500),
    )

    # Resolve projects list (multi-project) or synthesize from top-level (legacy)
    projects_raw = config_raw.get("projects")
    projects: list[ProjectConfig] = []

    if projects_raw is not None:
        # Multi-project mode. Top-level tracker/workspace/hooks/prompts/states
        # are not allowed in this mode (would be ambiguous). Top-level claude
        # and linear_states ARE allowed — they act as defaults each project
        # block can override.
        if not isinstance(projects_raw, list) or not projects_raw:
            raise ValueError("`projects:` must be a non-empty list of project blocks")
        for forbidden in ("tracker", "workspace", "hooks", "prompts", "states"):
            if forbidden in config_raw:
                raise ValueError(
                    f"Top-level `{forbidden}:` is not allowed when `projects:` is used. "
                    f"Move it under each project entry."
                )
        defaults = {
            "linear_states": config_raw.get("linear_states") or {},
            "claude": config_raw.get("claude") or {},
            "routing": config_raw.get("routing") or {},
        }
        seen_names: set[str] = set()
        for idx, raw in enumerate(projects_raw):
            if not isinstance(raw, dict):
                raise ValueError(f"`projects[{idx}]` must be a mapping")
            name = str(raw.get("name", "")).strip()
            if not name:
                raise ValueError(f"`projects[{idx}].name` is required")
            if name in seen_names:
                raise ValueError(f"Duplicate project name: {name}")
            seen_names.add(name)
            projects.append(_build_project(name, raw, defaults, workflow_dir))
    else:
        # Legacy single-project mode. Build one ProjectConfig from top-level.
        tracker_raw = config_raw.get("tracker", {}) or {}
        synthetic_raw = {
            "tracker": tracker_raw,
            "workspace": config_raw.get("workspace") or {},
            "hooks": config_raw.get("hooks") or {},
            "prompts": config_raw.get("prompts") or {},
            "states": config_raw.get("states") or {},
            "linear_states": config_raw.get("linear_states") or {},
            "claude": config_raw.get("claude") or {},
            "routing": config_raw.get("routing") or {},
        }
        name = _legacy_project_name(tracker_raw, path)
        projects.append(_build_project(name, synthetic_raw, {}, workflow_dir))

    # Populate top-level fields from projects[0] for backward compat.
    p0 = projects[0]
    # Parse workflows
    workflows_raw = config_raw.get("workflows", {}) or {}
    workflows: dict[str, WorkflowConfig] = {}

    if workflows_raw:
        # Multi-workflow mode: parse each workflow entry
        for wf_name, wf_data in workflows_raw.items():
            wd = wf_data or {}
            label = wd.get("label")
            default = bool(wd.get("default", False))
            triage = bool(wd.get("triage", False))
            path = _coerce_list(wd.get("path"))
            terminal_state = str(wd.get("terminal_state", "terminal"))
            transitions = derive_workflow_transitions(path, states)
            # Find entry_state: first agent state in path
            entry = ""
            for name in path:
                sc = states.get(name)
                if sc and sc.type == "agent":
                    entry = name
                    break
            workflows[wf_name] = WorkflowConfig(
                name=wf_name,
                label=label,
                default=default,
                path=path,
                terminal_state=terminal_state,
                transitions=transitions,
                entry_state=entry,
                triage=triage,
            )
    else:
        # Legacy/backward compat: synthesize a single _default workflow
        # using StateConfig.transitions verbatim (do NOT call derive_workflow_transitions)
        path = list(states.keys())
        transitions = {name: dict(sc.transitions) for name, sc in states.items()}
        entry = ""
        for name, sc in states.items():
            if sc.type == "agent":
                entry = name
                break
        workflows["_default"] = WorkflowConfig(
            name="_default",
            label=None,
            default=True,
            path=path,
            terminal_state="terminal",
            transitions=transitions,
            entry_state=entry,
        )

    # Parse repos registry. Distinguish "absent" from "explicit empty dict":
    # absent triggers legacy synthesis; explicit-empty is a config error that
    # surfaces via validate_config.
    repos_raw = config_raw.get("repos", None)
    repos: dict[str, RepoConfig] = {}
    repos_synthesized = False

    if isinstance(repos_raw, dict) and repos_raw:
        # Explicit registry with at least one entry
        for repo_name, repo_data in repos_raw.items():
            rd = repo_data or {}
            repos[repo_name] = RepoConfig(
                name=repo_name,
                label=rd.get("label"),
                clone_url=str(rd.get("clone_url", "")),
                default=bool(rd.get("default", False)),
                docker_image=rd.get("docker_image"),
            )
    elif repos_raw is None:
        # Absent: synthesize a _default entry for backward compat.
        # Mirrors the multi-workflow _default synthesis at the workflows
        # branch above. The synthetic entry carries sentinel values
        # (empty clone_url, None label) and is exempt from R21 validation.
        repos["_default"] = RepoConfig(
            name="_default",
            label=None,
            clone_url="",
            default=True,
            docker_image=None,
        )
        repos_synthesized = True
    # else: repos_raw is explicit empty dict ({}) — leave repos empty;
    # validate_config will surface the error.

    cfg = ServiceConfig(
        tracker=p0.tracker,
        polling=polling,
        workspace=p0.workspace,
        hooks=p0.hooks,
        claude=p0.claude,
        agent=agent,
        server=server,
        logging=logging_cfg,
        docker=docker,
        workflows=workflows,
        linear_states=p0.linear_states,
        prompts=p0.prompts,
        states=p0.states,
        # ServiceConfig mirrors the first project so single-project reads keep
        # working; per-project routing is read from the project itself.
        workflows=p0.workflows,
        routing=p0.routing,
        projects=projects,
        workflow_dir=workflow_dir,
        repos=repos,
        repos_synthesized=repos_synthesized,
    )

    return ParsedConfig(config=cfg, prompt_template=prompt_template)


def _prompt_exists(workflow_dir: Path, prompt: str) -> bool:
    """Resolve a prompt path the same way the runtime does."""
    candidate = Path(prompt)
    if not candidate.is_absolute():
        candidate = Path(workflow_dir) / candidate
    return candidate.is_file()


def _validate_project(project: ProjectConfig, errors: list[str]) -> None:
    """Validate a single project's state machine and tracker."""
    prefix = f"project '{project.name}'"

    # Detect legacy vs multi-workflow mode
    is_legacy = len(cfg.workflows) == 1 and "_default" in cfg.workflows

    if project.tracker.kind != "linear":
        errors.append(f"{prefix}: unsupported tracker kind: {project.tracker.kind}")
    if not project.resolved_api_key():
        errors.append(f"{prefix}: missing tracker API key")
    if not project.tracker.project_slug:
        errors.append(f"{prefix}: missing tracker.project_slug")

    for gp in global_prompt_paths(getattr(project.prompts, "global_prompt", None)):
        if not _prompt_exists(project.workflow_dir, gp):
            errors.append(f"{prefix}: global prompt not found: {gp}")

    # Routing sanity: every rule and the default must name a real workflow.
    for rule in project.routing.rules:
        if rule.workflow not in project.workflows:
            errors.append(
                f"{prefix}: routing rule for label '{rule.label}' points at "
                f"unknown workflow '{rule.workflow}'"
            )
    if project.routing.default and project.routing.default not in project.workflows:
        errors.append(
            f"{prefix}: default workflow '{project.routing.default}' does not exist"
        )
    if project.workflows and not project.routing.default:
        errors.append(
            f"{prefix}: {len(project.workflows)} workflows defined but no "
            f"routing.default — an unlabelled issue would have nowhere to go"
        )

    if not project.workflows:
        errors.append(f"{prefix}: no states defined")
        return

    # Validate EVERY workflow. An inline `states:` block has already been folded
    # in as `default`, so this covers both shapes — and a config carrying both
    # gets both checked, rather than the inline block masking a broken workflow
    # file.
    for wf_name, wf in project.workflows.items():
        label = prefix if wf_name == "default" and not _has_workflow_files(project) \
            else f"{prefix} workflow '{wf_name}'"
        _validate_states(wf.states, project, label, errors,
                         global_prompt=wf.global_prompt)


def _has_workflow_files(project: ProjectConfig) -> bool:
    """Whether this project has any `workflows/*.yaml` beside its config.

    Only affects error wording: a single-pipeline config should not suddenly
    report errors against "workflow 'default'" that the operator never named.
    """
    return (Path(project.workflow_dir) / "workflows").is_dir()


def _validate_states(
    states: dict[str, StateConfig],
    project: ProjectConfig,
    prefix: str,
    errors: list[str],
    global_prompt: str | list[str] | None = None,
) -> None:
    """Validate one state machine.

    Extracted so each workflow is checked on its own terms: a transition
    target only has to exist inside its own pipeline, and two workflows may
    legitimately share a state name.
    """
    for gp in global_prompt_paths(global_prompt):
        if not _prompt_exists(project.workflow_dir, gp):
            errors.append(f"{prefix}: global prompt not found: {gp}")

    valid_linear_keys = {"active", "awaiting_ci", "review", "gate_approved", "rework", "terminal"}
    has_agent = False
    has_terminal = False
    all_state_names = set(states.keys())

    for name, sc in states.items():
        if sc.type not in ("agent", "gate", "terminal"):
            errors.append(f"{prefix} state '{name}': invalid type: {sc.type}")
            continue

        if sc.type == "agent":
            has_agent = True
            if not sc.prompt:
                errors.append(f"{prefix} state '{name}': agent state missing 'prompt' field")
            elif not _prompt_exists(project.workflow_dir, sc.prompt):
                # Caught here rather than at dispatch: a typo'd path otherwise
                # sails through startup and only fails when the agent launches,
                # which is the most expensive moment to discover it.
                errors.append(
                    f"{prefix} state '{name}': prompt file not found: {sc.prompt}"
                )

        elif sc.type == "gate":
            if is_legacy:
                # Gates must have rework_to
                if not sc.rework_to:
                    errors.append(f"Gate state '{name}' is missing 'rework_to' field")
                elif sc.rework_to not in all_state_names:
                    errors.append(
                        f"Gate state '{name}' rework_to target '{sc.rework_to}' "
                        f"is not a defined state"
                    )
                # Gates must have approve transition
                if "approve" not in sc.transitions:
                    errors.append(f"Gate state '{name}' is missing 'approve' transition")
            # (in multi-workflow mode, gate rework_to and approve are validated per-workflow below)

        elif sc.type == "terminal":
            has_terminal = True

        if sc.linear_state not in valid_linear_keys:
            errors.append(
                f"{prefix} state '{name}': invalid linear_state '{sc.linear_state}' "
                f"(valid: {', '.join(sorted(valid_linear_keys))})"
            )

        for trigger, target in sc.transitions.items():
            if target not in all_state_names:
                errors.append(
                    f"{prefix} state '{name}': transition '{trigger}' points to "
                    f"unknown state '{target}'"
                )

    if not has_agent:
        errors.append(f"{prefix}: no agent states defined")
    if not has_terminal:
        errors.append(f"{prefix}: no terminal states defined")

    # In multi-workflow mode, error if any StateConfig has explicit transitions
    if not is_legacy:
        for name, sc in cfg.states.items():
            if sc.transitions:
                errors.append(
                    f"State '{name}' has explicit transitions in multi-workflow mode; "
                    f"transitions are derived from workflow paths"
                )

    # --- Workflow validation ---
    # Exactly one default workflow
    default_count = sum(1 for wf in cfg.workflows.values() if wf.default)
    if default_count == 0:
        errors.append("No default workflow defined (exactly one workflow must have default: true)")
    elif default_count > 1:
        errors.append(
            f"Multiple default workflows defined ({default_count}); "
            f"exactly one workflow must have default: true"
        )

    # No duplicate labels across workflows
    seen_labels: dict[str, str] = {}  # label -> workflow name
    for wf in cfg.workflows.values():
        if wf.label is not None:
            label_lower = wf.label.lower()
            if label_lower in seen_labels:
                errors.append(
                    f"Duplicate label '{wf.label}' on workflows "
                    f"'{seen_labels[label_lower]}' and '{wf.name}'"
                )
            else:
                seen_labels[label_lower] = wf.name

    # Per-workflow validation
    all_referenced_states: set[str] = set()
    for wf in cfg.workflows.values():
        # Every path entry must reference an existing state
        for state_name in wf.path:
            if state_name not in all_state_names:
                errors.append(
                    f"Workflow '{wf.name}' path references non-existent state '{state_name}'"
                )

        all_referenced_states.update(wf.path)

        # Each workflow path must contain at least one agent state
        has_path_agent = any(
            cfg.states.get(s) and cfg.states[s].type == "agent"
            for s in wf.path
        )
        if not has_path_agent:
            errors.append(
                f"Workflow '{wf.name}' path contains no agent states "
                f"(need at least one state with type 'agent')"
            )

        # Each workflow path must end with a terminal state
        if wf.path:
            last_state = wf.path[-1]
            last_sc = cfg.states.get(last_state)
            if last_sc and last_sc.type != "terminal":
                errors.append(
                    f"Workflow '{wf.name}' path must end with a terminal state "
                    f"('{last_state}' has type '{last_sc.type}')"
                )
        else:
            errors.append(f"Workflow '{wf.name}' has an empty path")

        # Validate terminal_state key
        valid_terminal_keys = {"terminal", "todo", "active", "review", "gate_approved", "rework"}
        if wf.terminal_state not in valid_terminal_keys:
            errors.append(
                f"Workflow '{wf.name}' has invalid terminal_state: '{wf.terminal_state}' "
                f"(must be a valid LinearStatesConfig key)"
            )
        if wf.terminal_state in ("active",):
            log.warning(
                "Workflow '%s' terminal_state resolves to an active state "
                "('%s') — this could cause dispatch loops",
                wf.name, wf.terminal_state,
            )

        # Gate validation within path context
        for i, state_name in enumerate(wf.path):
            sc = cfg.states.get(state_name)
            if not sc or sc.type != "gate":
                continue

            # Gate must have resolvable approve (next state in path)
            wf_transitions = wf.transitions.get(state_name, {})
            if "approve" not in wf_transitions:
                errors.append(
                    f"Gate '{state_name}' in workflow '{wf.name}' has no resolvable "
                    f"approve transition (no next state in path)"
                )

            # Gate must have resolvable rework_to
            if "rework_to" not in wf_transitions:
                # Check if the gate's StateConfig.rework_to is set
                if not sc.rework_to:
                    errors.append(
                        f"Gate '{state_name}' in workflow '{wf.name}' has no resolvable "
                        f"rework_to (no explicit rework_to and no prior agent state in path)"
                    )

        # Per-workflow reachability: walk this workflow's transition graph
        wf_entry = wf.entry_state
        wf_reachable: set[str] = set()
        if wf_entry:
            wf_reachable.add(wf_entry)
        for state_name, state_transitions in wf.transitions.items():
            for target in state_transitions.values():
                wf_reachable.add(target)
        wf_path_set = set(wf.path)
        wf_unreachable = wf_path_set - wf_reachable
        for name in wf_unreachable:
            log.warning(
                "State '%s' is unreachable in workflow '%s' "
                "(no transitions lead to it)",
                name, wf.name,
            )

    # Warn if a state in the pool is not referenced by any workflow path
    unreferenced = all_state_names - all_referenced_states
    for name in unreferenced:
        log.warning(
            "State '%s' is defined but not referenced by any workflow path", name
        )

    # Legacy mode: also run the original unreachable-states check using
    # StateConfig.transitions (the synthesized _default workflow copies them
    # verbatim, so the per-workflow check above covers this — but we keep
    # the original check for backward compatibility in case the logic diverges)
    if is_legacy:
        entry = cfg.entry_state
        reachable: set[str] = set()
        if entry:
            reachable.add(entry)
        for sc in cfg.states.values():
            for target in sc.transitions.values():
                reachable.add(target)
            if sc.rework_to:
                reachable.add(sc.rework_to)

        unreachable = all_state_names - reachable
        for name in unreachable:
            log.warning("State '%s' is unreachable (no transitions lead to it)", name)

def validate_config(cfg: ServiceConfig) -> list[str]:
    """Validate state machine config for dispatch readiness. Returns list of errors."""
    errors: list[str] = []
    if not cfg.projects:
        errors.append("No projects defined")
        return errors
    for project in cfg.projects:
        _validate_project(project, errors)
    # Docker validation
    if cfg.docker.enabled:
        if not cfg.docker.default_image:
            errors.append("docker.enabled is true but docker.default_image is not set")
        if cfg.docker.inherit_claude_config:
            host_dir = os.path.expandvars(os.path.expanduser(cfg.docker.host_claude_dir))
            # Only warn if running locally — in DooD mode this is a host path
            # that won't exist inside the orchestrator container
            if not os.environ.get("HOST_HOME") and not Path(host_dir).exists():
                log.warning(
                    "docker.host_claude_dir '%s' does not exist — "
                    "agents may fail to authenticate",
                    host_dir,
                )
            # In DooD mode (orchestrator inside a container), the orchestrator
            # cannot write host-visible temp files without an operator-provided
            # shim. Require explicit shim config whenever a Claude Code state
            # exists — Codex-only workflows don't consume plugin config and are
            # exempt from the shim requirement.
            if os.path.exists("/.dockerenv"):
                claude_states = [
                    name for name, sc in cfg.states.items()
                    if sc.type == "agent" and sc.runner == "claude"
                ]
                if claude_states:
                    missing = []
                    if not cfg.docker.host_claude_dir_mount:
                        missing.append("docker.host_claude_dir_mount")
                    if not cfg.docker.plugin_shim_host_path:
                        missing.append("docker.plugin_shim_host_path")
                    if not cfg.docker.plugin_shim_container_path:
                        missing.append("docker.plugin_shim_container_path")
                    if missing:
                        state_list = ", ".join(sorted(claude_states))
                        errors.append(
                            "Docker-in-Docker mode detected with inherit_claude_config: true "
                            f"and Claude Code state(s) present ({state_list}), "
                            f"but required shim fields are not set: {', '.join(missing)}. "
                            "These fields are needed to rewrite plugin paths without touching "
                            "host files. See CLAUDE.md (Docker mode) for setup."
                        )
    for name, sc in cfg.states.items():
        if sc.docker_image and not cfg.docker.enabled:
            log.warning(
                "State '%s' has docker_image set but docker.enabled is false", name
            )
        if sc.skip_labels and sc.type != "gate":
            log.warning(
                "State '%s' has skip_labels but is not a gate — labels will be ignored",
                name,
            )

    # Warn if max_concurrent_agents_by_state keys don't match state names
    for state_key in cfg.agent.max_concurrent_agents_by_state:
        if state_key not in cfg.states:
            log.warning(
                "max_concurrent_agents_by_state key '%s' does not match any defined state",
                state_key,
            )

    # Validate logging
    if cfg.logging.enabled and not cfg.logging.log_dir:
        log.warning("logging.enabled is true but log_dir is not set")

    return errors
