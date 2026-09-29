"""Runs api.deploy() for real — full orchestration, ordering, state write,
result shape — with docker and the Cloudflare API stubbed at the module
boundary. No docker daemon, no network, no Cloudflare account needed.

This does not test docker.py or cloudflare/*.py themselves (those need a live
daemon/API and are out of scope, same as elsewhere in this suite) — it tests
that api.deploy() wires them together correctly.
"""

from __future__ import annotations

from portal import api
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
    monkeypatch.setattr(api.tunnel_mod, "delete_tunnel", lambda *a, **kw: None)
    monkeypatch.setattr(api.ingress_mod, "put_config", lambda *a, **kw: {})
    monkeypatch.setattr(
        api.dns_mod,
        "resolve_zone",
        lambda *a, **kw: {"id": "zone-1", "name": "example.com"},
    )
    monkeypatch.setattr(api.dns_mod, "upsert_cname", lambda *a, **kw: "rec-1")
    monkeypatch.setattr(api.dns_mod, "delete_record", lambda *a, **kw: True)

    monkeypatch.setattr(api, "_account_id", lambda client, settings: "acct-1")
    monkeypatch.setattr(api, "_probe", lambda fqdn: "HTTP 200")


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

    # time.sleep(0) still advances wall-clock time as far as time.monotonic()
    # is concerned, so faking sleep alone doesn't shorten the 60s retry
    # window — fake the clock itself so the deadline trips after a couple of
    # iterations instead of a real minute.
    fake_time = [0.0]

    def fake_monotonic():
        fake_time[0] += 25.0
        return fake_time[0]

    monkeypatch.setattr(api.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(api.time, "sleep", lambda _seconds: None)

    result = api.deploy(
        str(fixtures_dir / "simple_dockerfile"),
        "app.example.com",
        port=8080,
        env_file=_env_file(tmp_path),
    )
    # deploy() does not raise when verification times out — it reports it.
    assert result.verified is False
    assert "unreachable" in result.probe
