import json
import os

import pytest

from portal.errors import StateError
from portal.state import ProjectRecord, StateStore


def make_record(slug="demo", **kw) -> ProjectRecord:
    base = dict(
        slug=slug,
        source_path="/srv/demo",
        compose_file="/srv/demo/compose.yml",
        override_file="/home/u/.portal/projects/demo/compose.override.yml",
        service="app",
        container_name=f"portal-{slug}-app",
        port=8080,
        fqdn=f"{slug}.example.com",
        tunnel_id="tun-123",
        zone_id="zone-123",
        dns_record_id="rec-123",
        network=f"portal-{slug}",
        cloudflared_container=f"portal-{slug}-cloudflared",
    )
    base.update(kw)
    return ProjectRecord(**base)


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.json")


def test_missing_file_reads_as_empty(store):
    assert store.list() == []
    assert store.get("demo") is None


def test_put_then_get_roundtrips(store):
    store.put(make_record())
    got = store.get("demo")
    assert got is not None
    assert got.tunnel_id == "tun-123"
    assert got.port == 8080
    assert got.deployed_at  # stamped automatically


def test_put_creates_parent_directory(tmp_path):
    store = StateStore(tmp_path / "nested" / "deep" / "state.json")
    store.put(make_record())
    assert store.get("demo") is not None


def test_put_is_upsert_not_append(store):
    store.put(make_record())
    store.put(make_record(port=9090))
    assert len(store.list()) == 1
    assert store.get("demo").port == 9090


def test_multiple_slugs_coexist(store):
    store.put(make_record("a"))
    store.put(make_record("b"))
    assert sorted(r.slug for r in store.list()) == ["a", "b"]


def test_delete_removes_only_that_entry(store):
    store.put(make_record("a"))
    store.put(make_record("b"))
    assert store.delete("a") is True
    assert [r.slug for r in store.list()] == ["b"]
    assert store.delete("a") is False


def test_require_raises_for_unknown_slug(store):
    with pytest.raises(StateError, match="No deployment named 'ghost'"):
        store.require("ghost")


def test_corrupt_file_raises_clear_error(store):
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="Could not read state file"):
        store.list()


def test_unknown_fields_in_file_are_ignored(store):
    store.put(make_record())
    data = json.loads(store.path.read_text())
    data["projects"]["demo"]["from_a_future_version"] = True
    store.path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get("demo").slug == "demo"


def test_write_leaves_no_temp_files_behind(store):
    store.put(make_record())
    leftovers = [p for p in store.path.parent.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []


def test_failed_write_leaves_previous_file_intact(store, monkeypatch):
    """The temp-file-then-rename dance means a crash mid-write cannot truncate
    the existing state file."""
    store.put(make_record(port=8080))
    original = store.path.read_text()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.put(make_record(port=9999))

    assert store.path.read_text() == original
    leftovers = [p for p in store.path.parent.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []


def test_no_secret_fields_on_the_record():
    """state.json must never hold the API token or a tunnel token."""
    fields = set(ProjectRecord.__dataclass_fields__)
    assert not {f for f in fields if "token" in f or "secret" in f}


def test_docker_host_defaults_to_local(store):
    store.put(make_record())
    assert store.get("demo").docker_host == ""


def test_docker_host_roundtrips_for_remote_deploys(store):
    store.put(make_record(docker_host="ssh://user@1.2.3.4"))
    assert store.get("demo").docker_host == "ssh://user@1.2.3.4"


def test_stale_lock_is_broken(tmp_path):
    store = StateStore(tmp_path / "state.json")
    lock = tmp_path / "state.json.lock"
    lock.write_text("")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    store.put(make_record())
    assert store.get("demo") is not None
    assert not lock.exists()


def test_fresh_lock_times_out(tmp_path, monkeypatch):
    from portal.state import store as store_mod

    monkeypatch.setattr(store_mod, "LOCK_TIMEOUT", 0.1)
    store = StateStore(tmp_path / "state.json")
    (tmp_path / "state.json.lock").write_text("")
    with pytest.raises(StateError, match="Timed out"):
        store.put(make_record())
