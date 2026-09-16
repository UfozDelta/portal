"""Pull EXPOSE ports out of a Dockerfile."""

from __future__ import annotations

import re
from pathlib import Path

# EXPOSE 8080 / EXPOSE 8080/tcp / EXPOSE 8080 9090
_EXPOSE = re.compile(r"^\s*EXPOSE\s+(.+?)\s*$", re.IGNORECASE)


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
            number = token.split("/", 1)[0]
            if number.isdigit():
                value = int(number)
                if value not in ports:
                    ports.append(value)
    return ports
