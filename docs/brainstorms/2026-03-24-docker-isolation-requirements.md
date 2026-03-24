---
date: 2026-03-24
topic: docker-isolation
---

# Docker Isolation for Stokowski Agents

## Problem Frame

Stokowski agents run as local subprocesses with `--dangerously-skip-permissions`, giving them unrestricted access to the host filesystem, processes, and network. For production and multi-tenant deployments, operators need the ability to sandbox agents inside Docker containers so that a misbehaving agent cannot escape its workspace, read host secrets, or interfere with other agents.

Additionally, the orchestrator itself should be deployable as a container so the entire stack can be managed via `docker compose` — no local Python installation required.

## Requirements

### Docker Mode Toggle

- R1. A new `docker` section in `workflow.yaml` enables Docker isolation. When `docker.enabled` is `false` (default), all behavior is identical to today — zero breaking changes.
- R2. When Docker mode is enabled, agent turns execute inside ephemeral Docker containers instead of local subprocesses.

### Image Configuration

- R3. `docker.default_image` specifies the Docker image used for all agent containers. This image must have Claude Code (or Codex) installed.
- R4. Per-state `docker.image` override allows different states to use different images (e.g., a heavier image with build tools for `implement`, a lighter one for `code-review`).
- R5. A default agent Dockerfile (`Dockerfile.agent`) is provided in the repo with Claude Code, git, gh CLI, and common dev tools pre-installed.

### Credential Passing

- R6. `ANTHROPIC_API_KEY`, `LINEAR_API_KEY`, `LINEAR_PROJECT_SLUG`, and `LINEAR_ENDPOINT` are automatically passed to agent containers as environment variables (mirroring today's `agent_env()` behavior).
- R7. `docker.extra_env` allows operators to declare additional env vars to forward (e.g., `GITHUB_TOKEN`, `NPM_TOKEN`). These are read from the orchestrator's environment and passed via `-e` flags.
- R8. `docker.extra_volumes` allows operators to mount host paths into agent containers (e.g., `~/.ssh:/root/.ssh:ro` for git auth, `~/.gitconfig:/root/.gitconfig:ro`).

### Claude Config Inheritance

- R9. `docker.inherit_claude_config` (default `true`) mounts the host's `~/.claude` directory into agent containers. This gives agents automatic access to existing Claude Code authentication, settings, MCP server configs, and session storage — matching local behavior with zero credential setup.
- R10. `docker.host_claude_dir` (default `~/.claude`) specifies the host path to mount. Supports `~` and `$VAR` expansion. When the orchestrator runs in Docker (DooD), this path is interpreted relative to the host filesystem. For docker-compose, operators should use `${HOME}/.claude` or an absolute path.
- R11. When `inherit_claude_config` is `true`, `ANTHROPIC_API_KEY` becomes optional — Claude Code finds its own stored auth. The separate `stokowski-sessions` volume is not needed; sessions write to per-project subdirectories within the mounted `~/.claude/projects/`.
- R12. When `inherit_claude_config` is `false`, a Docker named volume (`stokowski-sessions`) is used at `/root/.claude` for session persistence, and `ANTHROPIC_API_KEY` must be provided via env. This mode is for production/multi-tenant deployments where host config isolation is required.

### Workspace Persistence

- R13. Workspaces persist across turns via a Docker named volume (`stokowski-workspaces`). The orchestrator creates per-issue subdirectories; each agent container mounts its issue's subdirectory as the working directory.

### Container Lifecycle

- R14. Each agent turn spawns a new container (`docker run --rm`). Containers are ephemeral — only the mounted volumes persist.
- R15. Agent containers are labeled (`--label stokowski=true`) for identification and cleanup.
- R16. On graceful shutdown, the orchestrator runs `docker kill` on all active agent containers.
- R17. On startup, the orchestrator checks for orphaned containers from a previous crash (`docker ps --filter label=stokowski`) and kills them.
- R18. Container names follow a deterministic pattern: `stokowski-{issue_identifier}-{turn}` for debuggability.

### Hook Execution

- R19. In Docker mode, workspace hooks (`after_create`, `before_run`, `after_run`, `on_stage_enter`) execute inside a container using the same image and volume mounts as the agent. This ensures hooks have access to the same tools (git, npm, etc.).
- R20. `before_remove` continues to run on the host/orchestrator since it handles volume/directory cleanup.

### Orchestrator Containerization

- R21. A `Dockerfile` for the orchestrator packages Stokowski with the Docker CLI (not the Docker daemon — it uses the host's daemon via socket).
- R22. `docker-compose.yml` defines the full stack: orchestrator service with Docker socket mount, named volumes, env_file reference, and optional web dashboard port.
- R23. When running in a container, the orchestrator detects non-TTY stdin and disables the keyboard handler / TUI, relying on the web dashboard and log output instead.

### Networking

- R24. Agent containers use host networking (`--network host`). Network isolation is not a goal; the security boundary is filesystem and process containment.

### NDJSON Streaming Compatibility

- R25. The existing NDJSON stream parser, stall detector, and turn timeout logic must work unchanged — `docker run` naturally forwards container stdout/stderr to the client process.

## Success Criteria

- Existing non-Docker workflows work identically with no config changes
- An operator can add `docker: { enabled: true, default_image: "..." }` to their `workflow.yaml` and have agents run in containers
- `docker compose up` starts the full stack (orchestrator + web dashboard) with agents spawning as sibling containers
- With `inherit_claude_config: true`, agents authenticate using the host's existing Claude Code config — no API key env vars needed
- With `inherit_claude_config: false`, credentials work via env vars and a sessions volume
- Claude Code `--resume` works across turns (session persistence via mounted `~/.claude` or sessions volume)
- Graceful shutdown kills all agent containers; crash recovery cleans up orphans

## Scope Boundaries

- No custom Docker network configuration or network-level isolation (host networking only for v1)
- No container resource limits (CPU/memory caps) in v1 — can be added later via `docker.resource_limits`
- No remote Docker host support — assumes local Docker daemon via socket
- No Kubernetes / container orchestrator support — Docker Compose only
- No image building — operators provide pre-built images or use the default Dockerfile
- The orchestrator does not need to work as a Docker-in-Docker setup (no nested daemons)

## Key Decisions

- **Docker Socket Mounting (DooD) over Docker-in-Docker:** Agent containers are siblings on the host daemon, not nested. Simpler, more efficient, proven pattern. The orchestrator is trusted code — it's the agents being isolated.
- **Named volumes over bind mounts for inter-container data:** Avoids the DooD path-translation footgun where container-internal paths don't match host paths. Named volumes are daemon-managed and work correctly from any sibling container.
- **`inherit_claude_config` as default for local use:** Mounting the host's `~/.claude` gives agents auth, settings, MCP configs, and session storage for free. Eliminates credential setup friction. Concurrency-safe because Claude Code keys sessions by project path and Stokowski prevents concurrent turns per issue.
- **Two modes (inherit vs isolated):** Local developers use `inherit_claude_config: true` for convenience. Production/multi-tenant uses `false` with explicit env vars and a sessions volume. Clean separation of concerns.
- **Host networking:** Simplest setup. The security goal is filesystem/process isolation, not network restriction. Network policies can be layered on later.
- **Hooks run in containers:** `after_create` (git clone, npm install) must have access to the same tools as the agent. Running hooks via `docker run` with the same image ensures consistency.

## Dependencies / Assumptions

- Docker daemon must be running and accessible (either locally or via mounted socket)
- Docker CLI must be available in the orchestrator's PATH (installed in orchestrator Dockerfile)
- Agent images must have Claude Code installed and configured (the default `Dockerfile.agent` handles this)
- The operator's `.env` file contains all required credentials

## Outstanding Questions

### Deferred to Planning

- [Affects R13][Technical] How should the Docker runner translate workspace paths for the `-v` flag when both orchestrator and agents are in containers? Named volumes solve most of this, but the subdirectory mounting pattern needs validation.
- [Affects R10][Technical] When the orchestrator is in Docker (DooD), `host_claude_dir` must resolve to a host-absolute path. Validate that `${HOME}/.claude` in docker-compose correctly captures the host home at compose-up time.
- [Affects R19][Technical] Should hook timeout tracking account for Docker image pull time on first run? May need a separate `pull_timeout` or pre-pull step.
- [Affects R5][Needs research] What is the most reliable way to install Claude Code in a Docker image — `npm install -g @anthropic-ai/claude-code` or is there an official Docker image?

## Next Steps

-> `/ce:plan` for structured implementation planning
