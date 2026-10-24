import pytest

from portal.detect import detect
from portal.detect.dockerfile import parse_expose
from portal.detect.env import check_env, parse_keys
from portal.errors import DetectionError


def test_parse_expose_reads_port(fixtures_dir):
    assert parse_expose(fixtures_dir / "simple_dockerfile" / "Dockerfile") == [8080]


def test_parse_expose_handles_protocol_multiples_and_vars(tmp_path):
    df = tmp_path / "Dockerfile"
    df.write_text(
        "FROM scratch\nEXPOSE 80/tcp 443/tcp\nexpose 8080\nEXPOSE $PORT\nEXPOSE 80\n"
    )
    # protocol suffix stripped, lowercase honoured, $PORT skipped, dupes dropped
    assert parse_expose(df) == [80, 443, 8080]


def test_parse_expose_missing_file(tmp_path):
    assert parse_expose(tmp_path / "nope") == []


def test_detect_dockerfile_only_project(fixtures_dir):
    plan = detect(fixtures_dir / "simple_dockerfile")
    assert plan.kind == "dockerfile"
    assert plan.compose_file is None
    assert plan.service == "app"
    assert plan.port == 8080
    assert plan.port_source == "dockerfile"
    assert plan.slug == "simple-dockerfile"
    assert plan.container_name == "portal-simple-dockerfile-app"
    assert plan.origin_url == "http://portal-simple-dockerfile-app:8080"


def test_port_flag_beats_dockerfile(fixtures_dir):
    plan = detect(fixtures_dir / "simple_dockerfile", port=1234)
    assert (plan.port, plan.port_source) == (1234, "flag")


def test_detect_fails_without_dockerfile_or_compose(tmp_path):
    with pytest.raises(DetectionError, match="neither a Dockerfile nor a compose"):
        detect(tmp_path)


def test_detect_fails_when_no_port_discoverable(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\nCMD [\"/x\"]\n")
    with pytest.raises(DetectionError, match="port="):
        detect(tmp_path)


def test_env_report_lists_missing_keys_only(fixtures_dir, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "Dockerfile").write_text("FROM scratch\nEXPOSE 80\n")
    (project / ".env.example").write_text("A=\nB=\n# comment\nC=\n")
    (project / ".env").write_text("A=realsecret\nC=other\n")

    report = check_env(project)
    assert report.has_example and report.has_env
    assert report.missing_keys == ["B"]
    # values never leak into the report
    assert "realsecret" not in repr(report)


def test_env_report_no_env_file_at_all(fixtures_dir):
    report = check_env(fixtures_dir / "simple_dockerfile")
    assert report.has_example is True
    assert report.has_env is False
    assert report.missing_keys == ["API_KEY", "DEBUG"]


def test_parse_keys_handles_export_and_blank_lines(tmp_path):
    f = tmp_path / ".env"
    f.write_text("\n# c\nexport FOO=1\nBARE_NO_EQUALS\nBAR=2\n")
    assert parse_keys(f) == ["FOO", "BAR"]
