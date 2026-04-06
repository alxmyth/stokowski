"""Docker container lifecycle — builds docker run commands and manages containers/volumes."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import tempfile
from pathlib import Path

from .config import DockerConfig
from .workspace import sanitize_key

logger = logging.getLogger("stokowski.docker_runner")

_DOCKER_CLI_TIMEOUT = 30  # seconds
_DOCKER_PULL_TIMEOUT = 300  # 5 minutes


def resolve_host_path(path: str) -> str:
    """Expand ~ and $VAR in host paths for Docker -v flags.

    Does NOT call Path.resolve() — in DooD mode the orchestrator runs
    inside a container where host paths don't exist on the local
    filesystem.  The Docker daemon resolves the path on the host.
    """
    expanded = os.path.expandvars(os.path.expanduser(path))
    # Warn if variable expansion left unexpanded references
    if "$" in expanded or "${" in expanded:
        logger.warning(
            "host path %r still contains unexpanded variables after expansion: %r "
            "(is the env var set?)",
            path,
            expanded,
        )
    return expanded


_plugin_file_cache: dict[tuple[str, str, str], tuple[str, float]] = {}
"""Cache of (host_dir, container_home, relative_path) → (temp_file_path, mtime).
Avoids creating a new temp file per container launch. Invalidated when source mtime changes."""

# Plugin config files that need host→container path rewriting.
# Claude Code discovers plugins primarily through known_marketplaces.json
# (installLocation fields), with installed_plugins.json as secondary metadata.
_PLUGIN_FILES_TO_REWRITE = (
    os.path.join("plugins", "installed_plugins.json"),
    os.path.join("plugins", "known_marketplaces.json"),
)


def _cleanup_plugin_cache() -> None:
    """Remove cached temp files on process exit."""
    for tmp_path, _ in list(_plugin_file_cache.values()):
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup_plugin_cache)


def _prepare_plugin_file(
    host_claude_dir: str, container_home: str, relative_path: str
) -> str | None:
    """Create a temp copy of a plugin config file with paths rewritten for the container.

    Reads the host file at ``{host_claude_dir}/{relative_path}``, replaces all
    occurrences of ``host_claude_dir`` with the container equivalent, writes a
    ``0644`` temp file, and returns its path for bind-mounting into the container.

    Returns ``None`` if the source file doesn't exist or isn't readable (e.g. in
    DooD mode where host paths aren't accessible from the orchestrator container).
    Results are cached per ``(host_claude_dir, container_home, relative_path)``
    and invalidated when the source file's mtime changes.
    """
    host_file = Path(host_claude_dir) / relative_path
    if not host_file.is_file():
        return None

    cache_key = (host_claude_dir, container_home, relative_path)
    try:
        current_mtime = host_file.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    # Return cached temp file if source hasn't changed
    cached = _plugin_file_cache.get(cache_key)
    if cached:
        cached_path, cached_mtime = cached
        if current_mtime == cached_mtime and Path(cached_path).is_file():
            return cached_path

    try:
        content = host_file.read_text()
    except PermissionError:
        logger.warning("Cannot read %s — skipping path rewrite for container", host_file)
        return None

    # Rewrite host paths to container paths
    container_claude_dir = f"{container_home}/.claude"
    rewritten = content.replace(host_claude_dir, container_claude_dir)

    # Write to a temp file that persists until the source changes or process exits
    tmp = tempfile.NamedTemporaryFile(
        mode="w", prefix="stokowski-plugin-", suffix=".json", delete=False
    )
    tmp.write(rewritten)
    tmp.close()
    Path(tmp.name).chmod(0o644)

    # Clean up previous temp file if any
    if cached:
        old_path = cached[0]
        if old_path != tmp.name:
            try:
                Path(old_path).unlink(missing_ok=True)
            except OSError:
                pass

    _plugin_file_cache[cache_key] = (tmp.name, current_mtime)
    return tmp.name


def workspace_volume_name(docker_cfg: DockerConfig, workspace_key: str) -> str:
    """Return the per-issue Docker volume name."""
    return f"{docker_cfg.volume_prefix}-{workspace_key}".lower()


async def create_workspace_volume(
    docker_cfg: DockerConfig, workspace_key: str
) -> str:
    """Create a per-issue Docker volume if it doesn't exist. Returns volume name."""
    vol = workspace_volume_name(docker_cfg, workspace_key)
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "create", "--label", "stokowski=true", vol,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"Timed out creating volume {vol} after {_DOCKER_CLI_TIMEOUT}s")
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to create volume {vol}: {stderr.decode()[:200]}")
    return vol


async def remove_workspace_volume(
    docker_cfg: DockerConfig, workspace_key: str
) -> bool:
    """Remove a per-issue Docker volume. Best-effort. Returns True if removed."""
    vol = workspace_volume_name(docker_cfg, workspace_key)
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "rm", vol,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=_DOCKER_CLI_TIMEOUT)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Timed out removing volume {vol}, killing docker CLI process")
        proc.kill()
        return False


async def cleanup_orphaned_volumes(
    docker_cfg: DockerConfig, active_keys: set[str]
) -> int:
    """Remove workspace volumes not associated with active issues."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "volume", "ls", "-q", "--filter", "label=stokowski=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Timed out listing Docker volumes, killing docker CLI process")
        proc.kill()
        return 0
    count = 0
    prefix = docker_cfg.volume_prefix.lower()
    for vol_name in stdout.decode().strip().split("\n"):
        vol_name = vol_name.strip()
        if not vol_name or not vol_name.startswith(prefix):
            continue
        # Extract key from volume name
        key = vol_name[len(prefix) + 1:]  # strip "{prefix}-"
        if key not in active_keys:
            await remove_workspace_volume(docker_cfg, key)
            count += 1
    return count


def build_docker_run_args(
    docker_cfg: DockerConfig,
    image: str,
    command: list[str],
    workspace_key: str,
    env: dict[str, str],
    container_name: str | None = None,
) -> list[str]:
    """Build docker run CLI args wrapping an inner command."""
    args = ["docker", "run", "--rm", "-i"]

    if docker_cfg.init:
        args.append("--init")

    # Container identity
    if container_name:
        args.extend(["--name", container_name])
    args.extend(["--label", "stokowski=true"])

    # Host networking
    args.extend(["--network", "host"])

    # Per-issue workspace volume — full isolation, each agent only sees /workspace
    vol = workspace_volume_name(docker_cfg, workspace_key)
    args.extend(["-v", f"{vol}:/workspace", "-w", "/workspace"])

    # Claude config — either inherit from host or use sessions volume
    plugins_prepared = False
    if docker_cfg.inherit_claude_config:
        # Read-write mount: agents can write session data for --resume support.
        # This means agents can also modify host Claude config — accepted tradeoff
        # for inherit mode. Use inherit_claude_config: false for full isolation.
        # Mount into /home/agent (non-root user in Dockerfile.agent)
        home = "/home/agent"
        host_dir = resolve_host_path(docker_cfg.host_claude_dir)
        args.extend(["-v", f"{host_dir}:{home}/.claude"])
        # Claude Code also reads ~/.claude.json for its main config
        host_json = os.path.join(os.path.dirname(host_dir), ".claude.json")
        args.extend(["-v", f"{host_json}:{home}/.claude.json"])
        # Pass host claude dir so entrypoint can rewrite plugin paths
        args.extend(["-e", f"STOKOWSKI_HOST_CLAUDE_DIR={host_dir}"])
        # Prepare rewritten, readable copies of plugin config files.
        # These files are often mode 0600 (owner-only) which the container's
        # non-root agent user cannot read, and they contain host-absolute paths
        # that don't resolve inside the container. _prepare_plugin_file() reads
        # each file host-side, rewrites paths, writes a 0644 temp copy, and we
        # bind-mount it read-only over the original.
        # In DooD mode (orchestrator in a container), host paths aren't
        # accessible — _prepare_plugin_file returns None and we fall back to
        # an in-container fixup below.
        for rel_path in _PLUGIN_FILES_TO_REWRITE:
            tmp = _prepare_plugin_file(host_dir, home, rel_path)
            if tmp:
                target = f"{home}/.claude/{rel_path}"
                args.extend(["-v", f"{tmp}:{target}:ro"])
                plugins_prepared = True
    else:
        args.extend(["-v", f"{docker_cfg.sessions_volume}:/home/agent/.claude"])

    # Operator-declared extra volumes
    for v in docker_cfg.extra_volumes:
        parts = v.split(":", 1)
        if len(parts) >= 2:
            expanded = resolve_host_path(parts[0])
            args.extend(["-v", f"{expanded}:{parts[1]}"])
        else:
            args.extend(["-v", v])

    # Environment variables
    for key, val in env.items():
        args.extend(["-e", f"{key}={val}"])

    # Image
    args.append(image)

    # Plugin path rewriting: prefer host-side (temp files mounted over originals).
    # In DooD mode the orchestrator can't read host files, so we use a tmpfs
    # overlay for the plugins directory. The tmpfs shadows the bind-mounted
    # plugins subdir so writes never reach the host filesystem.
    if docker_cfg.inherit_claude_config and not plugins_prepared:
        # Mount host plugins dir read-only at a secondary path for the script
        # to read from, and a tmpfs at the real plugins path so writes are
        # container-local. The parent ~/.claude bind mount remains writable
        # for session persistence (--resume support).
        # host_dir and home are already set above (both paths require inherit_claude_config)
        plugins_host = f"{host_dir}/plugins"
        args.extend(["-v", f"{plugins_host}:/host-claude-plugins:ro"])
        # uid=1000 matches the 'agent' user created in Dockerfile.agent
        args.extend(["--tmpfs", f"{home}/.claude/plugins:exec,size=50m,uid=1000"])

        escaped_cmd = " ".join(_shell_escape(c) for c in command)
        fixup_script = (
            'if [ -n "$STOKOWSKI_HOST_CLAUDE_DIR" ] && [ -d /host-claude-plugins ]; then '
            'cp -a /host-claude-plugins/. "$HOME/.claude/plugins/" || echo "stokowski: plugin copy failed" >&2; '
            'for f in installed_plugins.json known_marketplaces.json; do '
            '[ -f "$HOME/.claude/plugins/$f" ] && '
            'sed -i "s|$STOKOWSKI_HOST_CLAUDE_DIR|$HOME/.claude|g" "$HOME/.claude/plugins/$f"; '
            "done; "
            'chmod -R a+rX "$HOME/.claude/plugins/marketplaces" 2>/dev/null; '
            "fi; "
            f"exec {escaped_cmd}"
        )
        args.extend(["bash", "-c", fixup_script])
    else:
        args.extend(command)

    return args


_SHELL_SAFE = re.compile(r"^[A-Za-z0-9_./:=@-]+$")


def _shell_escape(s: str) -> str:
    """Escape a string for safe inclusion in a shell command."""
    if not s:
        return "''"
    if _SHELL_SAFE.match(s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def container_name_for(
    issue_identifier: str, turn: int, attempt: int | None
) -> str:
    """Generate deterministic container name."""
    key = sanitize_key(issue_identifier)
    name = f"stokowski-{key}-t{turn}"
    if attempt is not None:
        name += f"-a{attempt}"
    return name.lower()


async def kill_container(name: str) -> None:
    """Kill a running container by name. Best-effort, no error on not-found."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "kill", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning(f"Timed out killing container {name}, killing docker CLI process")
        proc.kill()


async def cleanup_orphaned_containers() -> int:
    """Find and kill orphaned stokowski containers. Returns count killed."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-q", "--filter", "label=stokowski=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("Timed out listing Docker containers, killing docker CLI process")
        proc.kill()
        return 0
    container_ids = stdout.decode().strip().split("\n")
    count = 0
    for cid in container_ids:
        if cid.strip():
            await kill_container(cid.strip())
            count += 1
    return count


async def check_docker_available() -> tuple[bool, str]:
    """Check if Docker daemon is reachable. Returns (ok, message)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_CLI_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(
                f"Timed out checking Docker availability after {_DOCKER_CLI_TIMEOUT}s — daemon may be hung"
            )
        if proc.returncode == 0:
            return True, "Docker daemon reachable"
        return False, f"Docker daemon not reachable: {stderr.decode()[:200]}"
    except FileNotFoundError:
        return False, "Docker CLI not found in PATH"


async def pull_image(image: str) -> bool:
    """Pull a Docker image. Returns True on success."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "pull", image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_PULL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"Timed out pulling image {image} after {_DOCKER_PULL_TIMEOUT}s")
        proc.kill()
        return False
    return proc.returncode == 0
