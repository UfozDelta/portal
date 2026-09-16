import pytest
import yaml

from portal.detect import detect
from portal.detect.compose import (
    find_compose_file,
    load_compose,
    pick_service,
    service_networks,
    service_ports,
)
from portal.errors import DetectionError


def _compose(text: str) -> dict:
    return yaml.safe_load(text)


def test_find_compose_file_prefers_compose_yml(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {a: {image: x}}")
    (tmp_path / "compose.yml").write_text("services: {a: {image: x}}")
    assert find_compose_file(tmp_path).name == "compose.yml"


def test_single_service_is_trivial(fixtures_dir):
    plan = detect(fixtures_dir / "compose_single_service")
    assert plan.kind == "compose"
    assert plan.service == "web"
    # expose: 3000 in compose wins over the Dockerfile's EXPOSE 9999
    assert (plan.port, plan.port_source) == (3000, "compose")


def test_multi_service_picks_app_over_infra(fixtures_dir):
    plan = detect(fixtures_dir / "compose_multi_service")
    assert plan.service == "app"
    # container side of "127.0.0.1:8081:8080"
    assert (plan.port, plan.port_source) == (8080, "compose")
    assert "db" not in plan.services_found and "cache" not in plan.services_found


def test_explicit_service_flag_wins_even_for_infra(fixtures_dir):
    plan = detect(fixtures_dir / "compose_multi_service", service="db", port=5432)
    assert plan.service == "db"


def test_unknown_service_flag_errors(fixtures_dir):
    with pytest.raises(DetectionError, match="No service named 'nope'"):
        detect(fixtures_dir / "compose_multi_service", service="nope")


def test_all_infra_services_is_an_error():
    data = _compose(
        "services:\n  db: {image: postgres:16}\n  cache: {image: redis:7}\n"
    )
    with pytest.raises(DetectionError, match="looks like infrastructure"):
        pick_service(data)


def test_build_plus_web_name_beats_plain_build():
    data = _compose(
        "services:\n"
        "  worker: {build: ./worker}\n"
        "  api: {build: ./api}\n"
        "  db: {image: postgres:16}\n"
    )
    assert pick_service(data).name == "api"


def test_ambiguous_services_raise_with_candidates():
    data = _compose(
        "services:\n  api: {build: ./a}\n  web: {build: ./w}\n"
    )
    with pytest.raises(DetectionError, match="--service"):
        pick_service(data)


def test_cloudflared_is_treated_as_infra():
    data = _compose(
        "services:\n"
        "  app: {build: .}\n"
        "  cloudflared: {image: cloudflare/cloudflared:latest}\n"
    )
    assert pick_service(data).name == "app"


@pytest.mark.parametrize(
    "spec,expected",
    [
        ({"ports": ["8080"]}, [8080]),
        ({"ports": ["80:8080"]}, [8080]),
        ({"ports": ["127.0.0.1:80:8080/tcp"]}, [8080]),
        ({"ports": [{"target": 8080, "published": 80}]}, [8080]),
        ({"ports": ["8000-8010:9000-9010"]}, [9000]),
        ({"expose": ["3000"], "ports": ["80:8080"]}, [3000, 8080]),
        ({}, []),
    ],
)
def test_service_ports_parsing(spec, expected):
    assert service_ports(spec) == expected


@pytest.mark.parametrize(
    "spec,expected",
    [
        ({}, ["default"]),
        ({"networks": None}, ["default"]),
        ({"networks": []}, ["default"]),
        ({"networks": ["backend"]}, ["backend"]),
        ({"networks": {"backend": None, "front": {"aliases": ["x"]}}}, ["backend", "front"]),
    ],
)
def test_service_networks(spec, expected):
    assert service_networks(spec) == expected


def test_load_compose_rejects_serviceless_file(tmp_path):
    f = tmp_path / "compose.yml"
    f.write_text("version: '3'\n")
    with pytest.raises(DetectionError, match="no services"):
        load_compose(f)
