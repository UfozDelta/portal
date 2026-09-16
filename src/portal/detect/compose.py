"""Parse a compose file: pick the web-facing service, extract its port."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..errors import DetectionError

COMPOSE_NAMES = (
    "compose.yml",
    "compose.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
)

# Images that are never the thing we want to expose publicly.
INFRA_IMAGE_TOKENS = (
    "postgres",
    "postgis",
    "mysql",
    "mariadb",
    "mongo",
    "redis",
    "valkey",
    "memcached",
    "rabbitmq",
    "kafka",
    "zookeeper",
    "elasticsearch",
    "opensearch",
    "clickhouse",
    "minio",
    "cloudflared",
    "traefik",
    "nginx-proxy",
    "watchtower",
)

# Service names that read as "this is the app".
WEB_NAME_TOKENS = ("app", "web", "api", "server", "frontend", "backend")


def find_compose_file(project_path: Path) -> Path | None:
    for name in COMPOSE_NAMES:
        candidate = project_path / name
        if candidate.is_file():
            return candidate
    return None


def load_compose(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise DetectionError(f"Could not read {path}: {exc}") from None
    except yaml.YAMLError as exc:
        raise DetectionError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise DetectionError(f"{path} does not look like a compose file.")
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise DetectionError(f"{path} declares no services.")
    return data


def _is_infra(name: str, spec: dict) -> bool:
    image = str(spec.get("image") or "").lower()
    haystack = f"{name.lower()} {image}"
    return any(token in haystack for token in INFRA_IMAGE_TOKENS)


@dataclass
class ServiceChoice:
    name: str
    spec: dict
    candidates: list[str] = field(default_factory=list)


def pick_service(compose: dict, requested: str | None = None) -> ServiceChoice:
    """Choose the web-facing service.

    Order: an explicit --service wins; a single service is trivial; otherwise
    drop known infra, then prefer services that both `build:` and carry a
    web-ish name, then a web-ish name, then anything with `build:`.
    """
    services: dict = compose["services"]

    if requested:
        if requested not in services:
            known = ", ".join(sorted(services))
            raise DetectionError(
                f"No service named '{requested}' in the compose file. Found: {known}"
            )
        return ServiceChoice(requested, services[requested] or {}, sorted(services))

    names = list(services)
    if len(names) == 1:
        return ServiceChoice(names[0], services[names[0]] or {}, names)

    candidates = [n for n in names if not _is_infra(n, services[n] or {})]
    if not candidates:
        raise DetectionError(
            "Every service in the compose file looks like infrastructure "
            "(database, cache, proxy). Name the one to expose with --service."
        )
    if len(candidates) == 1:
        return ServiceChoice(candidates[0], services[candidates[0]] or {}, candidates)

    def web_named(n: str) -> bool:
        return any(t in n.lower() for t in WEB_NAME_TOKENS)

    def has_build(n: str) -> bool:
        return bool((services[n] or {}).get("build"))

    for predicate in (
        lambda n: has_build(n) and web_named(n),
        web_named,
        has_build,
    ):
        matches = [n for n in candidates if predicate(n)]
        if len(matches) == 1:
            return ServiceChoice(matches[0], services[matches[0]] or {}, candidates)
        if len(matches) > 1:
            candidates = matches
            break

    raise DetectionError(
        "Could not tell which service to expose. Candidates: "
        + ", ".join(sorted(candidates))
        + ". Pick one with --service."
    )


def _port_from_ports_entry(entry) -> int | None:
    """Container-side port from a compose `ports:` entry.

    Long form is a mapping with `target`. Short form is a string like
    "8080", "80:8080", "127.0.0.1:80:8080", optionally with "/tcp".
    """
    if isinstance(entry, dict):
        target = entry.get("target")
        return int(target) if str(target).isdigit() else None

    text = str(entry).split("/", 1)[0]
    parts = text.split(":")
    tail = parts[-1]
    if "-" in tail:  # a range like 8000-8010; take the low end
        tail = tail.split("-", 1)[0]
    return int(tail) if tail.isdigit() else None


def service_ports(spec: dict) -> list[int]:
    """Container ports the service declares, `expose:` before `ports:`.

    `expose:` is the stronger signal — it is always the container-side port,
    while `ports:` short form has to be parsed for which side is which.
    """
    found: list[int] = []

    for entry in spec.get("expose") or []:
        text = str(entry).split("/", 1)[0]
        if text.isdigit() and int(text) not in found:
            found.append(int(text))

    for entry in spec.get("ports") or []:
        port = _port_from_ports_entry(entry)
        if port is not None and port not in found:
            found.append(port)

    return found


def service_networks(spec: dict) -> list[str]:
    """Networks the service is already attached to.

    A service that names none is on compose's implicit `default` network, which
    can be referenced explicitly — so that is what we report, and the override
    can then append portal's network without detaching anything.
    """
    declared = spec.get("networks")
    if isinstance(declared, dict):
        return list(declared)
    if isinstance(declared, list) and declared:
        return [str(n) for n in declared]
    return ["default"]


def build_context(spec: dict, project_path: Path) -> Path | None:
    """Directory the service builds from, if it builds at all."""
    build = spec.get("build")
    if not build:
        return None
    if isinstance(build, str):
        return (project_path / build).resolve()
    context = build.get("context", ".") if isinstance(build, dict) else "."
    return (project_path / str(context)).resolve()
