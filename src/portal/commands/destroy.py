"""portal destroy — unwind a deploy in reverse order.

DNS first so the hostname stops resolving before the backend disappears, then
the tunnel, then the containers, then the state entry.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..cloudflare import dns as dns_mod
from ..cloudflare import tunnels as tunnel_mod
from ..cloudflare.client import CloudflareClient
from ..config import read_token, require_account_id
from ..errors import PortalError
from ..runtime import docker
from ..state import StateStore


def run(slug: str, *, yes: bool, volumes: bool) -> None:
    store = StateStore()
    record = store.require(slug)

    click.echo(f"about to destroy '{slug}':")
    click.echo(f"  hostname    {record.fqdn}")
    click.echo(f"  tunnel      {record.tunnel_id}")
    click.echo(f"  containers  {record.container_name}, {record.cloudflared_container}")
    click.echo(f"  volumes     {'REMOVED' if volumes else 'preserved'}")

    if not yes:
        typed = click.prompt("type the slug to confirm", default="", show_default=False)
        if typed.strip() != slug:
            raise PortalError("Aborted: the slug did not match.")

    # DNS first.
    try:
        removed = dns_mod.delete_record(
            CloudflareClient(read_token()), record.zone_id, record.dns_record_id, slug
        )
        click.echo(
            f"==> DNS record {'deleted' if removed else 'already gone'}"
        )
    except PortalError as exc:
        # A missing token or a refused delete must not strand the containers:
        # report it and carry on to the local teardown.
        click.echo(f"!! DNS: {exc}")

    # Then the tunnel — deleting it removes its ingress config too.
    try:
        client = CloudflareClient(read_token())
        tunnel_mod.delete_tunnel(client, require_account_id(), record.tunnel_id)
        click.echo(f"==> tunnel {record.tunnel_id} deleted")
    except PortalError as exc:
        click.echo(f"!! tunnel: {exc}")

    # Then the containers.
    docker.remove_container(record.cloudflared_container)
    click.echo(f"==> {record.cloudflared_container} removed")

    files = [Path(record.compose_file), Path(record.override_file)]
    docker.compose_down(
        project=f"portal-{slug}",
        files=[f for f in files if f.exists()],
        volumes=volumes,
    )
    click.echo(f"==> compose down ({'with' if volumes else 'without'} volumes)")
    docker.remove_network(record.network)

    store.delete(slug)
    click.echo(f"==> state entry for '{slug}' removed")
