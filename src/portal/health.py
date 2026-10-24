"""Post-deploy health checks: is everything a deployment needs actually there
and working? Read-only, and every check reports instead of raising, so one
broken piece never hides the state of the rest.

The checks run in the order a request travels: project .env, app container,
cloudflared container, tunnel connections, ingress rule, DNS record. The final
HTTP probe is added by the caller, which already has the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .cloudflare import dns, ingress, tunnels
from .cloudflare.credentials import Check
from .detect.env import check_env
from .errors import CloudflareError, PortalError
from .runtime import docker
from .state import ProjectRecord

# Cloudflare reports a tunnel as "healthy" only when every expected connection
# is up; "degraded" means some are, which still serves traffic.
_TUNNEL_SERVING = ("healthy", "degraded")


@dataclass
class HealthReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))

    def failures(self) -> str:
        return "\n".join(f"  {c.name}: {c.detail}" for c in self.checks if not c.ok)


def _container(report: HealthReport, label: str, name: str, docker_host: str | None) -> None:
    try:
        status = docker.container_status(name, docker_host=docker_host)
    except PortalError as exc:
        report.add(label, False, str(exc))
        return
    if status.lower().startswith("up"):
        report.add(label, True, status)
        return
    detail = f"{name}: {status}"
    try:
        logs = docker.container_logs(name, docker_host=docker_host)
    except PortalError:
        logs = ""
    if logs:
        detail += "\n" + "\n".join(f"      {line}" for line in logs.splitlines())
    report.add(label, False, detail)


def _cloudflare(report: HealthReport, record: ProjectRecord, client, account_id: str) -> None:
    try:
        tunnel = tunnels.get_tunnel(client, account_id, record.tunnel_id)
        status = tunnel.get("status", "unknown")
        connections = len(tunnel.get("connections") or [])
        report.add(
            "tunnel",
            status in _TUNNEL_SERVING and connections > 0,
            f"{status}, {connections} connection{'s' if connections != 1 else ''}",
        )
    except CloudflareError as exc:
        report.add("tunnel", False, str(exc))

    origin = f"http://{record.container_name}:{record.port}"
    try:
        rules = ingress.get_config(client, account_id, record.tunnel_id).get("ingress") or []
        match = next((r for r in rules if r.get("hostname") == record.fqdn), None)
        if match is None:
            report.add("ingress", False, f"no rule for {record.fqdn}")
        elif match.get("service") != origin:
            report.add(
                "ingress", False, f"{record.fqdn} points at {match.get('service')}, not {origin}"
            )
        elif not rules or not ingress.is_catch_all(rules[-1]):
            report.add("ingress", False, "the catch-all rule is missing")
        else:
            report.add("ingress", True, f"{record.fqdn} -> {origin}")
    except CloudflareError as exc:
        report.add("ingress", False, str(exc))

    if not record.dns_record_id:
        report.add("dns", False, "no DNS record is recorded for this deployment")
        return
    try:
        rec = dns.get_record(client, record.zone_id, record.dns_record_id)
        want = dns.tunnel_target(record.tunnel_id)
        if rec.get("content") != want:
            report.add(
                "dns", False, f"{rec.get('name')} points at {rec.get('content')}, not {want}"
            )
        elif not rec.get("proxied"):
            report.add("dns", False, f"{rec.get('name')} is not proxied (orange cloud off)")
        else:
            report.add("dns", True, f"{rec.get('name')} -> {want} (proxied)")
    except CloudflareError as exc:
        report.add("dns", False, str(exc))


def run(record: ProjectRecord, *, client=None, account_id: str | None = None) -> HealthReport:
    """Check one recorded deployment. Without a Cloudflare client only the
    local checks (env, containers) run."""
    report = HealthReport()
    host = record.docker_host or None

    source = Path(record.source_path)
    if source.is_dir():
        env = check_env(source)
        if env.problems:
            report.add("env", False, f"{source / '.env'} {env.describe()}")
        else:
            report.add(
                "env",
                True,
                ".env covers .env.example" if env.has_example else "no .env.example to check",
            )
    else:
        report.add("env", False, f"project directory {source} no longer exists")

    _container(report, "app", record.container_name, host)
    _container(report, "cloudflared", record.cloudflared_container, host)

    if client is not None and record.tunnel_id and account_id:
        _cloudflare(report, record, client, account_id)
    return report
