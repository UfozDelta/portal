"""portal deploy — container up, tunnel created, ingress set, DNS pointed.

Ordering matters: the app container comes up first so cloudflared has something
to reach, the tunnel and its ingress are configured before DNS is pointed at it,
and DNS is last so the hostname never resolves to a tunnel that is not yet
routing.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..cloudflare import dns as dns_mod
from ..cloudflare import ingress as ingress_mod
from ..cloudflare import tunnels as tunnel_mod
from ..cloudflare.client import CloudflareClient
from ..config import Paths, read_token, require_account_id
from ..detect import ProjectPlan, detect
from ..errors import PortalError
from ..runtime import docker, override, scaffold
from ..state import ProjectRecord, StateStore
from .inspect import render


def _compose_files(plan: ProjectPlan, paths: Paths) -> tuple[list[Path], Path]:
    """The -f chain: base compose (project's own, or synthesized) then override."""
    if plan.compose_file is not None:
        base = plan.compose_file
    else:
        base = override.write_yaml(
            override.synthesize_compose(plan), paths.work / "compose.generated.yml"
        )
    override_file = override.write_yaml(
        override.build_override(plan), paths.work / "compose.override.yml"
    )
    return [base, override_file], override_file


def run(
    path: str,
    *,
    host: str,
    port: int | None,
    service: str | None,
    yes: bool,
) -> None:
    plan = detect(path, port=port, service=service, fqdn=host)
    docker.ensure_daemon_running()
    account_id = require_account_id()
    token = read_token()

    click.echo("plan:")
    render(plan)
    click.echo("")
    click.echo(
        "This creates real Cloudflare resources (a tunnel and a DNS record) "
        "and starts containers."
    )
    if not yes and not click.confirm("Proceed?", default=False):
        raise PortalError("Aborted.")

    paths = Paths.for_slug(plan.slug)
    client = CloudflareClient(token)
    store = StateStore()

    # a. container up, on a network cloudflared can reach it over.
    click.echo(f"==> network {plan.network}")
    docker.ensure_network(plan.network)

    files, override_file = _compose_files(plan, paths)
    click.echo(f"==> docker compose up ({plan.compose_project})")
    env_file = plan.source_path / ".env"
    docker.compose_up(
        project=plan.compose_project,
        files=files,
        build=True,
        env_file=env_file if env_file.is_file() else None,
    )

    # b. one dedicated tunnel for this project.
    existing = tunnel_mod.find_tunnel(client, account_id, plan.slug)
    if existing:
        tunnel = existing
        click.echo(f"==> reusing tunnel {tunnel['id']}")
    else:
        tunnel = tunnel_mod.create_tunnel(client, account_id, plan.slug)
        click.echo(f"==> created tunnel {tunnel['id']}")
    tunnel_id = tunnel["id"]
    tunnel_token = tunnel_mod.get_tunnel_token(client, account_id, tunnel_id)

    # c. this project's own cloudflared, on the same network.
    click.echo(f"==> starting {plan.cloudflared_container}")
    docker.run_cloudflared(
        container=plan.cloudflared_container,
        network=plan.network,
        token=tunnel_token,
    )
    del tunnel_token  # nothing below this line needs it

    # d. ingress: the hostname rule, then the mandatory catch-all.
    click.echo(f"==> ingress {host} -> {plan.origin_url}")
    ingress_mod.put_config(
        client,
        account_id,
        tunnel_id,
        ingress_mod.build_config(host, plan.origin_url),
    )

    # e. DNS last.
    zone = dns_mod.resolve_zone(client, host)
    click.echo(f"==> zone {zone['name']} ({zone['id']})")
    record_id = dns_mod.upsert_cname(client, zone["id"], host, tunnel_id, plan.slug)
    click.echo(f"==> CNAME {host} -> {dns_mod.tunnel_target(tunnel_id)} (proxied)")

    # f. deploy scripts into the target project, never overwriting.
    written = scaffold.scaffold_deploy_scripts(plan.source_path)
    click.echo(
        f"==> scaffolded {', '.join(written)}"
        if written
        else "==> deploy scripts already present, left untouched"
    )

    # g. state.
    store.put(
        ProjectRecord(
            slug=plan.slug,
            source_path=str(plan.source_path),
            compose_file=str(files[0]),
            override_file=str(override_file),
            service=plan.service,
            container_name=plan.container_name,
            port=plan.port,
            fqdn=host,
            tunnel_id=tunnel_id,
            zone_id=zone["id"],
            dns_record_id=record_id,
            network=plan.network,
            cloudflared_container=plan.cloudflared_container,
        )
    )

    # h. summary.
    click.echo("")
    click.echo(f"app         {plan.container_name}  {docker.container_status(plan.container_name)}")
    click.echo(
        f"cloudflared {plan.cloudflared_container}  "
        f"{docker.container_status(plan.cloudflared_container)}"
    )
    click.echo(f"tunnel      {tunnel_id}")
    click.echo(f"live at     https://{host}")
    click.echo("")
    click.echo("DNS and the tunnel can take a minute to settle. `portal status` to check.")
