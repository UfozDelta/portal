"""Pull EXPOSE ports out of a Dockerfile."""

from __future__ import annotations

import re
from pathlib import Path

# EXPOSE 8080 / EXPOSE 8080/tcp / EXPOSE 8080 9090
_EXPOSE = re.compile(r"^\s*EXPOSE\s+(.+?)\s*$", re.IGNORECASE)


def parse_port(token) -> int | None:
    """One port token as Docker and compose write it: "8080", "8080/tcp", or a
    range "8000-8010" (the low end is taken). None for anything else, such as
    an unexpanded "$PORT"."""
    text = str(token).split("/", 1)[0].split("-", 1)[0]
    return int(text) if text.isdigit() else None


def parse_expose(path: Path) -> list[int]:
    """Every port named by an EXPOSE line, in file order, deduped.

    Build args (`EXPOSE $PORT`) are skipped rather than guessed at.
    """
    ports: list[int] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ports

    for line in text.splitlines():
        match = _EXPOSE.match(line)
        if not match:
            continue
        for token in match.group(1).split():
            value = parse_port(token)
            if value is not None and value not in ports:
                ports.append(value)
    return ports
