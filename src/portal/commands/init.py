"""portal init — verify the token, cache the account id, make ~/.portal."""

from __future__ import annotations

import click

from ..cloudflare.client import CloudflareClient
from ..config import (
    ENV_TOKEN,
    credentials_path,
    ensure_home,
    portal_home,
    read_token,
    save_account_id,
    write_token,
)
from ..errors import ConfigError


def run(token_arg: str | None = None) -> None:
    home = ensure_home()
    click.echo(f"portal home: {home}")

    if token_arg:
        path = write_token(token_arg)
        click.echo(f"wrote credentials to {path}")

    try:
        token = read_token()
    except ConfigError:
        raise ConfigError(
            f"No Cloudflare API token found.\n"
            f"  Either export {ENV_TOKEN}=... and re-run `portal init`,\n"
            f"  or run `portal init --token <token>` to write "
            f"{credentials_path()}.\n"
            f"  The token needs: Account > Cloudflare Tunnel > Edit, "
            f"Zone > DNS > Edit, Zone > Zone > Read."
        ) from None

    client = CloudflareClient(token)
    verified = client.verify_token()
    click.echo(f"token: {verified.get('status', 'unknown')}")

    account_id = client.resolve_account_id()
    save_account_id(account_id)
    click.echo(f"account id: {account_id} (cached in {portal_home() / 'account.json'})")
    click.echo("portal is ready. Next: portal inspect <path>")
