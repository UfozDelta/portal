"""portal status — report only. Never fixes anything."""

from __future__ import annotations

import click
import requests

from ..runtime import docker
from ..state import ProjectRecord, StateStore


def _probe(fqdn: str) -> str:
    """A single HTTPS GET at the hostname, reported as-is."""
    try:
        resp = requests.get(f"https://{fqdn}", timeout=10, allow_redirects=False)
        return f"HTTP {resp.status_code}"
    except requests.RequestException as exc:
        return f"unreachable ({type(exc).__name__})"


def _render(record: ProjectRecord, *, probe: bool) -> None:
    click.echo(f"{record.slug}")
    click.echo(f"  source      {record.source_path}")
    click.echo(f"  hostname    https://{record.fqdn}")
    click.echo(f"  tunnel      {record.tunnel_id}")
    click.echo(f"  deployed    {record.deployed_at}")
    click.echo(f"  app         {record.container_name}: {docker.container_status(record.container_name)}")
    click.echo(
        f"  cloudflared {record.cloudflared_container}: "
        f"{docker.container_status(record.cloudflared_container)}"
    )
    if probe:
        click.echo(f"  probe       {_probe(record.fqdn)}")


def run(slug: str | None, *, probe: bool = True) -> None:
    store = StateStore()
    records = [store.require(slug)] if slug else store.list()

    if not records:
        click.echo("portal has no deployments recorded. Try `portal deploy <path> --host <fqdn>`.")
        return

    for index, record in enumerate(sorted(records, key=lambda r: r.slug)):
        if index:
            click.echo("")
        _render(record, probe=probe)
