"""Thin subprocess wrapper around the docker CLI.

Every function takes an optional `docker_host`. When set (e.g.
`ssh://user@1.2.3.4`), it's passed to the docker CLI as DOCKER_HOST — the
docker CLI itself proxies the connection over SSH to a remote dockerd, so
every command here runs against a remote machine's docker with no portal
install needed there: only `docker` (client-side) and `ssh` on PATH locally,
and `docker` + sshd reachable on the remote end.

Not unit-tested: exercising these paths needs a live docker daemon. The
mocked deploy tests stub this module at its function boundary instead.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..errors import DockerError

# Pinned so an upstream release can't silently break every deploy. Bump
# deliberately; PORTAL_CLOUDFLARED_IMAGE overrides it without a code change.
CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.9.3"


def cloudflared_image() -> str:
    return os.environ.get("PORTAL_CLOUDFLARED_IMAGE") or CLOUDFLARED_IMAGE


def _require_docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        raise DockerError("`docker` was not found on PATH.")
    return exe


def _build_env(
    docker_host: str | None, extra: dict[str, str] | None = None
) -> dict[str, str] | None:
    """None means "inherit the parent process's environment unchanged" — the
    subprocess.run default. Only build an explicit copy when there's actually
    something to override, so the common local case doesn't pay for a dict
    copy on every call."""
    if docker_host is None and not extra:
        return None
    env = dict(os.environ)
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    if extra:
        env.update(extra)
    return env


def ensure_daemon_running(*, docker_host: str | None = None) -> None:
    """Fail fast with a clear message if the docker daemon isn't reachable.

    `docker` being on PATH doesn't mean the daemon is up (Docker Desktop
    stopped, dockerd not started, a remote host unreachable over SSH, etc.) —
    `docker info` is the cheapest call that actually talks to the daemon
    rather than just the CLI.
    """
    try:
        run(["info"], env=_build_env(docker_host))
    except DockerError as exc:
        where = f" at {docker_host}" if docker_host else ""
        raise DockerError(
            f"Docker daemon is not reachable{where} (is it running, and if "
            f"remote, is SSH access to it working?).\n{exc}"
        ) from None


def run(
    args: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    """Run a docker command. Returns stdout when capturing."""
    cmd = [_require_docker(), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        raise DockerError(f"Could not run `docker {' '.join(args)}`: {exc}") from None

    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise DockerError(
            f"`docker {' '.join(args)}` failed (exit {proc.returncode}).\n{detail}"
        )
    return (proc.stdout or "") if capture else ""


def ensure_network(name: str, *, docker_host: str | None = None) -> None:
    """Create the bridge network if it does not exist."""
    env = _build_env(docker_host)
    existing = run(["network", "ls", "--format", "{{.Name}}"], env=env).split()
    if name not in existing:
        run(["network", "create", name], env=env)


def remove_network(name: str, *, docker_host: str | None = None) -> None:
    """Best effort: the network may already be gone, or still hold containers."""
    try:
        run(["network", "rm", name], env=_build_env(docker_host))
    except DockerError:
        pass


def _compose_args(project: str, files: list[Path]) -> list[str]:
    """`compose -p PROJECT -f A -f B`: every compose call must name both the
    project and the full file chain, or it acts on a different stack."""
    args = ["compose", "-p", project]
    for f in files:
        args += ["-f", str(f)]
    return args


def compose_up(
    *,
    project: str,
    files: list[Path],
    build: bool = True,
    env_file: Path | None = None,
    docker_host: str | None = None,
) -> None:
    args = _compose_args(project, files)
    if env_file is not None:
        args += ["--env-file", str(env_file)]
    args += ["up", "-d"]
    if build:
        args.append("--build")
    run(args, capture=False, env=_build_env(docker_host))


def compose_down(
    *,
    project: str,
    files: list[Path],
    volumes: bool = False,
    docker_host: str | None = None,
) -> None:
    args = _compose_args(project, files)
    args.append("down")
    if volumes:
        args.append("--volumes")
    run(args, capture=False, check=False, env=_build_env(docker_host))


def run_cloudflared(
    *, container: str, network: str, token: str, docker_host: str | None = None
) -> None:
    """Start this project's own cloudflared sidecar.

    `--env TUNNEL_TOKEN` (no `=value`) tells docker to pass the value through
    from this process's own environment instead of putting the token in the
    `docker run` argv, where it would be visible to anything that can list
    processes on the host.
    """
    remove_container(container, docker_host=docker_host)
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
            "TUNNEL_TOKEN",
            cloudflared_image(),
            "tunnel",
            "--no-autoupdate",
            "run",
        ],
        env=_build_env(docker_host, {"TUNNEL_TOKEN": token}),
    )


def remove_container(name: str, *, docker_host: str | None = None) -> None:
    run(["rm", "-f", name], check=False, env=_build_env(docker_host))


def container_status(name: str, *, docker_host: str | None = None) -> str:
    """Human-readable state of one container, or 'not found'."""
    out = run(
        ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Status}}"],
        check=False,
        env=_build_env(docker_host),
    ).strip()
    return out.splitlines()[0] if out else "not found"


def container_logs(name: str, *, tail: int = 5, docker_host: str | None = None) -> str:
    """The last few log lines of one container, or '' if there are none."""
    return run(
        ["logs", "--tail", str(tail), name], check=False, env=_build_env(docker_host)
    ).strip()
