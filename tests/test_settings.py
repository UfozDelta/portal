import pytest

from portal.errors import ConfigError
from portal.settings import load_settings


def test_loads_token_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CLOUDFLARE_API_TOKEN=abc123\n")
    settings = load_settings(env)
    assert settings.token == "abc123"
    assert settings.account_id is None


def test_loads_optional_account_id(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CLOUDFLARE_API_TOKEN=abc123\nCLOUDFLARE_ACCOUNT_ID=acct1\n")
    settings = load_settings(env)
    assert settings.account_id == "acct1"


def test_real_env_var_wins_over_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("CLOUDFLARE_API_TOKEN=from-file\n")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "from-environment")
    assert load_settings(env).token == "from-environment"


def test_missing_token_raises_config_error(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SOME_OTHER_KEY=1\n")
    with pytest.raises(ConfigError, match="No Cloudflare API token found"):
        load_settings(env)


def test_missing_env_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="No Cloudflare API token found"):
        load_settings(tmp_path / "does-not-exist.env")
