"""credentials.check() against a fake client: which Cloudflare answers turn
into which reported check, and where it stops early."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from portal.cloudflare import credentials
from portal.errors import CloudflareError

ZONE = {"id": "zone-1", "name": "example.com"}


class FakeClient:
    """Answers the handful of GETs credentials.check() makes. Pass an
    exception for any of them to make that call fail."""

    def __init__(self, *, accounts=None, token=None, tunnels=None, zones=None, dns=None):
        self.accounts = accounts if accounts is not None else [{"id": "acct-1", "name": "me"}]
        self.token = token if token is not None else {"status": "active", "kind": "account"}
        self.tunnels = tunnels
        self.zones = zones if zones is not None else {"example.com": [ZONE]}
        self.dns = dns

    def resolve_account_id(self):
        if isinstance(self.accounts, Exception):
            raise self.accounts
        return self.accounts[0]["id"]

    def verify_token(self, account_id):
        if isinstance(self.token, Exception):
            raise self.token
        return self.token

    def get(self, path, params=None):
        if path.endswith("/cfd_tunnel"):
            outcome = self.tunnels
        elif path.endswith("/dns_records"):
            outcome = self.dns
        elif path == "/zones":
            return {"result": self.zones.get(params["name"], [])}
        else:
            raise AssertionError(f"unexpected GET {path}")
        if isinstance(outcome, Exception):
            raise outcome
        return {"result": []}


def _by_name(report):
    return {c.name: c for c in report.checks}


def test_everything_reachable():
    report = credentials.check(FakeClient(), None, "app.example.com")
    assert report.ok
    assert [c.name for c in report.checks] == ["token", "tunnels", "zone", "dns"]
    assert report.account_id == "acct-1"
    assert report.zone == ZONE


def test_without_host_only_checks_token_and_tunnels():
    report = credentials.check(FakeClient(), "acct-1")
    assert report.ok
    assert [c.name for c in report.checks] == ["token", "tunnels"]
    assert report.zone is None


def test_invalid_token_stops_at_once():
    bad = CloudflareError("Invalid API Token", status_code=401, codes=(1000,))
    report = credentials.check(FakeClient(accounts=bad), None, "app.example.com")
    assert not report.ok
    assert [c.name for c in report.checks] == ["token"]
    assert "invalid or revoked" in report.checks[0].detail


@pytest.mark.parametrize("status", ["expired", "disabled"])
def test_inactive_token_is_reported(status):
    report = credentials.check(
        FakeClient(token={"status": status, "kind": "user"}), "acct-1", "app.example.com"
    )
    assert not report.ok
    assert report.checks[-1].detail == f"user token is {status}"
    assert "tunnels" not in _by_name(report)


def test_expiry_soon_is_flagged_but_still_ok():
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = credentials.check(
        FakeClient(token={"status": "active", "kind": "account", "expires_on": soon}), "acct-1"
    )
    assert report.ok
    assert "rotate" in _by_name(report)["token"].detail


def test_missing_tunnel_permission_still_checks_dns():
    denied = CloudflareError("Authentication error", status_code=403, codes=(10000,))
    report = credentials.check(FakeClient(tunnels=denied), "acct-1", "app.example.com")
    checks = _by_name(report)
    assert not report.ok
    assert not checks["tunnels"].ok
    assert "Cloudflare Tunnel" in checks["tunnels"].detail
    assert checks["dns"].ok
    assert "tunnels:" in report.failures()


def test_no_zone_for_host():
    report = credentials.check(FakeClient(zones={}), "acct-1", "app.other.org")
    checks = _by_name(report)
    assert not checks["zone"].ok
    assert "dns" not in checks


def test_missing_dns_permission():
    denied = CloudflareError("Authentication error", status_code=403)
    report = credentials.check(FakeClient(dns=denied), "acct-1", "app.example.com")
    assert not _by_name(report)["dns"].ok


def test_verify_token_falls_back_to_user_endpoint_on_1000(monkeypatch):
    from portal.cloudflare.client import CloudflareClient

    client = CloudflareClient("t" * 40)
    calls = []

    def fake_get(path, **kw):
        calls.append(path)
        if path.startswith("/accounts/"):
            raise CloudflareError("Invalid API Token", status_code=401, codes=(1000,))
        return {"result": {"status": "active"}}

    monkeypatch.setattr(client, "get", fake_get)
    assert client.verify_token("acct-1") == {"status": "active", "kind": "user"}
    assert calls == ["/accounts/acct-1/tokens/verify", "/user/tokens/verify"]


def test_verify_token_does_not_mask_other_errors(monkeypatch):
    from portal.cloudflare.client import CloudflareClient

    client = CloudflareClient("t" * 40)

    def fake_get(path, **kw):
        raise CloudflareError("rate limited", status_code=429, codes=(10013,))

    monkeypatch.setattr(client, "get", fake_get)
    with pytest.raises(CloudflareError, match="rate limited"):
        client.verify_token("acct-1")
