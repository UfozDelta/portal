"""~/.portal layout, credential resolution, cached account id.

Never writes the API token into state.json and never logs it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

ENV_TOKEN = "CLOUDFLARE_API_TOKEN"


def portal_home() -> Path:
    """The ~/.portal directory. Overridable with PORTAL_HOME (used by tests)."""
    override = os.environ.get("PORTAL_HOME")
    return Path(override) if override else Path.home() / ".portal"


def ensure_home() -> Path:
    home = portal_home()
    (home / "projects").mkdir(parents=True, exist_ok=True)
    return home


def state_path() -> Path:
    return portal_home() / "state.json"


def credentials_path() -> Path:
    return portal_home() / "credentials.toml"


def account_cache_path() -> Path:
    return portal_home() / "account.json"


def project_dir(slug: str) -> Path:
    """Where portal keeps generated files (override, synthesized compose) for a slug."""
    d = portal_home() / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chmod_600(path: Path) -> None:
    """Best-effort restrictive permissions. POSIX only; on Windows ACLs are not
    handled yet (documented in the README)."""
    if os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def read_token() -> str:
    """CLOUDFLARE_API_TOKEN env var first, then ~/.portal/credentials.toml."""
    env = os.environ.get(ENV_TOKEN, "").strip()
    if env:
        return env

    path = credentials_path()
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Could not parse {path}: {exc}") from None
        token = str(data.get("cloudflare", {}).get("api_token", "")).strip()
        if token:
            return token

    raise ConfigError(
        f"No Cloudflare API token found.\n"
        f"  Set {ENV_TOKEN}=... in the environment, or write {path}:\n\n"
        f"    [cloudflare]\n"
        f'    api_token = "..."\n'
    )


def write_token(token: str) -> Path:
    """Persist the token to credentials.toml with 0600 where supported."""
    ensure_home()
    path = credentials_path()
    path.write_text(
        "# written by portal init. Treat this file as a secret.\n"
        "[cloudflare]\n"
        f'api_token = "{token}"\n',
        encoding="utf-8",
    )
    _chmod_600(path)
    return path


def save_account_id(account_id: str) -> None:
    ensure_home()
    account_cache_path().write_text(
        json.dumps({"account_id": account_id}, indent=2), encoding="utf-8"
    )


def load_account_id() -> str | None:
    path = account_cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("account_id") or None
    except (OSError, json.JSONDecodeError):
        return None


def require_account_id() -> str:
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip() or load_account_id()
    if not acct:
        raise ConfigError("No Cloudflare account id cached. Run `portal init` first.")
    return acct


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Project directory name -> a docker/DNS-safe slug."""
    slug = _SLUG_STRIP.sub("-", name.lower()).strip("-")
    return slug or "project"


@dataclass(frozen=True)
class Paths:
    """Resolved locations for one project slug."""

    slug: str
    home: Path
    work: Path

    @classmethod
    def for_slug(cls, slug: str) -> "Paths":
        return cls(slug=slug, home=ensure_home(), work=project_dir(slug))
