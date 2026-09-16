"""portal inspect — side-effect-free detection report."""

from __future__ import annotations

import json

import click

from ..detect import ProjectPlan, detect


def render(plan: ProjectPlan) -> None:
    click.echo(f"project   {plan.slug}   ({plan.source_path})")
    click.echo(f"kind      {plan.kind}")
    click.echo(
        f"compose   {plan.compose_file if plan.compose_file else '(none - portal would synthesize one)'}"
    )
    if len(plan.services_found) > 1:
        click.echo(f"services  {', '.join(plan.services_found)}")
    click.echo(f"service   {plan.service}")
    click.echo(f"port      {plan.port}  (from {plan.port_source})")
    click.echo(f"container {plan.container_name}")
    click.echo(f"network   {plan.network}")
    click.echo(f"origin    {plan.origin_url}")
    if plan.fqdn:
        click.echo(f"hostname  {plan.fqdn}")

    env = plan.env
    if not env.has_example:
        click.echo("env       no .env.example - nothing to check")
    elif env.missing_keys:
        click.echo(
            f"env       {len(env.missing_keys)} key(s) in .env.example missing "
            f"from .env{'' if env.has_env else ' (.env does not exist)'}:"
        )
        for key in env.missing_keys:
            click.echo(f"            - {key}")
    else:
        click.echo("env       .env has every key from .env.example")


def run(path: str, *, port: int | None, service: str | None, as_json: bool) -> None:
    plan = detect(path, port=port, service=service)
    if as_json:
        click.echo(json.dumps(plan.to_dict(), indent=2))
    else:
        render(plan)
