# Stokowski

Claude Code adaptation of [OpenAI's Symphony](https://github.com/openai/symphony). Orchestrates Claude Code agents via Linear issues.

This file is the single source of truth for contributors. It covers architecture, design decisions, key behaviours, and how to work on the codebase.

---

## What it does

Stokowski is a long-running Python daemon that:
1. Polls Linear for issues in configured active states
2. Creates an isolated git-cloned workspace per issue
3. Launches Claude Code (`claude -p`) in that workspace
4. Manages multi-turn sessions via `--resume <session_id>`
5. Retries failures with exponential backoff
6. Reconciles running agents against Linear state changes
7. Exposes a live web dashboard and terminal UI

The agent prompt, runtime config, and workspace setup all live in `workflow.yaml` in the operator's directory — not in this codebase.

---

## Package structure

```
stokowski/
  artifacts.py     Agent evidence: collection, git isolation, cleanup
  config.py        workflow.yaml parser + typed config dataclasses
  events.py        stream-json event parsing -> RunAttempt state
  ledger.py        Append-only run log + approval-rate summary
  linear.py        Linear GraphQL client (httpx async)
  model_catalogue.py  Known models by provider (data, for the studio dropdown)
  models.py        Domain models: Issue, RunAttempt, RetryEntry
  # workflows/*.yaml alongside the config hold one pipeline each
  orchestrator.py  Main poll loop, dispatch, reconciliation, retry
  prompt.py        Three-layer prompt assembly + the agent reporting contract
  report.py        Structured run reports -> rendered Linear comments
  studio.py        Comment-preserving config editing behind the dashboard
  runner.py        Claude Code CLI integration, subprocess lifecycle
  tracking.py      State machine tracking via Linear attachments + conditional comments
  workspace.py     Per-issue workspace lifecycle and hooks
  web.py           Optional FastAPI dashboard
  main.py          CLI entry point, keyboard handler
  __main__.py      Enables python -m stokowski
```

---

## Key design decisions

### Claude Code CLI instead of Codex app-server
Symphony uses Codex's JSON-RPC `app-server` protocol over stdio. Stokowski uses Claude Code's CLI:
- First turn: `claude -p "<prompt>" --output-format stream-json --verbose`
- Continuation: `claude -p "<prompt>" --resume <session_id> --output-format stream-json --verbose`

`--verbose` is required for `stream-json` to work. `session_id` is extracted from the `result` event in the NDJSON stream.

### Python + asyncio instead of Elixir/OTP
Simpler operational story — single process, no BEAM runtime, no distributed concerns. Concurrency via `asyncio.create_task`. Each agent turn is a subprocess launched with `asyncio.create_subprocess_exec`.

### No persistent database
All state lives in memory. The orchestrator recovers from restart by re-polling Linear and re-discovering active issues. Workspace directories on disk act as durable state.

### Many workflows, one runtime
A Linear label chooses which pipeline runs. `workflows/*.yaml` hold one state
machine and one global prompt each; `workflow.yaml` keeps everything else —
tracker, workspace, hooks, concurrency, server — because duplicating those per
pipeline would mean three places to rotate an API key.

Routing rules are evaluated in order, first match wins, with a `default` for
unlabelled issues. The workflow is **pinned at first dispatch** and written into
the tracking comment: relabelling a ticket mid-run must not move it onto a
different state machine, and a restart recovers the pin from Linear rather than
re-routing from labels that may have changed.

This is what removes `if this is a bug…` branching from prompts. Each workflow's
**global prompt** says once what kind of work this is, so a stage prompt shared
by several workflows (`review.md`, `merge.md`) needs no conditional. Sharing is
just two workflows naming the same path — there is no separate mechanism.

`workflows/x.example.yaml` and `workflows/x.yaml` are both the workflow `x`; a
real file shadows the shipped example, mirroring how prompts work.

An inline `states:` block still works and is folded in as a workflow named
`default`, so a single-pipeline config runs untouched.

### workflow.yaml as the operator contract
The operator's `workflow.yaml` defines the runtime config and state machine. Stokowski re-parses it on every poll tick — config changes take effect without restart. Both `.yaml` and legacy `.md` (YAML front matter + Jinja2 body) formats are supported. Prompt templates are now separate `.md` files referenced by path from the config.

### Multi-project polling
Stokowski supports polling N Linear projects from one orchestrator process by binding multiple `workflow.<project>.yml` files. Each file stays a complete self-contained `ServiceConfig` — no nested schema. The orchestrator multiplexes via `self.configs: dict[project_slug, ParsedConfig]` plus a per-issue `_issue_project: dict[issue_id, project_slug]` cache; every per-issue config lookup routes through `self._cfg_for_issue(issue_id)`.

Operators invoke with a single file (legacy), a directory, a glob, or an explicit list:
```bash
stokowski workflow.yaml                 # single project (legacy, unchanged)
stokowski workflows/                    # directory → every *.yaml/.yml inside
stokowski 'workflow.*.yml'              # glob
stokowski workflow.a.yml workflow.b.yml # explicit list
```

Config settings split three ways:
- *Shared globals, first-file-wins (alphabetical, case-insensitive):* `agent.max_concurrent_agents`, `server.port`.
- *Shared globals, min across files:* `polling.interval_ms` (tightest interval wins).
- *Per-project (isolated by issue.id):* `workspace.root`, `hooks`, `docker.*`, `claude`, `linear_states`, `states`, `workflows`, `repos`, `agent.max_concurrent_agents_by_state`.

Each project has its own `LinearClient` (supports per-project `tracker.api_key`). Broken files are isolated — healthy projects keep dispatching on hot-reload failures.

### State machine workflow
Each workflow defines a set of internal states that map to Linear states. States have types: `agent` (runs Claude Code), `evaluator` (agent-evaluates-agent), `gate` (waits for human review), or `terminal` (issue complete). Transitions between states are declared explicitly in config.

**Three-layer prompt assembly:** Every agent turn's prompt is built from three layers concatenated together:
1. **Global prompt** — shared context loaded from a `.md` file (referenced by `prompts.global_prompt`).
   `global_prompt` also takes a **list** of paths, loaded in order, so a specialised
   global supplements the shared one instead of replacing it. `global-bug-fix.md` used to
   say "everything in `global.md` applies" in prose — a file nothing loaded, so every
   bug-fix run shipped without the grounding and evidence rules it claimed to inherit.
   `config.global_prompt_paths()` normalises either shape.
2. **Stage prompt** — state-specific instructions loaded from the state's `prompt` path
3. **Lifecycle injection** — auto-generated section with issue metadata, transitions, rework context, and recent comments

**Grounding before gates:** The example pipeline puts an independent
`ground-check` agent between `investigate` and the first human gate, running
`session: fresh`. The failure it targets is an investigation that reasoned
correctly from the wrong data — wrong environment, dead column, unreproducible
number. That report is fluent, internally consistent and wrong, which is
exactly why a human gate does not catch it: a reviewer reading prose cannot
distinguish a well-sourced conclusion from a well-written one.

The fresh session is load-bearing rather than cosmetic. A continued session
inherits the assumptions it is supposed to be testing and will confirm them.
`tests/test_example_prompts.py` asserts both properties, so a future edit that
routes `investigate` straight to a gate fails the suite.

**Gate protocol:** When an agent completes a state that transitions to a gate, Stokowski moves the issue to the gate's Linear state and upserts the attachment. Humans approve or request rework via Linear state changes. On approval, Stokowski advances to the gate's `approve` transition target. On rework, it returns to the gate's `rework_to` state and posts a human-readable comment noting the rejection.

**Attachment-based state tracking:** The state machine position is stored in a Linear Attachment (`stokowski://state/{identifier}`) with a `metadata` JSON field (API-only, invisible in UI). `_upsert_state()` in the orchestrator is the single entry point for state persistence — it upserts the attachment and conditionally posts a human-readable comment. Comments fire only when they carry information the Linear ticket state does not convey: rework requested (which gate rejected, where work returns to), escalation (max rework exceeded), or evaluation findings (review-required tier). State transitions, gate waiting, gate approved, and evaluation approve are all silent. Crash recovery reads the attachment first (`fetch_stokowski_attachment`), falling back to legacy comment scanning (`parse_latest_tracking`) during migration from pre-attachment issues.

### Evaluator state type (agent-evaluates-agent)
Evaluators are first-class states (`type: evaluator`) that sit in the workflow path before gates. They run like agents (subprocess, prompt, concurrency tracking) but parse their output for a tier verdict (`approve` or `review-required`). On `approve` + `auto_approve: true`, the evaluator transitions past the gate directly to the gate's approve target. Otherwise, it enters the gate for human review. This is the Compound Engineering pattern — staged AI pipelines where each stage validates the previous one.

**Evaluation output contract:** The evaluator must emit `<!-- stokowski:evaluation {"tier": "...", "summary": "...", "findings": [...]} -->` in its result text. Stokowski parses this using last-match semantics (same as `TRANSITION_PATTERN`). Fallback keyword search always returns `review-required`. Parse failure defaults to `review-required` (fail-safe). A human-readable comment with findings is posted only for `review-required` tier; `approve` tier is silent (the attachment records the verdict).

**Known limitation:** Fresh session prevents anchoring to the agent's conversational reasoning, but the evaluator still reads the agent's artifacts (code, comments, commit messages). This is a "second pass" not an "independent audit."

### Workspace isolation
Each issue gets its own directory under `workspace.root`. Agents run with `cwd` set to that directory. Workspaces persist across turns for the same session; they're deleted when the issue reaches a terminal state. The workspace path is `{workspace.root}/{issue_identifier}-{repo_name}` — legacy 1:1 configs synthesize `repo_name="_default"` and get paths like `{root}/SMI-14-_default`.

### Multi-repo support
A Linear project may map to N repos via the optional `repos:` registry in `workflow.yaml`. Each entry is `{name, label, clone_url, default, docker_image?}`. Tickets are routed to a repo by `repo:<name>` Linear label — triage agents apply labels, or humans pre-apply.

- **Registry resolution:** `ServiceConfig.resolve_repo(issue)` mirrors `resolve_workflow` — case-insensitive label match (first wins), fallback to the `default: true` repo. `Orchestrator._issue_repo` caches the repo name per-issue with the same three-tier hot-reload fallback used for `_issue_workflow`.
- **Legacy synthesis:** configs without a `repos:` section get a synthetic `_default` entry with `label=None`, `clone_url=""`, `default=True`, and `repos_synthesized=True` on the ServiceConfig. This preserves R19 backward compatibility.
- **Jinja hook rendering:** root hooks are rendered at dispatch-time with `StrictUndefined` and a `repo` namespace (`{{ repo.name }}`, `{{ repo.clone_url }}`, `{{ repo.label }}`). Rendering is **gated on `repos_synthesized == False`** — legacy configs pass hooks through verbatim so shell bodies with literal `{`/`}` (e.g., `git config credential.helper '!f() { ...; }; f'`) continue to work unchanged.
- **`docker_image` 3-level hybrid:** resolution precedence is `StateConfig.docker_image` → `RepoConfig.docker_image` → `docker.default_image`. Startup pre-pull unions all three sources.
- **R10 single-repo cap (v1):** tickets with > 1 `repo:*` label are rejected via an async pre-pass in `_tick` (before the sync eligibility loop). The pre-pass fetches comments, dedups via `<!-- stokowski:rejected {...} -->` sentinels, posts a rejection comment, and marks `_rejected_issues`. `_is_eligible` simply observes set membership. Label-change invalidation clears stale markers so dispatch resumes when operators fix the labels.
- **Triage routing:** when the dispatching workflow has `triage: true`, the orchestrator injects `STOKOWSKI_REPOS_JSON` (`[{name, label, clone_url}, ...]`, excluding `_default`). The triage prompt classifies the issue and applies `repo:<name>` and `workflow:<name>` labels; the issue re-enters the dispatch cycle with the labels applied.
- **Cold-start recovery:** tracking comments now carry a `repo` field. On restart, `_resolve_repo_for_coldstart` reads the field with a defensive `.get('repo') or fallback` pattern. Pre-multi-repo tracking threads (no `repo` field) fall back to label resolution and post a one-time `<!-- stokowski:migrated {...} -->` notice when the result is `_default`.

### Headless system prompt
Every first-turn launch appends a system prompt via `--append-system-prompt` that instructs Claude not to use interactive skills, slash commands, or plan mode. This prevents agents from stalling on interactive workflows.

---

## Component deep-dives

### config.py
Parses `workflow.yaml` (or legacy `.md` with front matter) into typed dataclasses:
- `TrackerConfig` — Linear endpoint, API key, project slug
- `PollingConfig` — interval
- `WorkspaceConfig` — root path (supports `~` and `$VAR` expansion). Relative paths are resolved from the workflow directory via `resolved_root(base)`.
- `HooksConfig` — shell scripts for lifecycle events + timeout (includes `on_stage_enter`)
- `ClaudeConfig` — command, permission mode, model, timeouts, system prompt
- `AgentConfig` — concurrency limits (global + per-state)
- `ServerConfig` — optional web dashboard port
- `LoggingConfig` — agent run log retention: `enabled` (default false), `log_dir` (supports `~` and `$VAR`), `max_age_days` (default 14), `max_total_size_mb` (default 500). `resolved_log_dir()` expands path variables.
- `LinearStatesConfig` — maps logical state names (`todo`, `active`, `review`, `gate_approved`, `rework`, `terminal`) to actual Linear state names. Issues in the `todo` state are picked up and automatically moved to `active` on dispatch.
- `PromptsConfig` — global prompt file reference; `evaluator_prompt` is the default prompt file for evaluator states (used when state has no `prompt` set)
- `StateConfig` — a single state in the state machine: type (`agent`, `evaluator`, `gate`, `terminal`), prompt path, linear_state key, runner, session mode, transitions, per-state overrides (model, max_turns, timeouts, hooks), gate-specific fields (rework_to, max_rework), evaluator-specific fields (`auto_approve` — whether "approve" tier skips the gate, default false)

`ServiceConfig` provides helper methods: `entry_state` (first agent state), `active_linear_states()`, `gate_linear_states()`, `terminal_linear_states()`.

`merge_state_config(state, root_claude, root_hooks)` merges per-state overrides with root defaults — only specified fields are overridden. Returns `(ClaudeConfig, HooksConfig)`.

`parse_workflow_file()` detects format by file extension: `.yaml`/`.yml` files are parsed as pure YAML; `.md` files are split on `---` delimiters for front matter + body.

`validate_config()` checks state machine integrity: all transitions point to existing states, gates have `rework_to` and `approve` transition, at least one agent and one terminal state exist, warns about unreachable states.

`ServiceConfig.resolved_api_key()` resolves the key in priority order:
1. Literal value in YAML
2. `$VAR` reference resolved from env
3. `LINEAR_API_KEY` env var as fallback

### linear.py
Async GraphQL client over httpx. Three queries:
- `fetch_candidate_issues()` — paginated, fetches all issues in active states with full detail (labels, blockers, branch name)
- `fetch_issue_states_by_ids()` — lightweight reconciliation query, returns `{id: state_name}`
- `fetch_issues_by_states()` — used on startup cleanup, returns minimal Issue objects

Note: the reconciliation query uses `issues(filter: { id: { in: $ids } })` — not `nodes(ids:)` which doesn't exist in Linear's API.

### models.py
Three dataclasses:
- `Issue` — normalized Linear issue. `title` is required even for minimal fetches (use `title=""`).
- `RunAttempt` — per-issue runtime state: session_id, turn count, token usage, status, last message, result_text (full result text from the NDJSON result event, capped at 10K chars — used by evaluator tier parsing)
- `RetryEntry` — retry queue entry with due time and error

### orchestrator.py
The main loop. `start()` runs until `stop()` is called:

```
while running:
    _tick()          # reconcile → fetch → dispatch
    sleep(interval)  # interruptible via asyncio.Event
```

**Dispatch logic:**
1. Issues sorted by priority (lower = higher), then created_at, then identifier
2. `_is_eligible()` checks: valid fields, active state, not already running/claimed, blockers resolved
3. Per-state concurrency limits checked against `max_concurrent_agents_by_state`
4. `_dispatch()` creates a `RunAttempt`, adds to `self.running`, spawns `_run_worker` task

**Reconciliation:** on each tick, fetches current states for all running AND gated issue IDs (`self.running | self._pending_gates`). If an issue moved to terminal state → `_kill_worker()` (kills PID + container + task) + `_cleanup_issue_state()` + remove workspace. If moved to review → `_kill_worker()`. If moved out of active states → `_kill_worker()` + `_cleanup_issue_state()`. If a gated issue is not found in Linear (deleted/archived) → `_cleanup_issue_state()`.

**Cancellation infrastructure:**
- `_kill_pid(pid)` — static method, sends SIGKILL to process group with individual kill fallback
- `_kill_worker(issue_id, reason)` — kills subprocess PID → Docker container → async task (order matters: CancelledError does not propagate to child processes)
- `_cleanup_issue_state(issue_id)` — removes all 16 per-issue tracking dict/set entries (added `_issue_repo`, `_rejected_issues`, `_migrated_issues` with multi-repo support, and `_issue_project` with multi-project support). Idempotent. Also used by `_transition()` terminal branch.
- `_force_cancelled` set — populated by `_reconcile()` before calling `_kill_worker()`. Checked at the top of `_on_worker_exit()` to prevent double-processing (token aggregation is not idempotent, and `_safe_transition()` could re-populate tracking dicts after cleanup).
- `_fire_and_forget(coro)` — schedules a coroutine without awaiting it, with `_background_tasks` set to prevent GC.

**Agent self-cancellation:** agents can emit `<!-- transition:cancel -->` to cleanly exit without entering a retry loop.

**Retry logic:**
- `succeeded` → schedule continuation retry in 1s (checks if more work needed)
- `failed/timed_out/stalled` → exponential backoff: `min(10000 * 2^(attempt-1), max_retry_backoff_ms)`
- `canceled` → release claim immediately

**Shutdown:** `stop()` sets `_stop_event`, kills all child PIDs via `_kill_pid()`, calls `cleanup_orphaned_containers()`, cancels async tasks. Uses bulk operations (not per-issue `_kill_worker()`) for speed.

### runner.py
`run_agent_turn()` builds CLI args, launches subprocess, streams NDJSON output. Sets `attempt.pid` after subprocess creation for targeted kill.

**Scope restriction guardrail:** `build_claude_args()` accepts `issue_identifier` and interpolates a prohibition into the headless system prompt on first turns. The text uses a read/write split: agents MAY read other issues for context but MUST NOT write to them. The `SCOPE_RESTRICTION_SYSTEM` constant uses `str.format()` (not Jinja2 — `_SilentUndefined` would silently drop the identifier, removing the guardrail).

**Agent run log capture:** When `log_path` is provided, raw stdout bytes are written to a file during `read_stream()`. The file handle is opened before the `asyncio.wait()` block and closed in a `finally` — this ensures cleanup on all exit paths including `CancelledError`. Write failures are silently swallowed (best-effort). Both `run_agent_turn()` (NDJSON) and `run_codex_turn()` (plain text) support log capture via the same `log_path` parameter.

**PID tracking:** `on_pid` callback registers/unregisters child PIDs with the orchestrator for clean shutdown.

**Stall detection:** background `stall_monitor()` task checks time since last output. Kills process if `stall_timeout_ms` exceeded.

**Turn timeout:** `asyncio.wait()` with `turn_timeout_ms` as overall deadline.

**Event processing** (`_process_event`):
- `result` event → extracts `session_id`, token usage, result text
- `assistant` event → extracts last message for display
- `tool_use` event → updates last message with tool name

### workspace.py
`ensure_workspace()` creates the directory if needed, runs `after_create` hook on first creation.
`remove_workspace()` runs `before_remove` hook, then deletes the directory.
`run_hook()` executes shell scripts via `asyncio.create_subprocess_shell` with timeout.

Workspace key is the composite `{sanitize(issue_identifier)}-{sanitize(repo_name)}` built by `compose_workspace_key`. Legacy 1:1 configs pass `repo_name="_default"` (the synthetic fallback) and get paths like `{root}/SMI-14-_default`. Both components are sanitized to `[A-Za-z0-9._-]` so the composite is path-safe.

### events.py
Owns the mapping from stream-json onto `RunAttempt`. Split out of `runner.py`
so the parsing is testable without launching a subprocess — the original bug
class survived precisely because nothing could exercise it in isolation.

`process_event()` folds one event in. Tool calls, thinking, text, tool errors,
rate limits and results all append to `attempt.activity`, a bounded deque
(`ACTIVITY_MAXLEN = 250`) that the dashboard renders as a timeline.

Successful tool results are deliberately NOT recorded — a real run makes
hundreds, and they carry no information. Only failures are.

`display_tool_name()` shortens `mcp__playwright__browser_take_screenshot` to
`playwright:browser_take_screenshot` so the argument stays visible.

### artifacts.py
Agent evidence (screenshots, exports) lives in `.stokowski/artifacts/` **inside**
the workspace, not beside it. Playwright MCP and the simulator MCP refuse to
write outside their working directory, so an external path silently produces
nothing.

Because it lives inside the clone it must be ignored, and the ignore goes in
`.git/info/exclude` — never the project's `.gitignore`, which belongs to the
project and would show up in every diff. `tests/test_artifacts.py` asserts
against real `git status` output.

`collect()` sorts by mtime so before/after pairs read in capture order, filters
to known evidence types, and skips empty or oversized files.

### report.py
Stokowski renders the Linear comment; the agent supplies structured JSON at
`.stokowski/report.json`. Authorship sits here because a model asked to
summarise its own work reliably produces something readable and unreliably
produces something checkable.

The rendering is deliberately unflattering. A claim with no `evidence` or
`source` is printed with a warning marker rather than dropped; an unverified
`data_source` is marked as such; a missing report posts "no structured report"
rather than silently falling back to prose. The point is that thin work should
look thin on the issue.

`classification` maps to a Linear label (`stokowski/bug-fix`,
`stokowski/improvement`, `stokowski/prototype`, …), created on the team if
absent. This is how the board gets filterable by what the work turned out to be.

The prompt side of this contract is `prompt.build_reporting_contract()`, which
is injected into every agent prompt.

### ledger.py
Append-only JSONL at `<workflow-dir>/.stokowski/ledger.jsonl`, recording each
stage, each gate decision, and each terminal outcome.

Stokowski keeps no database, which is fine for scheduling and useless for the
question that matters once you are running dozens of tickets a week: is this
working, and for what? A Linear issue holds one run's report; it cannot tell
you that `bug-fix` work lands 95% of the time while `improvement` lands 50%,
or whether the agent's own `high` confidence predicts anything.

The human verdict is taken from gate decisions already in the workflow —
approved means accepted, rework means sent back. No separate rating step: the
judgement was always being made, it just was not written down.

Attribution uses the FIRST stage that declared a classification (the
investigation that framed the work). A later stage's self-assessment is not
independent of the work it just did.

`stokowski --stats` prints the summary. Rates below 10 decisions are shown with
their sample size rather than as a bare percentage, because a 1-for-1 reading
as 100% is how a ledger starts lying to you.

### studio.py
Backs `/studio` on the dashboard: the pipeline at a glance, plus editing for
the obvious knobs. Modelled on the content-pipeline studio — the config file
stays the source of truth and this is a view onto it that can write back.

Two properties do the work:

**Comments survive.** The comments in a workflow file are its documentation.
PyYAML keeps 7 of 193 on the shipped example and collapses 310 lines to 131, so
round-tripping goes through `ruamel.yaml` with `indent(mapping=2, sequence=4,
offset=2)` — which reproduces the hand-written file byte for byte. A single
field edit changes exactly one line.

**An invalid config is never written.** Every edit is rendered, written to a
temp file *inside the workflow directory* (so relative prompt paths resolve as
they will at runtime), parsed and validated. Only then is it committed, via
`os.replace`. The orchestrator re-parses config on every poll tick, so a bad or
torn write is a live failure.

Edits are confined to a whitelist (`ROOT_FIELDS`, `STATE_FIELDS`). Structural
changes — adding states, rewiring transitions — stay in the file where a diff
shows what happened. `tracker.api_key` is deliberately absent.

Note the route ordering constraint in `web.py`: `/api/v1/{issue_identifier}`
matches any single segment, so every literal `/api/v1/...` route must be
declared before it.

### web.py
Optional FastAPI app returned by `create_app(orch)`. Routes:
- `GET /` — HTML dashboard (IBM Plex Mono font, dark theme, amber accents)
- `GET /api/v1/state` — full JSON snapshot from `orch.get_state_snapshot()`
- `GET /api/v1/{issue_identifier}` — single issue state
- `POST /api/v1/refresh` — triggers `orch._tick()` immediately

Dashboard JS polls `/api/v1/state` every 3s and updates the DOM without page reload. Agent cards are clickable — expanding an accordion with token breakdown (input/output), elapsed time, session ID, container name (Docker mode), and full untruncated last message. The `expandedCards` Set preserves accordion state across refreshes. Each card also shows a workflow progress indicator (`implement › eval-code › gate-merge › done`) with the current state highlighted. The API snapshot includes a `workflows` dict (workflow name → path + terminal_state) and `docker_enabled` flag so the frontend can render these without extra requests.

Uvicorn is started as an `asyncio.create_task` with `install_signal_handlers` monkey-patched to a no-op to prevent it hijacking SIGINT/SIGTERM. On shutdown, `server.should_exit = True` is set and the task is awaited with a 2s timeout.

### main.py
CLI entry point (`cli()`) and keyboard handler.

**`KeyboardHandler`** runs in a daemon background thread using `tty.setcbreak()` (not `setraw` — `setraw` disables `OPOST` output processing which causes diagonal log output). Uses `select.select()` with 100ms timeout for non-blocking key reads. Restores terminal state in `finally`.

**`_make_footer()`** builds the Rich `Text` status line shown at bottom of terminal via `Live`.

**`check_for_updates()`** hits the GitHub releases API (`/repos/Sugar-Coffee/stokowski/releases/latest`) via httpx, compares the latest tag against the installed `__version__`, and sets `_update_message` if a newer version exists. Best-effort — all exceptions are silently swallowed.

**`_force_kill_children()`** uses `pgrep -f "claude.*-p.*--output-format.*stream-json"` as a last-resort cleanup on `KeyboardInterrupt`.

**`_load_dotenv(directory)`** reads `.env` from a directory — supports `KEY=value` format, ignores comments and blank lines. The project-local `.env` takes precedence over the shell environment (uses direct assignment, overrides existing env vars). Called twice: first from cwd BEFORE `resolve_workflow_paths` so `STOKOWSKI_WORKFLOW_PATH` from `.env` is visible to path resolution, then once per unique workflow-file directory AFTER resolution so a `.env` next to `workflow.yaml` is picked up regardless of cwd. Later values win via direct assignment.

**`STOKOWSKI_WORKFLOW_PATH`** env var in `resolve_workflow_paths` (stokowski/main.py): when no CLI args are passed AND the env var is a non-empty string, the env value is routed through the single-arg branch (file / directory / glob all work). Precedence: CLI args > env var > auto-detect. Empty or whitespace-only values fall through to auto-detect.

### prompt.py
Three-layer prompt assembly for state machine workflows. Main entry point is `assemble_prompt()`.

**`load_prompt_file(path, workflow_dir)`** resolves a prompt file path (absolute or relative to workflow dir) and returns its contents.

**`render_template(template_str, context)`** renders a Jinja2 template with `_SilentUndefined` — missing variables render as empty strings instead of raising errors.

**`build_template_context(issue, state_name, run, attempt, last_run_at)`** builds the flat dict used for Jinja2 rendering. Includes: `issue_id`, `issue_identifier`, `issue_title`, `issue_description`, `issue_url`, `issue_priority`, `issue_state`, `issue_branch`, `issue_labels`, `state_name`, `run`, `attempt`, `last_run_at`.

**`build_lifecycle_section()`** generates the auto-injected lifecycle section appended to every prompt. Includes issue metadata, **scope restriction guardrail** (read/write split — agents may read other issues but must not write to them), rework context with review comments, recent activity, available transitions, and completion instructions. Clearly demarcated with HTML comments.

**`assemble_prompt()`** orchestrates the three layers: loads and renders every global prompt named by the workflow (in order), loads and renders stage prompt, generates lifecycle section, joins with double newlines.

### tracking.py
State machine tracking via Linear attachments and conditional comments:
- `build_attachment_metadata(state, run, extras)` — builds the metadata dict stored on the Linear Attachment
- `parse_attachment_state(metadata)` — converts attachment metadata back to the tracking format used by the orchestrator for crash recovery
- `make_state_comment(state, run)` — returns `None` (state transitions are silent; the attachment is the SoT)
- `make_gate_comment(state, status, prompt, rework_to, run)` — returns human-readable text for `rework` and `escalated` statuses only; returns `None` for `waiting` and `approved`
- `make_evaluation_comment(state, tier, summary, findings, run, workflow)` — returns human-readable text with findings for `review-required` tier only; returns `None` for `approve` tier
- `parse_latest_tracking(comments)` — legacy fallback: scans comments (oldest-first) to find latest state or gate tracking entry for crash recovery of pre-attachment issues
- `get_last_tracking_timestamp(comments)` — finds the timestamp of the latest tracking comment
- `get_comments_since(comments, since_timestamp)` — filters to non-tracking comments after a given timestamp (used to gather review feedback for rework runs)
- `parse_evaluation_tier(result_text)` — extracts tier, summary, findings from agent result; uses last-match semantics; fallback always returns review-required

---

## Data flow: issue dispatch to PR

```
workflow.yaml parsed → states + config loaded
    → Linear poll → Issue fetched → state resolved from attachment (fallback: tracking comments)
    → _dispatch() called
        → RunAttempt created in self.running
        → _run_worker() task spawned
            → ensure_workspace() → after_create hook (git clone, npm install, etc.)
            → assemble_prompt() → 3 layers: global + stage + lifecycle
            → run_agent_turn() called in loop (up to max_turns)
                → build_claude_args() → claude -p subprocess
                → NDJSON streamed: tool_use events, assistant messages, result
                → session_id captured for next turn
            → _on_worker_exit() called
                → state transition on success → attachment upserted + conditional comment
                → tokens/timing aggregated
                → retry or continuation scheduled
```

The agent itself handles: moving Linear state, posting comments, creating branches, opening PRs via `gh pr create`, linking PR to issue. Stokowski doesn't do any of that — it's the scheduler, not the agent.

---

## Stream-json event format

Claude Code emits NDJSON on stdout under `--output-format stream-json --verbose`.
Parsing lives in `events.py`; `tests/fixtures/real_turn.ndjson` is an unedited
capture, and `tests/test_events.py` asserts against it. **Verify any change here
against a real capture rather than against this document.**

```json
{"type":"system","subtype":"init","session_id":"uuid","model":"claude-sonnet-4-6","tools":[…]}
{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"…"}]}}
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{…}}]}}
{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","is_error":false,…}]}}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","rateLimitType":"five_hour","resetsAt":1788181200}}
{"type":"result","subtype":"success","session_id":"uuid","usage":{…},"modelUsage":{…},"total_cost_usd":0.079,"num_turns":2,"stop_reason":"end_turn","permission_denials":[]}
```

Three things about this shape bite repeatedly:

- **There is no top-level `tool_use` event.** Tool calls are content blocks
  inside `assistant` messages; their outcomes are `tool_result` blocks inside
  `user` messages. Parsing for a top-level event yields a dashboard that shows
  nothing but "running" — which is exactly what shipped before v0.6.
- **`usage` has no `total_tokens` field**, and `cache_creation_input_tokens` /
  `cache_read_input_tokens` normally dwarf `input_tokens` by two orders of
  magnitude. A measured trivial turn: 4 input, 150 output, 10,479 cache-write,
  45,255 cache-read. Summing only input+output reports 154 of 55,888.
- **`usage` is per invocation, not per session.** A worker running many turns
  via `--resume` must accumulate; assigning reports only the final turn.

`total_cost_usd` is authoritative — prefer it over computing cost from tokens.

Exit code 0 = success, non-zero = failure (stderr captured). Note that
`is_error: true` on a `result` event (e.g. `error_max_turns`) still exits 0, so
the exit code alone is not sufficient.

---

## Running it

`package.json` carries script aliases for people who reach for `pnpm` first;
they wrap `scripts/stokowski.sh`, which finds the CLI (activated venv → local
`.venv` → PATH) and the workflow file, so nothing depends on remembering where
the virtualenv lives. Run them from the directory holding your `workflow.yaml`
— usually the operator directory, not this repo.

```bash
pnpm start      # run the orchestrator; opens the dashboard
pnpm studio     # same process, opens /studio instead
pnpm check      # --dry-run
pnpm stats      # ledger summary
pnpm test       # pytest
```

The dashboard and the studio are one process — `/studio` is a route on it, not
a second server. `pnpm studio` differs from `pnpm start` only in which page it
opens, reading `server.port` out of the config to build the URL.

The local `.venv` is checked before PATH deliberately: a stale global install
silently running old code against new config is a genuinely confusing failure,
and it happened during development.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[web]"

# Validate config without dispatching agents
stokowski --dry-run

# Run with verbose logging
stokowski -v

# Run with web dashboard
stokowski --port 4200
```

```bash
pip install pytest && python -m pytest tests/ -q
```

`tests/` covers the parts that fail silently: stream-json parsing (against a
real captured fixture), artifact git-isolation (against real `git status`), and
report rendering. Everything else — dispatch, gates, reconciliation — is still
best verified by running against a real Linear project with a test ticket.

---

## Contributing

### Adding a new tracker (not Linear)
1. Add a client in a new file (e.g., `github_issues.py`) implementing the same three methods as `LinearClient`
2. Add the new tracker kind to `config.py` parsing
3. Update `orchestrator.py` to instantiate the right client based on `cfg.tracker.kind`
4. Update `validate_config()` to handle the new kind

### Running against a project
The workflow path is a positional argument (`stokowski /path/to/workflow.yaml`),
so an operator directory needs only `workflow.yaml`, `prompts/` and `.env` —
**not** a checkout of this repo. Keeping a full clone as the operator directory
pins that deployment to whatever version it was cloned at, and `python -m
stokowski` run from inside it will import the local `stokowski/` package in
preference to anything installed, silently running old code against new config.

### Adding config fields
1. Add the field to the relevant dataclass in `config.py`
2. Parse it in `parse_workflow_file()`
3. Use it wherever needed
4. Update `WORKFLOW.example.md` and the README config reference

### Changing the web dashboard
`web.py` is self-contained. The HTML/CSS/JS is inline in the `HTML` constant. The dashboard is intentionally dependency-free on the frontend — no build step, no npm.

### Common pitfalls
- **Never trust a documented event shape over a captured one.** The parser bug
  fixed in v0.6 existed because this file described a `{"type":"tool_use"}`
  event that the CLI has never emitted. Capture a real stream and read it.
- **Cache tokens are the token count.** Any usage arithmetic that ignores
  `cache_read_input_tokens` is wrong by roughly two orders of magnitude.
- **Agent evidence must go inside the workspace.** Tools that produce it will
  not write outside their cwd. Isolate with `.git/info/exclude`, not
  `.gitignore`.
- **Headless bans interactivity, not tooling.** Slash commands, skills and
  subagents all work under `claude -p` and are usually the best work available.
  Only plan mode, brainstorming and confirmation prompts must be excluded.
- **`/api/v1/{issue_identifier}` is a catch-all.** Any literal route under
  `/api/v1` must be declared before it or it will 404 as an unknown issue.
- **Never round-trip a workflow file through PyYAML.** It destroys the comments
  that document it. Use `studio._yaml()`.
- **`max_turns` does nothing in state machine mode.** Each dispatch is exactly
  one `claude -p` invocation — the state machine controls continuation — and
  the CLI has no `--max-turns` flag, so the value reaches neither the loop nor
  the agent. It applies to legacy multi-turn workflows only.
- **A run is bounded by time, not money.** `turn_timeout_ms` and
  `stall_timeout_ms` are the guards. `--max-budget-usd` was tried and removed:
  it caps *API* spend, and Stokowski runs on a Claude subscription where there
  is no dollar meter for it to read, so it either did nothing or halted a run
  for a reason that did not apply.
- **`--resume` needs a session id captured from `system/init`.** Reading it
  only from `result` loses the session on any turn that stalls or times out.
- **`tty.setraw` vs `tty.setcbreak`**: Don't switch back to `setraw`. It disables `OPOST` output processing and causes Rich log lines to render diagonally (no carriage return on newlines).
- **`Issue(title=...)` is required**: Minimal Issue constructors (in `linear.py` `fetch_issues_by_states` and the `orchestrator.py` state-check default) must pass `title=""` — it's a required positional field.
- **`--verbose` with stream-json**: Claude Code requires `--verbose` when using `--output-format stream-json`. Without it you get an error.
- **Linear project slug**: The `project_slug` is the hex `slugId` from the project URL, not the human-readable name. These look like `abc123def456`.
- **Uvicorn signal handlers**: Must be monkey-patched (`server.install_signal_handlers = lambda: None`) before calling `serve()`, otherwise uvicorn hijacks SIGINT.
- **workflow.yaml is pure YAML**: No markdown front matter. The legacy `.md` format with `---` delimiters is still supported but `.yaml` is the canonical format.
- **Prompt files use Jinja2 with silent undefined**: Missing variables become empty strings rather than raising errors. This is intentional — not all variables are available in every context.
- **Docker mode: Claude Code auth**: Containers cannot use interactive OAuth login. Use either `ANTHROPIC_API_KEY` (API plan) or `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max users). Generate an OAuth token via `claude setup-token` in your terminal, then add it to `.env` and list it in `docker.extra_env`. Without one of these, agents fail with "Not logged in".
- **Docker mode: agent runs as non-root**: The `Dockerfile.agent` creates a non-root `agent` user. Claude Code refuses `--dangerously-skip-permissions` as root. Volume mounts target `/home/agent/` not `/root/`.
- **Docker mode: `~/.claude.json` must also be mounted**: Claude Code stores its main config at `~/.claude.json` (a file in the home directory), separate from the `~/.claude/` directory. Both must be mounted for auth to work with `inherit_claude_config: true`.
- **Docker mode: uvicorn binds `0.0.0.0` in containers**: On macOS Docker Desktop, `127.0.0.1` inside a container isn't reachable from the host. The web dashboard auto-detects non-TTY mode and binds to `0.0.0.0`.
- **Docker mode: plugin config files are rewritten for containers**: Claude Code discovers plugins primarily through `known_marketplaces.json` (`installLocation` fields), with `installed_plugins.json` as secondary metadata. Both files store absolute host paths and are typically mode `0600`. `_prepare_plugin_file()` in `docker_runner.py` reads each file, rewrites paths to container equivalents, writes a `0644` copy to a host-visible location, and bind-mounts it read-only over the original in the agent container. **The operator's `~/.claude` directory is never written to for this purpose** — the rewrite always stages to a separate location.
- **Docker compose: workflow path is split across two env vars**: `docker-compose.yml` bind-mounts `${STOKOWSKI_WORKFLOW_HOST_PATH:-./workflow.yaml}` at `/app/workflow` and hard-sets `STOKOWSKI_WORKFLOW_PATH=/app/workflow` in the container env. Operators never change the container-side path — they switch single-vs-multi project by setting `STOKOWSKI_WORKFLOW_HOST_PATH` in `.env` (to a file for single-project, to a directory for multi-project). The container target `/app/workflow` adapts to whichever shape the host path is because Docker bind-mounts don't care about file-vs-dir at the target. The compose `command:` no longer encodes the workflow path — it passes only `--port 4200`.
- **Docker mode: DooD/DinD requires explicit shim config**: When the orchestrator itself runs in a container, it cannot see the host's `.claude` directory and has no host-visible `/tmp` to stage rewritten plugin files. With `inherit_claude_config: true` **and at least one agent state with `runner: "claude"`**, operators must provide three `docker` config fields: `host_claude_dir_mount` (the orchestrator's view of the host `.claude` dir — bind-mount the host path here read-only), `plugin_shim_host_path` (a host-resolvable directory for staging rewrites), and `plugin_shim_container_path` (the orchestrator's view of that same shim — bind-mount the host shim path here read-write). Stokowski refuses to start without these when DooD is detected (`/.dockerenv` present). Codex-only workflows are exempt — Codex does not consume Claude Code plugin config. The plugin-prep path is gated by `needs_plugin_config=True` in `build_docker_run_args`, which only `run_agent_turn` (Claude Code) sets; Codex dispatches and shell hooks bypass the shim entirely. There is no fallback — prior versions wrote through the bind-mount to the host plugin files, which silently polluted them.
- **Agent scope guardrails**: Agents receive a scope restriction in both the system prompt (first turn) and lifecycle section (every turn) prohibiting writes to other Linear issues. This is a probabilistic guardrail, not hard enforcement. For hard enforcement, operators can use `permission_mode: allowedTools` to exclude Linear MCP tools — but this also blocks agents from managing their own ticket (posting comments, moving state).
- **`STOKOWSKI_ISSUE_IDENTIFIER` env var**: Set per-dispatch in `_run_worker()`, not in `ServiceConfig`. Informational only — useful for hooks that need to know which issue they service. Not a security boundary.
- **`_cleanup_issue_state()` must stay in sync with `__init__`**: Any new per-issue tracking dict added to `Orchestrator.__init__` must also be added to `_cleanup_issue_state()`. Failure to do so causes memory leaks and stale state on cancellation.
- **Multi-project: every per-issue `self.cfg` read must route via `_cfg_for_issue(issue_id)`**: Reading `self.cfg` directly in a post-dispatch path silently falls back to the primary project's cfg — project B's ticket would resolve against project A's workflows, repos, or state names. The transitional `self.cfg` property exists only as a back-compat shim during the Unit 5 sweep; new code should use `self._cfg_for_issue_or_primary(issue.id)` (falls back to primary when `_issue_project` has no binding yet) or `self._primary_cfg()` for shared-globals reads.
- **Multi-project: `_issue_project` must be stamped before any `_cfg_for_issue` call for gate issues**: Gate states (review / gate_approved / rework) are not in `active_linear_states()` so they're never returned by `_tick`'s candidate fetch. `_handle_gate_responses` is the sole binding site for gate issues after orchestrator restart — it iterates projects, stamps `_issue_project` before fetching comments, then runs the existing per-issue logic.
- **Agent run logs**: When `logging.enabled` is true, raw agent stdout is captured to `{log_dir}/{issue_identifier}/` as `.ndjson` (Claude Code) or `.log` (Codex) files. To debug an agent run: `cat {log_dir}/SMI-14/20260324T041500Z-turn-1.ndjson | jq .` Logs survive workspace cleanup — their lifetime is controlled by `max_age_days` and `max_total_size_mb`. Retention cleanup runs at startup and after each worker exit. Log writes are best-effort — failures do not affect agent execution.
- **Multi-repo: hook Jinja rendering is gated on `cfg.repos_synthesized`**: When `repos:` section is absent (synthesized=True), root hooks bypass Jinja entirely and pass through to the shell verbatim. This is deliberate — legacy configs may contain literal `{`/`}` shell syntax (credential helpers with `!f() { ...; }; f`). When the operator adds an explicit `repos:` section, Jinja rendering activates with `StrictUndefined` over the `repo` namespace. Typos (e.g. `{{ repo.clne_url }}`) raise `UndefinedError` and post a user-facing Linear comment with retry-cap = 1. Do NOT switch rendering to `_SilentUndefined` for hooks — `git clone $EMPTY` from a silent substitution is catastrophic.
- **Multi-repo: `_default` repo name is reserved**: Operator-authored `repos:` entries must not use the name `_default`. R21 validation rejects configs that try. The synthesis branch uses `_default` with `label=None`/`clone_url=""` as its sentinel shape, and `_repos_synthesized` distinguishes synthesis from operator authorship.
- **Multi-repo: R10 rejection is posted by an async pre-pass, not `_is_eligible`**: `_is_eligible` stays synchronous and only reads `_rejected_issues`. The async work (comment fetch + dedup + post) happens in `_process_rejections` before the sync eligibility loop runs in `_tick`. Any future eligibility rule requiring Linear I/O should follow the same pre-pass pattern rather than making `_is_eligible` async.
- **Multi-repo: tracking comments `repo` field uses defensive read**: `parse_latest_tracking` applies `setdefault("repo", None)`. Readers MUST use `tracking.get("repo") or fallback` — never `tracking["repo"]`. Pre-multi-repo comments don't carry the field; cold-start recovery relies on the fallback and posts a one-time migration notice.
- **Evaluator `session` should be `fresh`**: Using `inherit` lets the evaluator see the prior agent's conversation, which defeats the adversarial review purpose. `validate_config()` warns but does not error. Evaluators default to `session: fresh` when not explicitly set.
- **Evaluator must be followed by a gate**: An evaluator not followed by a gate in the workflow path is a validation error — the approve transition has no gate to skip.
- **Evaluation output parsing is best-effort**: If the evaluator doesn't produce a `<!-- stokowski:evaluation {...} -->` marker in its result text, the tier defaults to `review-required` (fail-safe). Check agent logs if evaluations always fall to human review.
- **`result_text` vs `last_message`**: `RunAttempt.last_message` is truncated to 200 chars. Evaluator tier parsing uses `result_text` (full text from the result event, capped at 10K). If you add new fields to RunAttempt that are populated in `_process_event`, they don't need cleanup in `_cleanup_issue_state` — RunAttempt is per-dispatch, not per-issue.
- **Evaluators consume agent concurrency slots**: Evaluators are real agent subprocesses. Operators with high gate throughput should set per-state limits (e.g., `eval-merge: 1`) to avoid starving implementation agents.
- **Evaluator findings are visible during rework**: When an evaluation results in `review-required`, a human-readable comment with findings is posted. If the gate then sends work back, the rework agent sees both the evaluator findings and any gate reviewer comments via `get_comments_since()`.
- **`_transition()` handles evaluators in the `else` branch**: Evaluator states are intentionally handled by the catch-all agent branch in `_transition()`. The comment says "Agent or evaluator state." Do not add a separate `elif` for evaluators unless the behavior needs to diverge.
- **Attachment is the SoT for crash recovery**: State machine position is stored in a Linear Attachment (`stokowski://state/{identifier}`) with metadata JSON. `_upsert_state()` centralizes attachment upsert + conditional comment posting. Comments no longer contain `<!-- stokowski:...-->` JSON payloads — they are human-readable only.
- **Terminal state deletes the attachment**: When an issue reaches a terminal state, the attachment is removed to keep Linear clean. Workspace cleanup happens separately.
- **`parse_latest_tracking()` still needed for migration**: Pre-attachment issues store state in HTML comment payloads. The legacy comment scanner is the fallback when no attachment is found. It can be removed once all active issues have been migrated.
