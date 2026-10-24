"""Compare .env.example against .env.

Only KEY NAMES are ever read out of these files. Values are never returned,
logged or stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def parse_keys(path: Path) -> list[str]:
    """Key names from a dotenv-style file, in order. Values are discarded."""
    keys: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, _ = line.partition("=")
        key = key.strip()
        if sep and key and key not in keys:
            keys.append(key)
    return keys


@dataclass
class EnvReport:
    has_example: bool
    has_env: bool
    missing_keys: list[str]


def check_env(project_path: Path) -> EnvReport:
    """Keys present in .env.example but absent from .env."""
    example = project_path / ".env.example"
    env = project_path / ".env"
    has_example, has_env = example.is_file(), env.is_file()

    if not has_example:
        return EnvReport(has_example=False, has_env=has_env, missing_keys=[])

    expected = parse_keys(example)
    present = set(parse_keys(env)) if has_env else set()
    return EnvReport(
        has_example=True,
        has_env=has_env,
        missing_keys=[k for k in expected if k not in present],
    )
