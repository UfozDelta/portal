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
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

import requests

from .cloudflare import credentials
from .cloudflare import dns as dns_mod
from .cloudflare import ingress as ingress_mod
from .cloudflare import tunnels as tunnel_mod
from .cloudflare.client import CloudflareClient
from .cloudflare.credentials import CredentialReport
from . import health as health_mod
from .config import project_dir, resource_prefix
from .detect import ProjectPlan, detect
from .errors import ConfigError, DockerError, OwnershipError, PortalError
from .health import HealthReport
from .runtime import docker, override
from .settings import Settings, load_settings
from .state import ProjectRecord, StateStore

log = logging.getLogger("portal")

__all__ = [
    "CredentialReport",
    "DeployResult",
    "HealthReport",
    "DestroyResult",
    "StatusResult",
    "check",
    "deploy",
    "destroy",
    "status",
    "inspect",
    "verify",
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
    # Every check in one place (env, containers, tunnel, ingress, DNS, HTTP).
    # Never raised on: a failed check shows up here, with the deployment left
    # in place for destroy() or a fix and redeploy.
    health: HealthReport


@dataclass(frozen=True)
class DestroyResult:
    slug: str
    dns_removed: bool
    tunnel_deleted: bool
    # False when a DNS or tunnel deletion failed for a retryable reason; the
    # state entry is kept so calling destroy() again finishes the job.
    state_removed: bool


@dataclass(frozen=True)
class StatusResult:
    record: ProjectRecord
    container_status: str
    cloudflared_status: str
    probe: str | None


def _client(settings: Settings) -> CloudflareClient:
    return CloudflareClient(settings.token)


def _compose_files(plan: ProjectPlan) -> list[Path]:
    """The -f chain: base compose (project's own, or synthesized) then override."""
    work = project_dir(plan.slug)
    if plan.compose_file is not None:
        base = plan.compose_file
    else:
        base = override.write_yaml(
            override.synthesize_compose(plan), work / "compose.generated.yml"
        )
    override_file = override.write_yaml(
        override.build_override(plan), work / "compose.override.yml"
    )
    return [base, override_file]


def inspect(
    path: str,
    *,
    port: int | None = None,
    service: str | None = None,
    name: str | None = None,
) -> ProjectPlan:
    """What deploy() would do, with no side effects."""
    return detect(path, port=port, service=service, name=name)


_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def _normalize_host(host: str) -> str:
    """A bare hostname, lowercased. Rejects schemes, ports and paths up front,
    instead of letting them surface later as a confusing zone-lookup error."""
    h = host.strip().lower().rstrip(".")
    labels = h.split(".")
    if len(labels) < 2 or len(h) > 253 or not all(_HOST_LABEL.match(l) for l in labels):
        raise PortalError(
            f"'{host}' is not a bare hostname. Pass something like "
            f"app.example.com, with no scheme, port or path."
        )
    return h


# Statuses Cloudflare's edge generates itself when it can't reach the app
# through the tunnel (530/1033: no connector; 502: connector can't reach the
# origin). Seeing one means the deploy is not working yet, not that the app
# answered.
EDGE_ERROR_STATUSES = frozenset({502, 504, *range(520, 531)})
_HTTP_STATUS = re.compile(r"^HTTP (\d+)")


def _probe(fqdn: str) -> str:
    try:
        resp = requests.get(f"https://{fqdn}", timeout=10, allow_redirects=False)
    except requests.RequestException as exc:
        return f"unreachable ({type(exc).__name__})"
    code = resp.status_code
    if code in EDGE_ERROR_STATUSES:
        return f"HTTP {code} (Cloudflare could not reach the app through the tunnel)"
    return f"HTTP {code}"


def _app_answered(probe: str) -> bool:
    match = _HTTP_STATUS.match(probe)
    return bool(match) and int(match.group(1)) not in EDGE_ERROR_STATUSES


def _poll(read, done, *, timeout: float, interval: float) -> tuple[bool, str]:
    """Call read() until done(result) or the timeout passes. Returns whether it
    finished and the last result ("" if it never got to read)."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = read()
        if done(last):
            return True, last
        time.sleep(interval)
    return False, last


def _wait_for_container(
    name: str, *, docker_host: str | None = None, timeout: float = 30, interval: float = 1.5
) -> None:
    """Block until the container is actually Up, not just started. compose up -d
    returns as soon as the container is created — a bad image/command can exit
    it a second later, and creating cloud resources for that is wasted API
    calls plus a tunnel pointed at nothing."""
    up, last = _poll(
        lambda: docker.container_status(name, docker_host=docker_host),
        lambda s: s.lower().startswith("up"),
        timeout=timeout,
        interval=interval,
    )
    if not up:
        raise DockerError(
            f"{name} did not reach a running state within {timeout:.0f}s "
            f"(last status: {last or 'not found'}). Check `docker logs {name}`."
        )


def _wait_for_http(fqdn: str, *, timeout: float = 60, interval: float = 3) -> tuple[bool, str]:
    """Poll the public hostname until it answers or the timeout passes. DNS and
    the tunnel can take a few seconds to settle after being created, so a
    single immediate curl is expected to fail even on a good deploy."""
    answered, last = _poll(
        lambda: _probe(fqdn), _app_answered, timeout=timeout, interval=interval
    )
    return answered, last or "not checked"


def check(host: str | None = None, *, env_file: str | Path | None = None) -> CredentialReport:
    """Check the Cloudflare token without creating anything: that it is
    active (and when it expires), that it can reach tunnels, and — given a
    host — that a zone covers it and its DNS records are reachable.
    deploy() runs the same checks before starting any containers.

    Raises ConfigError only when there is no token at all; every other
    problem is reported in the result (`report.ok`, `report.checks`).
    """
    settings = load_settings(env_file)
    return credentials.check(
        _client(settings), settings.account_id, _normalize_host(host) if host else None
    )


def deploy(
    path: str,
    host: str,
    *,
    port: int | None = None,
    service: str | None = None,
    name: str | None = None,
    env_file: str | Path | None = None,
    docker_host: str | None = None,
    strict_env: bool = True,
) -> DeployResult:
    """Deploy PATH and publish it at HOST. Creates real Cloudflare resources
    and starts containers — no confirmation prompt, the caller owns that.

    `name` sets the deployment's slug; it defaults to the directory name.

    `docker_host`, e.g. "ssh://user@1.2.3.4", targets a remote docker daemon
    over SSH instead of the local one. The project directory at PATH is still
    read locally (portal, not the remote box, needs the compose/Dockerfile);
    only the container runtime moves. Requires `ssh` on PATH locally and
    key-based access to a docker-capable account on the remote host.

    `strict_env` (default) refuses to start when a key listed in the project's
    .env.example is missing from its .env, or blank there while the example
    gives it a value. Pass False to only log a warning.

    State is saved as each resource is created, not only at the end, so a
    deploy that fails halfway can still be cleaned up with destroy().
    """
    host = _normalize_host(host)
    plan = detect(path, port=port, service=service, fqdn=host, name=name)
    store = StateStore()
    previous = store.get(plan.slug)
    if previous and Path(previous.source_path) != plan.source_path:
        raise PortalError(
            f"A deployment named '{plan.slug}' already exists for "
            f"{previous.source_path}. Pass name= to deploy {plan.source_path} "
            f"under a different name, or destroy('{plan.slug}') first."
        )
    if plan.env.problems:
        env_path = plan.source_path / ".env"
        if strict_env:
            raise ConfigError(
                f"{env_path} doesn't cover .env.example ({plan.env.describe()}). "
                f"Fix it, or pass strict_env=False to deploy anyway."
            )
        log.warning("%s doesn't cover .env.example (%s)", env_path, plan.env.describe())

    docker.ensure_daemon_running(docker_host=docker_host)
    settings = load_settings(env_file)
    client = _client(settings)
    # Before any container starts: a bad token should fail here, not after a
    # full build with the app already running.
    report = credentials.check(client, settings.account_id, host)
    if not report.ok:
        raise ConfigError(
            "The Cloudflare token can't do everything deploy needs:\n" + report.failures()
        )
    account_id, zone = report.account_id, report.zone

    files = _compose_files(plan)

    # Until the new DNS record exists, keep tracking the previous one (on a
    # redeploy), so a failure in between never loses track of it.
    record = ProjectRecord(
        slug=plan.slug,
        source_path=str(plan.source_path),
        compose_file=str(files[0]),
        override_file=str(files[1]),
        service=plan.service,
        container_name=plan.container_name,
        port=plan.port,
        fqdn=previous.fqdn if previous else host,
        tunnel_id=previous.tunnel_id if previous else "",
        zone_id=previous.zone_id if previous else "",
        dns_record_id=previous.dns_record_id if previous else "",
        network=plan.network,
        cloudflared_container=plan.cloudflared_container,
        docker_host=docker_host or "",
    )
    store.put(record)

    log.info("network %s", plan.network)
    docker.ensure_network(plan.network, docker_host=docker_host)

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
    record = replace(record, tunnel_id=tunnel_id)
    store.put(record)
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

    log.info("zone %s (%s)", zone["name"], zone["id"])
    record_id =dns_mod.upsert_cname(client, zone["id"], host, tunnel_id, plan.slug)
    log.info("CNAME %s -> %s (proxied)", host, dns_mod.tunnel_target(tunnel_id))

    if previous and previous.dns_record_id and previous.dns_record_id != record_id:
        # Redeployed under a new hostname: the old CNAME still points at this
        # tunnel, whose ingress no longer routes it, so it would only 404.
        try:
            dns_mod.delete_record(client, previous.zone_id, previous.dns_record_id, plan.slug)
            log.info("removed old DNS record for %s", previous.fqdn)
        except PortalError as exc:
            log.warning(
                "could not remove the old DNS record for %s, remove it by hand: %s",
                previous.fqdn,
                exc,
            )

    record = replace(record, fqdn=host, zone_id=zone["id"], dns_record_id=record_id)
    store.put(record)

    log.info("curling https://%s until it answers", host)
    verified, probe_result = _wait_for_http(host)
    log.info("probe: %s%s", probe_result, "" if verified else " (gave up waiting)")

    health = health_mod.run(record, client=client, account_id=account_id)
    health.add("http", verified, probe_result)
    if not health.ok:
        log.warning("deployment is not fully healthy:\n%s", health.failures())

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
        health=health,
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
    # Cleared when a Cloudflare deletion fails for a reason a retry could fix.
    # Containers are torn down regardless (a Cloudflare problem must not strand
    # them), but the state entry is kept so the leftover is never forgotten.
    complete = True

    dns_removed = False
    if record.dns_record_id:
        try:
            dns_removed = dns_mod.delete_record(
                client, record.zone_id, record.dns_record_id, slug
            )
            log.info("DNS record %s", "deleted" if dns_removed else "already gone")
        except OwnershipError as exc:
            # Someone else owns the record now; retrying can never change that.
            log.warning("DNS: %s", exc)
        except PortalError as exc:
            log.warning("DNS: %s", exc)
            complete = False

    # The connector has to stop before the tunnel is deleted: Cloudflare
    # refuses to delete a tunnel that still has active connections.
    docker.remove_container(record.cloudflared_container, docker_host=docker_host)
    log.info("%s removed", record.cloudflared_container)

    tunnel_deleted = False
    if record.tunnel_id:
        try:
            account_id = settings.account_id or client.resolve_account_id()
            tunnel_deleted = tunnel_mod.delete_tunnel(client, account_id, record.tunnel_id)
            log.info(
                "tunnel %s %s",
                record.tunnel_id,
                "deleted" if tunnel_deleted else "already gone",
            )
        except PortalError as exc:
            log.warning("tunnel: %s", exc)
            complete = False

    files = [Path(record.compose_file), Path(record.override_file)]
    docker.compose_down(
        project=resource_prefix(slug),
        files=[f for f in files if f.exists()],
        volumes=volumes,
        docker_host=docker_host,
    )
    docker.remove_network(record.network, docker_host=docker_host)

    if complete:
        store.delete(slug)
        log.info("state entry for '%s' removed", slug)
    else:
        log.warning(
            "kept the state entry for '%s' because cleanup is incomplete; "
            "run destroy('%s') again to finish",
            slug,
            slug,
        )

    return DestroyResult(
        slug=slug,
        dns_removed=dns_removed,
        tunnel_deleted=tunnel_deleted,
        state_removed=complete,
    )


def status(slug: str | None = None, *, probe: bool = True) -> list[StatusResult]:
    """Report only — never fixes anything. Reads local state, no Cloudflare token
    needed. Each record carries its own docker_host from when it was deployed,
    so a fleet mixing local and remote deployments reports correctly in one call."""
    store = StateStore()
    records = [store.require(slug)] if slug else store.list()

    def one(record: ProjectRecord) -> StatusResult:
        host = record.docker_host or None
        return StatusResult(
            record=record,
            container_status=docker.container_status(record.container_name, docker_host=host),
            cloudflared_status=docker.container_status(
                record.cloudflared_container, docker_host=host
            ),
            probe=_probe(record.fqdn) if probe else None,
        )

    return [one(record) for record in sorted(records, key=lambda r: r.slug)]



def verify(
    slug: str, *, env_file: str | Path | None = None, probe: bool = True
) -> HealthReport:
    """Run every health check on a deployed SLUG and report each result:
    project .env, both containers, the tunnel's connections, the ingress rule,
    the DNS record and a live HTTP request. Changes nothing.

    Needs the Cloudflare token for the tunnel, ingress and DNS checks. Without
    one (or with no .env), those are skipped and a `cloudflare` check says so.
    """
    record = StateStore().require(slug)
    client = account_id = None
    skipped = ""
    try:
        settings = load_settings(env_file)
        client = _client(settings)
        account_id = settings.account_id or client.resolve_account_id()
    except PortalError as exc:
        client = None
        skipped = str(exc)
    report = health_mod.run(record, client=client, account_id=account_id)
    if client is None:
        report.add("cloudflare", False, f"tunnel, ingress and DNS not checked: {skipped}")
    if probe:
        result = _probe(record.fqdn)
        report.add("http", _app_answered(result), result)
    return report
