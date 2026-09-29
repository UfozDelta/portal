"""portal's own config: where CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID
come from. The only source is a `.env` file (or the real environment) —
never a separate credentials file, never an interactive setup step.

This is portal's *own* .env, not the target project's. The target project's
.env is never read (see detect/env.py) — only checked for which key names
are present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

from .errors import ConfigError

TOKEN_KEY = "CLOUDFLARE_API_TOKEN"
ACCOUNT_KEY = "CLOUDFLARE_ACCOUNT_ID"


@dataclass(frozen=True)
class Settings:
    token: str
    account_id: str | None


def _env_file_path(env_file: str | Path | None) -> Path | None:
    if env_file is not None:
        return Path(env_file)
    override = os.environ.get("PORTAL_ENV_FILE")
    if override:
        return Path(override)
    found = find_dotenv(usecwd=True)
    return Path(found) if found else None


def load_settings(env_file: str | Path | None = None) -> Settings:
    """CLOUDFLARE_API_TOKEN (required) and CLOUDFLARE_ACCOUNT_ID (optional).

    Checked in order: the real environment, then the resolved `.env` file.
    Nothing is cached to disk and nothing is written back.
    """
    path = _env_file_path(env_file)
    values = dotenv_values(path) if path and path.is_file() else {}

    token = (os.environ.get(TOKEN_KEY) or values.get(TOKEN_KEY) or "").strip()
    if not token:
        where = f" (looked in {path})" if path else " (no .env file found)"
        raise ConfigError(
            f"No Cloudflare API token found{where}.\n"
            f"  Set {TOKEN_KEY}=... in a .env file, or export it in the "
            f"environment.\n"
            f"  The token needs: Account > Cloudflare Tunnel > Edit, "
            f"Zone > DNS > Edit, Zone > Zone > Read."
        )

    account_id = (os.environ.get(ACCOUNT_KEY) or values.get(ACCOUNT_KEY) or "").strip()
    return Settings(token=token, account_id=account_id or None)
