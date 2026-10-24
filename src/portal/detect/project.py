"""Orchestrates detection into a ProjectPlan. Side-effect free: nothing here
writes a file, starts a container or calls an API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import resource_prefix, slugify
from ..errors import DetectionError
from . import compose as compose_mod
from . import dockerfile as dockerfile_mod
from .env import EnvReport, check_env

DOCKERFILE_NAME = "Dockerfile"


@dataclass
class ProjectPlan:
    slug: str
    source_path: Path
    kind: str  # "compose" | "dockerfile"
    compose_file: Path | None  # None when it has to be synthesized
    service: str
    port: int
    port_source: str  # "flag" | "compose" | "dockerfile"
    dockerfile: Path | None
    env: EnvReport
    services_found: list[str] = field(default_factory=list)
    # Networks the service already belongs to. The override must ADD the portal
    # network to these, not replace them, or a service loses its link to the
    # rest of its own stack (its database, say).
    service_networks: list[str] = field(default_factory=list)
    fqdn: str | None = None

    @property
    def container_name(self) -> str:
        return f"{resource_prefix(self.slug)}-{self.service}"

    @property
    def network(self) -> str:
        return resource_prefix(self.slug)

    @property
    def compose_project(self) -> str:
        return resource_prefix(self.slug)

    @property
    def cloudflared_container(self) -> str:
        return f"{resource_prefix(self.slug)}-cloudflared"

    @property
    def origin_url(self) -> str:
        """What cloudflared points at: container name over the shared network."""
        return f"http://{self.container_name}:{self.port}"

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "source_path": str(self.source_path),
            "kind": self.kind,
            "compose_file": str(self.compose_file) if self.compose_file else None,
            "service": self.service,
            "port": self.port,
            "port_source": self.port_source,
            "dockerfile": str(self.dockerfile) if self.dockerfile else None,
            "container_name": self.container_name,
            "network": self.network,
            "origin_url": self.origin_url,
            "fqdn": self.fqdn,
            "services_found": self.services_found,
            "env": {
                "has_example": self.env.has_example,
                "has_env": self.env.has_env,
                "missing_keys": self.env.missing_keys,
                "empty_keys": self.env.empty_keys,
            },
        }


def detect(
    path: str | Path,
    *,
    port: int | None = None,
    service: str | None = None,
    fqdn: str | None = None,
    name: str | None = None,
) -> ProjectPlan:
    """Inspect a project directory and decide what portal would deploy.

    `name` overrides the slug, which otherwise comes from the directory name —
    needed when two projects live in directories with the same name.
    """
    project_path = Path(path).expanduser().resolve()
    if not project_path.is_dir():
        raise DetectionError(f"{project_path} is not a directory.")

    slug = slugify(name or project_path.name)
    env_report = check_env(project_path)
    dockerfile = project_path / DOCKERFILE_NAME
    dockerfile = dockerfile if dockerfile.is_file() else None
    compose_file = compose_mod.find_compose_file(project_path)

    if compose_file is not None:
        data = compose_mod.load_compose(compose_file)
        choice = compose_mod.pick_service(data, service)
        network_mode = choice.spec.get("network_mode")
        if network_mode:
            raise DetectionError(
                f"Service '{choice.name}' uses network_mode: {network_mode}. portal "
                f"reaches the app by attaching it to a private network, which "
                f"compose does not allow alongside network_mode. Remove "
                f"network_mode, or pick another service with service=."
            )
        resolved_port, source = _resolve_port(
            flag_port=port,
            compose_ports=compose_mod.service_ports(choice.spec),
            dockerfile=_service_dockerfile(choice.spec, project_path, dockerfile),
        )
        return ProjectPlan(
            slug=slug,
            source_path=project_path,
            kind="compose",
            compose_file=compose_file,
            service=choice.name,
            port=resolved_port,
            port_source=source,
            dockerfile=dockerfile,
            env=env_report,
            services_found=choice.candidates,
            service_networks=compose_mod.service_networks(choice.spec),
            fqdn=fqdn,
        )

    if dockerfile is None:
        raise DetectionError(
            f"{project_path} has neither a Dockerfile nor a compose file. "
            f"portal needs one of them to know what to deploy."
        )

    if service and service != "app":
        raise DetectionError(
            "service= only applies to compose projects; this project is a bare "
            "Dockerfile, whose synthesized service is always named 'app'."
        )

    resolved_port, source = _resolve_port(
        flag_port=port, compose_ports=[], dockerfile=dockerfile
    )
    return ProjectPlan(
        slug=slug,
        source_path=project_path,
        kind="dockerfile",
        compose_file=None,
        service="app",
        port=resolved_port,
        port_source=source,
        dockerfile=dockerfile,
        env=env_report,
        services_found=["app"],
        service_networks=["default"],
        fqdn=fqdn,
    )


def _service_dockerfile(
    spec: dict, project_path: Path, fallback: Path | None
) -> Path | None:
    """The Dockerfile a compose service builds from, so EXPOSE can be read."""
    context = compose_mod.build_context(spec, project_path)
    if context is None:
        return fallback
    build = spec.get("build")
    name = DOCKERFILE_NAME
    if isinstance(build, dict) and build.get("dockerfile"):
        name = str(build["dockerfile"])
    candidate = context / name
    return candidate if candidate.is_file() else fallback


def _resolve_port(
    *, flag_port: int | None, compose_ports: list[int], dockerfile: Path | None
) -> tuple[int, str]:
    """port= > compose expose/ports > Dockerfile EXPOSE > a clear error."""
    if flag_port is not None:
        return flag_port, "flag"
    if compose_ports:
        return compose_ports[0], "compose"
    if dockerfile is not None:
        exposed = dockerfile_mod.parse_expose(dockerfile)
        if exposed:
            return exposed[0], "dockerfile"
    raise DetectionError(
        "Could not determine which port the app listens on. Nothing declares it "
        "(no compose expose:/ports:, no Dockerfile EXPOSE). Pass port=N."
    )
