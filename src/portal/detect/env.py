"""Compare .env.example against .env.

Only KEY NAMES are ever read out of these files, plus whether each has a
value at all. The values themselves are never returned, logged or stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1].strip()
    return value


def parse_entries(path: Path) -> dict[str, bool]:
    """Key name -> whether it has a non-empty value, in file order. The
    values themselves are discarded."""
    entries: dict[str, bool] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entries

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and key not in entries:
            entries[key] = bool(_unquote(value))
    return entries


def parse_keys(path: Path) -> list[str]:
    """Key names from a dotenv-style file, in order. Values are discarded."""
    return list(parse_entries(path))


@dataclass
class EnvReport:
    has_example: bool
    has_env: bool
    missing_keys: list[str]
    # Keys that are set in .env but blank, although .env.example gives them a
    # value. A blank in .env.example itself marks the key as optional.
    empty_keys: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        """Every key that is missing or blank, missing ones first."""
        return [*self.missing_keys, *self.empty_keys]

    def describe(self) -> str:
        parts = []
        if self.missing_keys:
            parts.append(f"missing: {', '.join(self.missing_keys)}")
        if self.empty_keys:
            parts.append(f"empty: {', '.join(self.empty_keys)}")
        return "; ".join(parts)


def check_env(project_path: Path) -> EnvReport:
    """Keys present in .env.example but absent from, or blank in, .env."""
    example = project_path / ".env.example"
    env = project_path / ".env"
    has_example, has_env = example.is_file(), env.is_file()

    if not has_example:
        return EnvReport(has_example=False, has_env=has_env, missing_keys=[])

    expected = parse_entries(example)
    present = parse_entries(env) if has_env else {}
    return EnvReport(
        has_example=True,
        has_env=has_env,
        missing_keys=[k for k in expected if k not in present],
        empty_keys=[
            k for k, has_default in expected.items()
            if has_default and k in present and not present[k]
        ],
    )
