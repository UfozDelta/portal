"""Runs api.deploy() for real — full orchestration, ordering, state write,
result shape — with docker and the Cloudflare API stubbed at the module
boundary. No docker daemon, no network, no Cloudflare account needed.

This does not test docker.py or cloudflare/*.py themselves (those need a live
daemon/API and are out of scope, same as elsewhere in this suite) — it tests
that api.deploy() wires them together correctly.
"""

from __future__ import annotations

import shutil

import pytest

from portal import api
from portal.cloudflare.credentials import Check, CredentialReport
from portal.errors import CloudflareError, ConfigError, OwnershipError, PortalError
from portal.state import StateStore


def _mock_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_HOME", str(tmp_path / ".portal"))

    monkeypatch.setattr(api.docker, "ensure_daemon_running", lambda **kw: None)
    monkeypatch.setattr(api.docker, "ensure_network", lambda *a, **kw: None)
    monkeypatch.setattr(api.docker, "compose_up", lambda **kw: None)
    monkeypatch.setattr(api.docker, "container_status", lambda *a, **kw: "Up 2 seconds")
    monkeypatch.setattr(api.docker, "run_cloudflared", lambda **kw: None)
    monkeypatch.setattr(api.docker, "remove_container", lambda *a, **kw: None)
    monkeypatch.setattr(api.docker, "compose_down", lambda **kw: None)
    monkeypatch.setattr(api.docker, "remove_network", lambda *a, **kw: None)

    monkeypatch.setattr(api.tunnel_mod, "find_tunnel", lambda *a, **kw: None)
    monkeypatch.setattr(
        api.tunnel_mod, "create_tunnel", lambda *a, **kw: {"id": "tun-abc"}
    )
    monkeypatch.setattr(
        api.tunnel_mod, "get_tunnel_token", lambda *a, **kw: "fake-tunnel-token"
    )
    monkeypatch.setattr(api.tunnel_mod, "delete_tunnel", lambda *a, **kw: True)
    monkeypatch.setattr(api.ingress_mod, "put_config", lambda *a, **kw: {})
    monkeypatch.setattr(api.dns_mod, "upsert_cname", lambda *a, **kw: "rec-1")
    monkeypatch.setattr(api.dns_mod, "delete_record", lambda *a, **kw: True)
    monkeypatch.setattr(
        api.credentials,
        "check",
        lambda client, account_id, host: CredentialReport(
            checks=[Check("token", True, "active account token")],
            account_id="acct-1",
            zone={"id": "zone-1", "name": "example.com"},
        ),
    )

    monkeypatch.setattr(api, "_probe", lambda fqdn: "HTTP 200")


def _fast_clock(monkeypatch):
    # time.sleep(0) still advances wall-clock time as far as time.monotonic()
    # is concerned, so faking sleep alone doesn't shorten the retry window —
    # fake the clock itself so the deadline trips after a couple of iterations
    # instead of a real minute.
    fake_time = [0.0]

    def fake_monotonic():
        fake_time[0] += 25.0
        return fake_time[0]

    monkeypatch.setattr(api.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)


def _env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("CLOUDFLARE_API_TOKEN=test-token\nCLOUDFLARE_ACCOUNT_ID=acct-1\n")
    return f


def test_deploy_runs_with_no_docker_or_network(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)

    result = api.deploy(
        str(fixtures_dir / "simple_dockerfile"),
        "app.example.com",
        env_file=_env_file(tmp_path),
    )

    assert result.slug == "simple-dockerfile"
    assert result.tunnel_id == "tun-abc"
    assert result.live_url == "https://app.example.com"
    assert result.verified is True
    assert result.probe == "HTTP 200"
    assert result.container_status == "Up 2 seconds"


def test_deploy_records_state_that_status_and_destroy_can_read(
    monkeypatch, tmp_path, fixtures_dir
):
    _mock_everything(monkeypatch, tmp_path)

    api.deploy(
        str(fixtures_dir / "simple_dockerfile"),
        "app.example.com",
        env_file=_env_file(tmp_path),
    )

    record = StateStore().require("simple-dockerfile")
    assert record.tunnel_id == "tun-abc"
    assert record.fqdn == "app.example.com"
    assert record.zone_id == "zone-1"
    assert record.dns_record_id == "rec-1"

    results = api.status("simple-dockerfile")
    assert len(results) == 1
    assert results[0].container_status == "Up 2 seconds"
    assert results[0].probe == "HTTP 200"

    destroyed = api.destroy("simple-dockerfile", env_file=_env_file(tmp_path))
    assert destroyed.dns_removed is True
    assert destroyed.tunnel_deleted is True
    assert StateStore().get("simple-dockerfile") is None


def test_deploy_reuses_existing_tunnel_instead_of_creating_one(
    monkeypatch, tmp_path, fixtures_dir
):
    _mock_everything(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api.tunnel_mod, "find_tunnel", lambda *a, **kw: {"id": "tun-existing"}
    )

    def _fail_create(*a, **kw):
        raise AssertionError("should not create a new tunnel when one already exists")

    monkeypatch.setattr(api.tunnel_mod, "create_tunnel", _fail_create)

    result = api.deploy(
        str(fixtures_dir / "simple_dockerfile"),
        "app.example.com",
        env_file=_env_file(tmp_path),
    )
    assert result.tunnel_id == "tun-existing"


def test_deploy_never_verified_when_the_probe_never_answers(
    monkeypatch, tmp_path, fixtures_dir
):
    _mock_everything(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "_probe", lambda fqdn: "unreachable (ConnectionError)")
    _fast_clock(monkeypatch)

    result = api.deploy(
        str(fixtures_dir / "simple_dockerfile"),
        "app.example.com",
        port=8080,
        env_file=_env_file(tmp_path),
    )
    # deploy() does not raise when verification times out — it reports it.
    assert result.verified is False
    assert "unreachable" in result.probe


def test_bad_token_fails_before_any_container_starts(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api.credentials,
        "check",
        lambda *a, **kw: CredentialReport(checks=[Check("token", False, "token is expired")]),
    )

    def must_not_start(**kw):
        raise AssertionError("compose up must not run with an unusable token")

    monkeypatch.setattr(api.docker, "compose_up", must_not_start)
    with pytest.raises(ConfigError, match="token is expired"):
        api.deploy(
            str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path)
        )
    assert StateStore().get("simple-dockerfile") is None


def test_cloudflare_edge_error_is_not_verified(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api, "_probe", lambda fqdn: "HTTP 530 (Cloudflare could not reach the app through the tunnel)"
    )
    _fast_clock(monkeypatch)

    result = api.deploy(
        str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path)
    )
    assert result.verified is False
    assert result.probe.startswith("HTTP 530")


def test_app_error_status_still_counts_as_answered():
    # the app itself answering 500 or 404 means routing works
    assert api._app_answered("HTTP 500")
    assert api._app_answered("HTTP 404")
    assert not api._app_answered("HTTP 502 (Cloudflare could not reach the app through the tunnel)")
    assert not api._app_answered("unreachable (ConnectionError)")


@pytest.mark.parametrize(
    "bad", ["https://app.example.com", "app.example.com/x", "app.example.com:8443", "localhost", ""]
)
def test_deploy_rejects_non_hostnames(monkeypatch, tmp_path, fixtures_dir, bad):
    _mock_everything(monkeypatch, tmp_path)
    with pytest.raises(PortalError, match="bare hostname"):
        api.deploy(str(fixtures_dir / "simple_dockerfile"), bad, env_file=_env_file(tmp_path))


def test_deploy_normalizes_hostname(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    result = api.deploy(
        str(fixtures_dir / "simple_dockerfile"), " App.Example.com. ", env_file=_env_file(tmp_path)
    )
    assert result.fqdn == "app.example.com"


def test_destroy_stops_cloudflared_before_deleting_tunnel(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path))

    calls = []
    monkeypatch.setattr(api.docker, "remove_container", lambda name, **kw: calls.append(("rm", name)))
    monkeypatch.setattr(
        api.tunnel_mod, "delete_tunnel", lambda *a, **kw: calls.append(("tunnel",)) or True
    )
    api.destroy("simple-dockerfile", env_file=_env_file(tmp_path))
    assert calls == [("rm", "portal-simple-dockerfile-cloudflared"), ("tunnel",)]


def test_failed_deploy_leaves_state_that_destroy_can_clean_up(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)

    def dns_refused(*a, **kw):
        raise OwnershipError("A DNS record for app.example.com already exists")

    monkeypatch.setattr(api.dns_mod, "upsert_cname", dns_refused)
    with pytest.raises(CloudflareError):
        api.deploy(
            str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path)
        )

    record = StateStore().require("simple-dockerfile")
    assert record.tunnel_id == "tun-abc"
    assert record.dns_record_id == ""

    def must_not_touch_dns(*a, **kw):
        raise AssertionError("no DNS record was created, so none should be deleted")

    monkeypatch.setattr(api.dns_mod, "delete_record", must_not_touch_dns)
    result = api.destroy("simple-dockerfile", env_file=_env_file(tmp_path))
    assert result.tunnel_deleted is True
    assert result.state_removed is True
    assert StateStore().get("simple-dockerfile") is None


def test_destroy_keeps_state_when_tunnel_delete_fails(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path))

    def boom(*a, **kw):
        raise CloudflareError("Cloudflare rejected DELETE: 1022: active connections")

    monkeypatch.setattr(api.tunnel_mod, "delete_tunnel", boom)
    result = api.destroy("simple-dockerfile", env_file=_env_file(tmp_path))
    assert result.state_removed is False
    assert StateStore().get("simple-dockerfile") is not None


def test_destroy_drops_state_when_dns_is_owned_by_someone_else(
    monkeypatch, tmp_path, fixtures_dir
):
    _mock_everything(monkeypatch, tmp_path)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path))

    def not_ours(*a, **kw):
        raise OwnershipError("no longer marked as managed by portal")

    monkeypatch.setattr(api.dns_mod, "delete_record", not_ours)
    result = api.destroy("simple-dockerfile", env_file=_env_file(tmp_path))
    assert result.state_removed is True


def test_same_dir_name_elsewhere_is_refused(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path))

    twin = tmp_path / "elsewhere" / "simple_dockerfile"
    shutil.copytree(fixtures_dir / "simple_dockerfile", twin)
    with pytest.raises(PortalError, match="already exists"):
        api.deploy(str(twin), "other.example.com", env_file=_env_file(tmp_path))

    result = api.deploy(str(twin), "other.example.com", name="twin", env_file=_env_file(tmp_path))
    assert result.slug == "twin"
    assert StateStore().get("simple-dockerfile").fqdn == "app.example.com"


def test_redeploy_to_new_host_removes_old_record(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "old.example.com", env_file=_env_file(tmp_path))

    deleted = []
    monkeypatch.setattr(api.dns_mod, "upsert_cname", lambda *a, **kw: "rec-2")
    monkeypatch.setattr(
        api.dns_mod, "delete_record", lambda client, zone, rec, slug: deleted.append(rec) or True
    )
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "new.example.com", env_file=_env_file(tmp_path))

    assert deleted == ["rec-1"]
    record = StateStore().require("simple-dockerfile")
    assert (record.fqdn, record.dns_record_id) == ("new.example.com", "rec-2")


def test_redeploy_same_host_keeps_record(monkeypatch, tmp_path, fixtures_dir):
    _mock_everything(monkeypatch, tmp_path)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path))

    def must_not_delete(*a, **kw):
        raise AssertionError("same record id, nothing to remove")

    monkeypatch.setattr(api.dns_mod, "delete_record", must_not_delete)
    api.deploy(str(fixtures_dir / "simple_dockerfile"), "app.example.com", env_file=_env_file(tmp_path))
