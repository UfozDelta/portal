"""~/.portal layout: where generated compose files and deployment state live.

Credentials are not handled here — see settings.py. Nothing in this module
ever touches a secret.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def portal_home() -> Path:
    """The ~/.portal directory. Overridable with PORTAL_HOME (used by tests)."""
    override = os.environ.get("PORTAL_HOME")
    return Path(override) if override else Path.home() / ".portal"


def state_path() -> Path:
    return portal_home() / "state.json"


def project_dir(slug: str) -> Path:
    """Where portal keeps generated files (override, synthesized compose) for a slug."""
    d = portal_home() / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Project directory name -> a docker/DNS-safe slug."""
    slug = _SLUG_STRIP.sub("-", name.lower()).strip("-")
    return slug or "project"
