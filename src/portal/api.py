"""The whole of portal, callable directly. No CLI, no prompts, no separate
setup step — call `deploy()` and it either does the thing or raises a
PortalError with a plain-English reason.

Ordering in deploy(): the app container comes up first so cloudflared has
something to reach, the tunnel and its ingress are configured before DNS is
pointed at it, and DNS is last so the hostname never resolves to a tunnel
that is not yet routing. destroy() unwinds in the opposite order.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .cloudflare import dns as dns_mod
from .cloudflare import ingress as ingress_mod
from .cloudflare import tunnels as tunnel_mod
from .cloudflare.client import CloudflareClient
from .config import Paths
from .detect import ProjectPlan, detect
from .errors import DockerError, PortalError
from .runtime import docker, override
from .settings import Settings, load_settings
from .state import ProjectRecord, StateStore

log = logging.getLogger("portal")

__all__ = [
    "DeployResult",
    "DestroyResult",
    "StatusResult",
    "deploy",
    "destroy",
    "status",
    "inspect",
]


@dataclass(frozen=True)
class DeployResult:
    slug: str
    fqdn: str
    live_url: str
    tunnel_id: str
    container_name: str
    container_status: str
    cloudflared_container: str
    cloudflared_status: str
    verified: bool
    probe: str


@dataclass(frozen=True)
class DestroyResult:
    slug: str
    dns_removed: bool
    tunnel_deleted: bool


@dataclass(frozen=True)
class StatusResult:
    record: ProjectRecord
    container_status: str
    cloudflared_status: str
    probe: str | None


def _client(settings: Settings) -> CloudflareClient:
    return CloudflareClient(settings.token)


def _account_id(client: CloudflareClient, settings: Settings) -> str:
    return settings.account_id or client.resolve_account_id()


def _compose_files(plan: ProjectPlan, paths: Paths) -> tuple[list[Path], Path]:
    """The -f chain: base compose (project's own, or synthesized) then override."""
    if plan.compose_file is not None:
        base = plan.compose_file
    else:
        base = override.write_yaml(
            override.synthesize_compose(plan), paths.work / "compose.generated.yml"
        )
    override_file = override.write_yaml(
        override.build_override(plan), paths.work / "compose.override.yml"
    )
    return [base, override_file], override_file


def inspect(path: str, *, port: int | None = None, service: str | None = None) -> ProjectPlan:
    """What deploy() would do, with no side effects."""
    return detect(path, port=port, service=service)


def _probe(fqdn: str) -> str:
    try:
        resp = requests.get(f"https://{fqdn}", timeout=10, allow_redirects=False)
        return f"HTTP {resp.status_code}"
    except requests.RequestException as exc:
        return f"unreachable ({type(exc).__name__})"


def _wait_for_container(
    name: str, *, docker_host: str | None = None, timeout: float = 30, interval: float = 1.5
) -> None:
    """Block until the container is actually Up, not just started. compose up -d
    returns as soon as the container is created — a bad image/command can exit
    it a second later, and creating cloud resources for that is wasted API
    calls plus a tunnel pointed at nothing."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = docker.container_status(name, docker_host=docker_host)
        if last.lower().startswith("up"):
            return
        time.sleep(interval)
    raise DockerError(
        f"{name} did not reach a running state within {timeout:.0f}s "
        f"(last status: {last or 'not found'}). Check `docker logs {name}`."
    )


def _wait_for_http(fqdn: str, *, timeout: float = 60, interval: float = 3) -> tuple[bool, str]:
    """Poll the public hostname until it answers or the timeout passes. DNS and
    the tunnel can take a few seconds to settle after being created, so a
    single immediate curl is expected to fail even on a good deploy."""
    deadline = time.monotonic() + timeout
    result = "not checked"
    while time.monotonic() < deadline:
        result = _probe(fqdn)
        if result.startswith("HTTP"):
            return True, result
        time.sleep(interval)
    return False, result


def deploy(
    path: str,
    host: str,
    *,
    port: int | None = None,
    service: str | None = None,
    env_file: str | Path | None = None,
    docker_host: str | None = None,
) -> DeployResult:
    """Deploy PATH and publish it at HOST. Creates real Cloudflare resources
    and starts containers — no confirmation prompt, the caller owns that.

    `docker_host`, e.g. "ssh://user@1.2.3.4", targets a remote docker daemon
    over SSH instead of the local one. The project directory at PATH is still
    read locally (portal, not the remote box, needs the compose/Dockerfile);
    only the container runtime moves. Requires `ssh` on PATH locally and
    key-based access to a docker-capable account on the remote host.
    """
    plan = detect(path, port=port, service=service, fqdn=host)
    docker.ensure_daemon_running(docker_host=docker_host)
    settings = load_settings(env_file)
    client = _client(settings)
    account_id = _account_id(client, settings)
    store = StateStore()

    paths = Paths.for_slug(plan.slug)

    log.info("network %s", plan.network)
    docker.ensure_network(plan.network, docker_host=docker_host)

    files, override_file = _compose_files(plan, paths)
    log.info("docker compose up (%s)", plan.compose_project)
    env_path = plan.source_path / ".env"
    docker.compose_up(
        project=plan.compose_project,
        files=files,
        build=True,
        env_file=env_path if env_path.is_file() else None,
        docker_host=docker_host,
    )
    log.info("waiting for %s to be up", plan.container_name)
    _wait_for_container(plan.container_name, docker_host=docker_host)

    existing = tunnel_mod.find_tunnel(client, account_id, plan.slug)
    if existing:
        tunnel = existing
        log.info("reusing tunnel %s", tunnel["id"])
    else:
        tunnel = tunnel_mod.create_tunnel(client, account_id, plan.slug)
        log.info("created tunnel %s", tunnel["id"])
    tunnel_id = tunnel["id"]
    tunnel_token = tunnel_mod.get_tunnel_token(client, account_id, tunnel_id)

    log.info("starting %s", plan.cloudflared_container)
    docker.run_cloudflared(
        container=plan.cloudflared_container,
        network=plan.network,
        token=tunnel_token,
        docker_host=docker_host,
    )
    del tunnel_token

    log.info("ingress %s -> %s", host, plan.origin_url)
    ingress_mod.put_config(
        client, account_id, tunnel_id, ingress_mod.build_config(host, plan.origin_url)
    )

    zone = dns_mod.resolve_zone(client, host)
    log.info("zone %s (%s)", zone["name"], zone["id"])
    record_id = dns_mod.upsert_cname(client, zone["id"], host, tunnel_id, plan.slug)
    log.info("CNAME %s -> %s (proxied)", host, dns_mod.tunnel_target(tunnel_id))

    log.info("curling https://%s until it answers", host)
    verified, probe_result = _wait_for_http(host)
    log.info("probe: %s%s", probe_result, "" if verified else " (gave up waiting)")

    store.put(
        ProjectRecord(
            slug=plan.slug,
            source_path=str(plan.source_path),
            compose_file=str(files[0]),
            override_file=str(override_file),
            service=plan.service,
            container_name=plan.container_name,
            port=plan.port,
            fqdn=host,
            tunnel_id=tunnel_id,
            zone_id=zone["id"],
            dns_record_id=record_id,
            network=plan.network,
            cloudflared_container=plan.cloudflared_container,
            docker_host=docker_host or "",
        )
    )

    return DeployResult(
        slug=plan.slug,
        fqdn=host,
        live_url=f"https://{host}",
        tunnel_id=tunnel_id,
        container_name=plan.container_name,
        container_status=docker.container_status(plan.container_name, docker_host=docker_host),
        cloudflared_container=plan.cloudflared_container,
        cloudflared_status=docker.container_status(
            plan.cloudflared_container, docker_host=docker_host
        ),
        verified=verified,
        probe=probe_result,
    )


def destroy(
    slug: str,
    *,
    volumes: bool = False,
    env_file: str | Path | None = None,
    docker_host: str | None = None,
) -> DestroyResult:
    """Tear down the deployment named SLUG: DNS, tunnel, containers, state.
    No confirmation prompt, the caller owns that.

    `docker_host` defaults to whatever host deploy() recorded for this slug,
    so a remote deployment tears down on the same machine automatically —
    pass it explicitly only to override that.
    """
    store = StateStore()
    record = store.require(slug)
    docker_host = docker_host if docker_host is not None else (record.docker_host or None)
    settings = load_settings(env_file)
    client = _client(settings)

    dns_removed = False
    try:
        dns_removed = dns_mod.delete_record(
            client, record.zone_id, record.dns_record_id, slug
        )
        log.info("DNS record %s", "deleted" if dns_removed else "already gone")
    except PortalError as exc:
        # A refused delete must not strand the containers: report and carry on.
        log.warning("DNS: %s", exc)

    tunnel_deleted = False
    try:
        account_id = _account_id(client, settings)
        tunnel_mod.delete_tunnel(client, account_id, record.tunnel_id)
        tunnel_deleted = True
        log.info("tunnel %s deleted", record.tunnel_id)
    except PortalError as exc:
        log.warning("tunnel: %s", exc)

    docker.remove_container(record.cloudflared_container, docker_host=docker_host)
    log.info("%s removed", record.cloudflared_container)

    files = [Path(record.compose_file), Path(record.override_file)]
    docker.compose_down(
        project=f"portal-{slug}",
        files=[f for f in files if f.exists()],
        volumes=volumes,
        docker_host=docker_host,
    )
    docker.remove_network(record.network, docker_host=docker_host)

    store.delete(slug)
    log.info("state entry for '%s' removed", slug)

    return DestroyResult(slug=slug, dns_removed=dns_removed, tunnel_deleted=tunnel_deleted)


def status(slug: str | None = None, *, probe: bool = True) -> list[StatusResult]:
    """Report only — never fixes anything. Reads local state, no Cloudflare token
    needed. Each record carries its own docker_host from when it was deployed,
    so a fleet mixing local and remote deployments reports correctly in one call."""
    store = StateStore()
    records = [store.require(slug)] if slug else store.list()

    return [
        StatusResult(
            record=record,
            container_status=docker.container_status(
                record.container_name, docker_host=record.docker_host or None
            ),
            cloudflared_status=docker.container_status(
                record.cloudflared_container, docker_host=record.docker_host or None
            ),
            probe=_probe(record.fqdn) if probe else None,
        )
        for record in sorted(records, key=lambda r: r.slug)
    ]
