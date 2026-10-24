"""Read-only checks that the Cloudflare token can do what portal needs, run
before anything is created.

A token can't read its own permission list (that needs a separate
API Tokens:Read grant nobody gives a deploy token), so each permission is
probed with the cheapest read call that requires it. The API offers no way to
tell Read from Edit without writing something, so a Read-only token passes
here and fails at the first write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..errors import CloudflareError
from . import dns

# Warn when a token expires within this window, so it gets rotated before a
# deploy fails on it.
EXPIRY_WARNING = timedelta(days=7)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class CredentialReport:
    checks: list[Check] = field(default_factory=list)
    account_id: str | None = None
    # The zone covering the host, when a host was checked. deploy() reuses it.
    zone: dict | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def failures(self) -> str:
        return "\n".join(f"  {c.name}: {c.detail}" for c in self.checks if not c.ok)


def _rejected(exc: CloudflareError) -> bool:
    return 1000 in exc.codes or exc.status_code in (401, 403)


def _token_detail(token: dict) -> str:
    detail = f"active {token['kind']} token"
    expires = token.get("expires_on")
    if expires:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        detail += f", expires {when:%Y-%m-%d}"
        if when - datetime.now(timezone.utc) < EXPIRY_WARNING:
            detail += " (soon, rotate it)"
    return detail


def check(client, account_id: str | None, host: str | None = None) -> CredentialReport:
    """Run every check that applies and report each one. Stops early only
    when a later check depends on an earlier one (no account, bad token, no
    zone). Never raises for a failed check."""
    report = CredentialReport()

    def add(name: str, ok: bool, detail: str) -> None:
        report.checks.append(Check(name, ok, detail))

    try:
        report.account_id = account_id or client.resolve_account_id()
    except CloudflareError as exc:
        if _rejected(exc):
            add("token", False, "Cloudflare rejected the token: it is invalid or revoked")
        else:
            add("account", False, str(exc))
        return report

    try:
        token = client.verify_token(report.account_id)
    except CloudflareError as exc:
        add("token", False, f"Cloudflare rejected the token: {exc}")
        return report
    if token.get("status") != "active":
        add("token", False, f"{token['kind']} token is {token.get('status', 'unknown')}")
        return report
    add("token", True, _token_detail(token))

    try:
        client.get(f"/accounts/{report.account_id}/cfd_tunnel", params={"per_page": 1})
        add("tunnels", True, "can list tunnels")
    except CloudflareError as exc:
        add("tunnels", False, f"needs Account > Cloudflare Tunnel > Edit ({exc})")

    if host:
        try:
            report.zone = dns.resolve_zone(client, host)
        except CloudflareError as exc:
            add("zone", False, str(exc))
            return report
        name = report.zone["name"]
        add("zone", True, f"{name} covers {host}")
        try:
            client.get(f"/zones/{report.zone['id']}/dns_records", params={"per_page": 1})
            add("dns", True, f"can read DNS records in {name}")
        except CloudflareError as exc:
            add("dns", False, f"needs Zone > DNS > Edit on {name} ({exc})")

    return report
