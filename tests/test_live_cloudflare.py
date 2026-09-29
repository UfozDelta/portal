"""Hits the real Cloudflare API with the real token from .env: creates a
throwaway tunnel, confirms it exists, deletes it, confirms it's gone. Proves
the token/account actually works end-to-end — the rest of the suite (see
test_deploy_mocked.py) stubs the Cloudflare client entirely and can't prove
that.

Opt-in, double-gated so a plain `pytest` run never touches your account:
  1. pyproject.toml sets `addopts = "-m 'not live'"`, so this module is
     excluded unless you explicitly ask for it.
  2. This module also skips unless PORTAL_RUN_LIVE_TESTS=1 is set, so even
     `pytest -m live` by itself (e.g. muscle memory, a stray CI config) still
     no-ops.

Run it with:
    PORTAL_RUN_LIVE_TESTS=1 pytest -m live -s -v tests/test_live_cloudflare.py

Creates and deletes a real tunnel named portal-smoketest-<random>. Never
touches a project's real tunnel (those are named portal-<project-slug>, not
portal-smoketest-*). Optionally exercises DNS (see test_live_dns_scope_works
below) only when PORTAL_LIVE_TEST_DNS=1 and PORTAL_LIVE_TEST_ZONE are also set
— that one briefly creates a real CNAME on a real zone.
"""

from __future__ import annotations

import os
import uuid

import pytest

from portal.cloudflare import dns as dns_mod
from portal.cloudflare import tunnels as tunnel_mod
from portal.cloudflare.client import CloudflareClient
from portal.errors import CloudflareError, ConfigError, PortalError
from portal.settings import load_settings

pytestmark = pytest.mark.live

if os.environ.get("PORTAL_RUN_LIVE_TESTS") != "1":
    pytest.skip(
        "live Cloudflare test skipped: set PORTAL_RUN_LIVE_TESTS=1 to run it "
        "(it creates and deletes a real tunnel under your account)",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def live_client() -> tuple[CloudflareClient, str]:
    try:
        settings = load_settings()
    except ConfigError as exc:
        pytest.skip(f"no usable .env for a live test: {exc}")

    client = CloudflareClient(settings.token)
    verified = client.verify_token(account_id=settings.account_id)
    print(f"\ntoken: {verified.get('status', 'unknown')}")

    account_id = settings.account_id or client.resolve_account_id()
    print(f"account: {account_id}")
    return client, account_id


def test_live_tunnel_create_verify_delete(live_client):
    """The whole point: prove Account > Cloudflare Tunnel > Edit actually works
    by doing the three things deploy()/destroy() do to a tunnel, against the
    real API, on a name that can never collide with a real deployment."""
    client, account_id = live_client
    slug = f"smoketest-{uuid.uuid4().hex[:8]}"
    name = tunnel_mod.tunnel_name(slug)
    print(f"tunnel: {name}")

    tunnel_id: str | None = None
    try:
        created = tunnel_mod.create_tunnel(client, account_id, slug)
        tunnel_id = created["id"]
        assert tunnel_id
        assert created["name"] == name
        print(f"created: {tunnel_id}")

        found = tunnel_mod.find_tunnel(client, account_id, slug)
        assert found is not None
        assert found["id"] == tunnel_id
        print("find_tunnel: located it")

        token = tunnel_mod.get_tunnel_token(client, account_id, tunnel_id)
        assert isinstance(token, str)
        assert len(token) > 50  # never print this — it's a live credential
        print("get_tunnel_token: looks credential-shaped")

        tunnel_mod.delete_tunnel(client, account_id, tunnel_id)
        print("delete_tunnel: no error")
        tunnel_id = None  # deleted; the finally block has nothing to clean up

        gone = tunnel_mod.find_tunnel(client, account_id, slug)
        assert gone is None
        print("find_tunnel: confirmed gone")
    finally:
        # Belt and suspenders: the client never retries a POST (a lost 5xx on
        # create_tunnel could mean it actually succeeded), and an assertion
        # above could have failed before delete_tunnel ran. Either way, make
        # sure nothing named portal-smoketest-* survives this test.
        if tunnel_id is not None:
            try:
                tunnel_mod.delete_tunnel(client, account_id, tunnel_id)
                print(f"cleanup: deleted {tunnel_id} after an earlier failure")
            except CloudflareError as exc:
                print(f"cleanup FAILED — remove '{name}' by hand: {exc}")
        else:
            leftover = tunnel_mod.find_tunnel(client, account_id, slug)
            if leftover is not None:
                try:
                    tunnel_mod.delete_tunnel(client, account_id, leftover["id"])
                    print(f"cleanup: deleted stray {leftover['id']}")
                except CloudflareError as exc:
                    print(f"cleanup FAILED — remove '{name}' by hand: {exc}")


def test_live_dns_zone_is_visible(live_client):
    """Read-only: proves Zone:Zone:Read works and the account can see the zone
    you plan to deploy to. No DNS record is created — see
    test_live_dns_scope_works for the opt-in version that actually writes one."""
    client, _account_id = live_client
    zone_name = os.environ.get("PORTAL_LIVE_TEST_ZONE")
    if not zone_name:
        pytest.skip("set PORTAL_LIVE_TEST_ZONE=example.com to check zone visibility")

    zone = dns_mod.resolve_zone(client, zone_name)
    assert zone["id"]
    print(f"zone visible: {zone['name']} ({zone['id']})")


def test_live_dns_scope_works(live_client):
    """Opt-in and separate from the tunnel test: proves Zone:DNS:Edit works by
    creating a real, ownership-stamped CNAME and deleting it again. A token
    can have Tunnel:Edit without DNS:Edit — deploy() needs both — but this
    writes a (briefly) public DNS record on a real zone, so it only runs when
    explicitly asked for."""
    if os.environ.get("PORTAL_LIVE_TEST_DNS") != "1":
        pytest.skip("set PORTAL_LIVE_TEST_DNS=1 (and PORTAL_LIVE_TEST_ZONE) to test DNS write access")
    zone_name = os.environ.get("PORTAL_LIVE_TEST_ZONE")
    if not zone_name:
        pytest.skip("PORTAL_LIVE_TEST_DNS=1 also needs PORTAL_LIVE_TEST_ZONE=example.com")

    client, account_id = live_client
    slug = f"smoketest-{uuid.uuid4().hex[:8]}"
    tunnel_name = tunnel_mod.tunnel_name(slug)
    fqdn = f"{slug}.{zone_name}"

    tunnel_id: str | None = None
    record_id: str | None = None
    try:
        tunnel = tunnel_mod.create_tunnel(client, account_id, slug)
        tunnel_id = tunnel["id"]

        zone = dns_mod.resolve_zone(client, fqdn)
        record_id = dns_mod.upsert_cname(client, zone["id"], fqdn, tunnel_id, slug)
        assert record_id
        print(f"created CNAME: {fqdn}")

        found = dns_mod.find_record(client, zone["id"], fqdn)
        assert found is not None
        assert dns_mod.is_portal_owned(found, slug)
        print("find_record + ownership stamp: confirmed")

        removed = dns_mod.delete_record(client, zone["id"], record_id, slug)
        assert removed is True
        record_id = None
        print("delete_record: no error")
    finally:
        if record_id is not None or tunnel_id is not None:
            try:
                zone = dns_mod.resolve_zone(client, fqdn)
                if record_id is not None:
                    dns_mod.delete_record(client, zone["id"], record_id, slug)
                    print(f"cleanup: deleted stray record for {fqdn}")
            except (PortalError, KeyError) as exc:
                print(f"cleanup FAILED — remove DNS record for '{fqdn}' by hand: {exc}")
        if tunnel_id is not None:
            try:
                tunnel_mod.delete_tunnel(client, account_id, tunnel_id)
                print(f"cleanup: deleted tunnel {tunnel_id}")
            except CloudflareError as exc:
                print(f"cleanup FAILED — remove tunnel '{tunnel_name}' by hand: {exc}")
