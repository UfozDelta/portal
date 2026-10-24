"""The catch-all invariant is the easiest thing here to get subtly wrong:
cloudflared rejects a config whose last rule has a hostname, and rules are
evaluated in order, so a catch-all anywhere but last swallows everything.
"""

import pytest

from portal.cloudflare.ingress import (
    CATCH_ALL,
    build_config,
    build_ingress,
    is_catch_all,
    validate_ingress,
)

HOST = "app.example.com"
ORIGIN = "http://portal-demo-app:8080"


def test_ingress_has_exactly_two_rules():
    assert len(build_ingress(HOST, ORIGIN)) == 2


def test_hostname_rule_comes_first():
    rules = build_ingress(HOST, ORIGIN)
    assert rules[0] == {"hostname": HOST, "service": ORIGIN}


def test_catch_all_is_last():
    rules = build_ingress(HOST, ORIGIN)
    assert rules[-1] == {"service": "http_status:404"}
    assert is_catch_all(rules[-1])
    assert not is_catch_all(rules[0])


def test_exactly_one_catch_all():
    rules = build_ingress(HOST, ORIGIN)
    assert sum(1 for r in rules if is_catch_all(r)) == 1


def test_catch_all_is_copied_not_shared():
    a = build_ingress(HOST, ORIGIN)[-1]
    b = build_ingress("other.example.com", ORIGIN)[-1]
    a["service"] = "mutated"
    assert b["service"] == "http_status:404"
    assert CATCH_ALL["service"] == "http_status:404"


def test_build_config_shape():
    config = build_config(HOST, ORIGIN)
    assert set(config) == {"config"}
    assert config["config"]["ingress"] == build_ingress(HOST, ORIGIN)


def test_build_ingress_requires_hostname_and_origin():
    with pytest.raises(ValueError):
        build_ingress("", ORIGIN)
    with pytest.raises(ValueError):
        build_ingress(HOST, "")


def test_validate_accepts_a_good_config():
    validate_ingress(build_ingress(HOST, ORIGIN))


def test_validate_rejects_empty():
    with pytest.raises(ValueError, match="at least the catch-all"):
        validate_ingress([])


def test_validate_rejects_missing_catch_all():
    with pytest.raises(ValueError, match="last ingress rule must be the catch-all"):
        validate_ingress([{"hostname": HOST, "service": ORIGIN}])


def test_validate_rejects_catch_all_that_is_not_last():
    rules = [dict(CATCH_ALL), {"hostname": HOST, "service": ORIGIN}, dict(CATCH_ALL)]
    with pytest.raises(ValueError, match="position 0"):
        validate_ingress(rules)


def test_validate_rejects_rule_without_service():
    with pytest.raises(ValueError, match="no service"):
        validate_ingress([{"hostname": HOST}, dict(CATCH_ALL)])
