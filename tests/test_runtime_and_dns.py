"""Pure-logic coverage for spec invariants: portal never clobbers a DNS record
it does not own, and its compose override actually strips host ports.

The networked halves of cloudflare/ (client.py, tunnels.py) and the subprocess
half of runtime/docker.py are deliberately not covered here — they need a live
API or a live docker daemon, and are out of scope for this phase.
"""

from portal.cloudflare.dns import candidate_zones, is_portal_owned, ownership_comment
from portal.detect import detect
from portal.runtime.override import build_override, synthesize_compose, write_yaml


def test_override_ports_are_tagged_for_replacement_not_merge(fixtures_dir, tmp_path):
    """Compose's default merge rule for a list field like `ports` is to
    concatenate with the base file, not replace it — a plain `ports: []` in
    the override would leave a project's own published ports in place. The
    override must carry the `!override` merge tag so compose actually
    replaces the field."""
    plan = detect(fixtures_dir / "compose_multi_service", fqdn="x.example.com")
    doc = build_override(plan)
    path = write_yaml(doc, tmp_path / "override.yml")
    assert "ports: !override []" in path.read_text()


def test_override_strips_ports_and_sets_labels(fixtures_dir):
    plan = detect(fixtures_dir / "compose_multi_service", fqdn="x.example.com")
    doc = build_override(plan)
    svc = doc["services"]["app"]

    assert svc["ports"] == []  # host publication removed
    assert svc["container_name"] == "portal-compose-multi-service-app"
    assert svc["expose"] == ["8080"]
    assert svc["labels"] == {
        "portal.managed": "true",
        "portal.project": "compose-multi-service",
        "portal.service": "app",
        "portal.hostname": "x.example.com",
        "portal.port": "8080",
    }
    assert doc["networks"]["portal"]["name"] == "portal-compose-multi-service"
    assert doc["networks"]["portal"]["external"] is True
    # portal's network is ADDED, not substituted, so app can still reach db
    assert svc["networks"] == ["default", "portal"]


def test_override_preserves_custom_service_networks(fixtures_dir):
    plan = detect(fixtures_dir / "compose_multi_service")
    plan.service_networks = ["backend", "frontend"]
    assert build_override(plan)["services"]["app"]["networks"] == [
        "backend",
        "frontend",
        "portal",
    ]


def test_override_does_not_duplicate_portal_network(fixtures_dir):
    plan = detect(fixtures_dir / "compose_multi_service")
    plan.service_networks = ["portal", "default"]
    assert build_override(plan)["services"]["app"]["networks"] == ["default", "portal"]


def test_synthesized_compose_points_at_source_dir(fixtures_dir):
    plan = detect(fixtures_dir / "simple_dockerfile")
    doc = synthesize_compose(plan)
    assert doc["services"]["app"]["build"]["context"] == str(plan.source_path)
    # no .env in the fixture, so no env_file wiring
    assert "env_file" not in doc["services"]["app"]


def test_candidate_zones_longest_suffix_first():
    assert candidate_zones("app.staging.example.com") == [
        "staging.example.com",
        "example.com",
        "com",
    ]


def test_ownership_comment_matching():
    assert is_portal_owned({"comment": ownership_comment("demo")}, "demo")
    assert not is_portal_owned({"comment": "managed by portal:other"}, "demo")
    assert not is_portal_owned({}, "demo")
    assert not is_portal_owned({"comment": None}, "demo")
