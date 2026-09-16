"""Ingress config for a remotely-managed tunnel.

The catch-all rule is a hard requirement: cloudflared rejects a config whose
last rule has a hostname, and rules are evaluated top to bottom, so the
catch-all must be last and must appear exactly once.
"""

from __future__ import annotations

CATCH_ALL = {"service": "http_status:404"}


def is_catch_all(rule: dict) -> bool:
    """A rule with no hostname and no path is the catch-all."""
    return not rule.get("hostname") and not rule.get("path")


def build_ingress(hostname: str, origin_url: str) -> list[dict]:
    """One hostname rule, then the mandatory catch-all, in that order."""
    if not hostname:
        raise ValueError("hostname is required to build an ingress rule")
    if not origin_url:
        raise ValueError("origin_url is required to build an ingress rule")
    return [
        {"hostname": hostname, "service": origin_url},
        dict(CATCH_ALL),
    ]


def build_config(hostname: str, origin_url: str) -> dict:
    """The body for PUT /cfd_tunnel/{id}/configurations."""
    return {"config": {"ingress": build_ingress(hostname, origin_url)}}


def empty_config() -> dict:
    """Ingress stripped back to nothing but the catch-all — used when tearing a
    tunnel's routing down without deleting the tunnel."""
    return {"config": {"ingress": [dict(CATCH_ALL)]}}


def validate_ingress(rules: list[dict]) -> None:
    """Guard the invariants before anything is sent to Cloudflare."""
    if not rules:
        raise ValueError("ingress must contain at least the catch-all rule")
    if not is_catch_all(rules[-1]):
        raise ValueError("the last ingress rule must be the catch-all rule")
    for index, rule in enumerate(rules[:-1]):
        if is_catch_all(rule):
            raise ValueError(
                f"catch-all rule at position {index} would shadow every rule "
                f"after it; it must be last"
            )
        if not rule.get("service"):
            raise ValueError(f"ingress rule at position {index} has no service")


def put_config(client, account_id: str, tunnel_id: str, config: dict) -> dict:
    validate_ingress(config["config"]["ingress"])
    return client.put(
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", json=config
    )["result"]
