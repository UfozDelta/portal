"""_build_env is the whole of SSH-remote support: it decides whether docker
subprocess calls inherit the parent environment untouched (the local case) or
get DOCKER_HOST pointed at a remote daemon. No live docker daemon needed to
verify the env dict it builds."""

import os

from portal.runtime.docker import _build_env


def test_no_docker_host_returns_none():
    """None means subprocess.run inherits the parent env as-is — the common
    local-docker case shouldn't pay for a dict copy."""
    assert _build_env(None) is None


def test_docker_host_sets_env_var_and_keeps_the_rest(monkeypatch):
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")
    env = _build_env("ssh://user@1.2.3.4")
    assert env["DOCKER_HOST"] == "ssh://user@1.2.3.4"
    assert env["SOME_UNRELATED_VAR"] == "keep-me"


def test_extra_overrides_are_merged_in():
    env = _build_env(None, {"TUNNEL_TOKEN": "secret"})
    assert env["TUNNEL_TOKEN"] == "secret"
    assert env["PATH"] == os.environ["PATH"]


def test_docker_host_and_extra_together():
    env = _build_env("ssh://user@1.2.3.4", {"TUNNEL_TOKEN": "secret"})
    assert env["DOCKER_HOST"] == "ssh://user@1.2.3.4"
    assert env["TUNNEL_TOKEN"] == "secret"
