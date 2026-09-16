"""Entry point. Parses arguments and dispatches; all real work is in commands/."""

from __future__ import annotations

import sys

import click

from . import __version__
from .commands import deploy as deploy_cmd
from .commands import destroy as destroy_cmd
from .commands import init as init_cmd
from .commands import inspect as inspect_cmd
from .commands import status as status_cmd
from .errors import PortalError


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="portal")
def cli() -> None:
    """Deploy a dockerized project behind its own Cloudflare Tunnel."""


@cli.command("init")
@click.option(
    "--token",
    default=None,
    help="Cloudflare API token to save to ~/.portal/credentials.toml. "
    "Omit to use the CLOUDFLARE_API_TOKEN env var instead.",
)
def init(token: str | None) -> None:
    """Verify the Cloudflare token and set up ~/.portal."""
    init_cmd.run(token)


@cli.command("inspect")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--port", type=int, default=None, help="Override the detected port.")
@click.option("--service", default=None, help="Compose service to expose.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def inspect(path: str, port: int | None, service: str | None, as_json: bool) -> None:
    """Report what portal would deploy for PATH. Changes nothing."""
    inspect_cmd.run(path, port=port, service=service, as_json=as_json)


@cli.command("deploy")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--host", required=True, help="Public hostname, e.g. app.example.com")
@click.option("--port", type=int, default=None, help="Override the detected port.")
@click.option("--service", default=None, help="Compose service to expose.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def deploy(
    path: str, host: str, port: int | None, service: str | None, yes: bool
) -> None:
    """Deploy PATH and publish it at --host."""
    deploy_cmd.run(path, host=host, port=port, service=service, yes=yes)


@cli.command("destroy")
@click.argument("slug")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--volumes", is_flag=True, help="Also remove the stack's volumes.")
def destroy(slug: str, yes: bool, volumes: bool) -> None:
    """Tear down the deployment named SLUG."""
    destroy_cmd.run(slug, yes=yes, volumes=volumes)


@cli.command("status")
@click.argument("slug", required=False)
@click.option("--no-probe", is_flag=True, help="Skip the HTTPS reachability check.")
def status(slug: str | None, no_probe: bool) -> None:
    """Show what portal has deployed, and whether it is up."""
    status_cmd.run(slug, probe=not no_probe)


def main() -> int:
    """Console-script entry point. PortalError prints as a message, not a traceback."""
    try:
        cli.main(standalone_mode=False)
    except PortalError as exc:
        click.echo(f"error: {exc}", err=True)
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("aborted", err=True)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
