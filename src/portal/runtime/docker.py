"""Thin subprocess wrapper around the docker CLI.

Not covered by automated tests in this phase: exercising these paths needs a
live docker daemon. The boundary is deliberately narrow (one `run` function) so
a later phase can mock it in one place.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..errors import DockerError


def _require_docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        raise DockerError("`docker` was not found on PATH.")
    return exe


def ensure_daemon_running() -> None:
    """Fail fast with a clear message if the docker daemon isn't reachable.

    `docker` being on PATH doesn't mean the daemon is up (Docker Desktop
    stopped, dockerd not started, etc.) — `docker info` is the cheapest call
    that actually talks to the daemon rather than just the CLI.
    """
    exe = _require_docker()
    try:
        proc = subprocess.run(
            [exe, "info"], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise DockerError(f"Could not run `docker info`: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DockerError(
            "Docker daemon is not reachable (is Docker Desktop / dockerd running?).\n"
            f"{detail}"
        )


def run(args: list[str], *, capture: bool = True, check: bool = True) -> str:
    """Run a docker command. Returns stdout when capturing."""
    cmd = [_require_docker(), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DockerError(f"Could not run `docker {' '.join(args)}`: {exc}") from None

    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DockerError(
            f"`docker {' '.join(args)}` failed (exit {proc.returncode}).\n{detail}"
        )
    return (proc.stdout or "") if capture else ""


def ensure_network(name: str) -> None:
    """Create the bridge network if it does not exist."""
    existing = run(["network", "ls", "--format", "{{.Name}}"]).split()
    if name not in existing:
        run(["network", "create", name])


def remove_network(name: str) -> None:
    """Best effort: the network may already be gone, or still hold containers."""
    try:
        run(["network", "rm", name])
    except DockerError:
        pass


def compose_up(
    *, project: str, files: list[Path], build: bool = True, env_file: Path | None = None
) -> None:
    args = ["compose", "-p", project]
    for f in files:
        args += ["-f", str(f)]
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    args += ["up", "-d"]
    if build:
        args.append("--build")
    run(args, capture=False)


def compose_down(
    *, project: str, files: list[Path], volumes: bool = False
) -> None:
    args = ["compose", "-p", project]
    for f in files:
        args += ["-f", str(f)]
    args.append("down")
    if volumes:
        args.append("--volumes")
    run(args, capture=False, check=False)


def run_cloudflared(*, container: str, network: str, token: str) -> None:
    """Start this project's own cloudflared sidecar.

    The token is passed via `--env TUNNEL_TOKEN=...` in the argv of a subprocess
    we control; it is never echoed, and DockerError messages from here are built
    from stderr only after the caller redacts.
    """
    remove_container(container)
    run(
        [
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "--restart",
            "unless-stopped",
            "--label",
            "portal.managed=true",
            "--env",
            f"TUNNEL_TOKEN={token}",
            "cloudflare/cloudflared:latest",
            "tunnel",
            "--no-autoupdate",
            "run",
        ]
    )


def remove_container(name: str) -> None:
    run(["rm", "-f", name], check=False)


def container_status(name: str) -> str:
    """Human-readable state of one container, or 'not found'."""
    out = run(
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
        check=False,
    ).strip()
    return out.splitlines()[0] if out else "not found"


def connect_network(container: str, network: str) -> None:
    run(["network", "connect", network, container], check=False)
