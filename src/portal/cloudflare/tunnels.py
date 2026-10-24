"""Create / fetch-token / delete a per-project Cloudflare Tunnel.

One tunnel per project. Tunnels are created with config_src="cloudflare" so the
ingress config lives in Cloudflare and can be set over the API — a locally
managed tunnel would need a config file inside the cloudflared container.
"""

from __future__ import annotations

from ..config import resource_prefix
from ..errors import CloudflareError


def tunnel_name(slug: str) -> str:
    return resource_prefix(slug)


def create_tunnel(client, account_id: str, slug: str) -> dict:
    result = client.post(
        f"/accounts/{account_id}/cfd_tunnel",
        json={"name": tunnel_name(slug), "config_src": "cloudflare"},
    )["result"]
    if not result.get("id"):
        raise CloudflareError("Cloudflare created a tunnel but returned no id.")
    return result


def find_tunnel(client, account_id: str, slug: str) -> dict | None:
    """An existing, not-yet-deleted tunnel with this project's name."""
    results = client.get(
        f"/accounts/{account_id}/cfd_tunnel",
        params={"name": tunnel_name(slug), "is_deleted": "false"},
    )["result"]
    return results[0] if results else None


def get_tunnel_token(client, account_id: str, tunnel_id: str) -> str:
    """The tunnel run token. This is a credential — never log or store it."""
    token = client.get(f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")["result"]
    if not isinstance(token, str) or not token:
        raise CloudflareError("Cloudflare returned no usable tunnel token.")
    return token


def delete_tunnel(client, account_id: str, tunnel_id: str) -> bool:
    """Deleting the tunnel takes its ingress config with it — for one tunnel per
    project that is simpler than unwinding the config rule by rule.

    Cloudflare refuses to delete a tunnel that still has active connections,
    and a connector that was just stopped can linger as "connected" for a
    while. So stale connections are cleaned up first (best effort), and the
    caller is expected to have stopped cloudflared before calling this.

    Returns False if the tunnel was already gone.
    """
    base = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"
    try:
        client.delete(f"{base}/connections")
    except CloudflareError as exc:
        if exc.status_code == 404:
            return False
        # not fatal on its own — the delete below reports the real problem
    try:
        client.delete(base)
    except CloudflareError as exc:
        if exc.status_code == 404:
            return False
        raise
    return True
