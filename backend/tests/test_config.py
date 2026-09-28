"""Settings validation: mode switches, placeholder tokens, path resolution."""
import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**env):
    return Settings(_env_file=None, **env)


def test_rejects_invalid_mode():
    with pytest.raises(ValidationError):
        _settings(MODE="bogus")


def test_rejects_invalid_host_mode():
    with pytest.raises(ValidationError):
        _settings(HOST_MODE="bogus")


def test_rejects_placeholder_agent_token():
    with pytest.raises(ValidationError):
        _settings(AGENT_TOKEN="changeme-auth-token-replace")


def test_blank_agent_token_becomes_none():
    assert _settings(AGENT_TOKEN="   ").agent_token is None


def test_remote_without_agent_url_is_allowed_at_config_time():
    cfg = _settings(HOST_MODE="remote")
    assert cfg.host_mode == "remote"
    assert cfg.agent_url is None


def test_local_agent_flag_parsing():
    assert _settings(LOCAL_AGENT="true").local_agent is True
    assert _settings().local_agent is False


def test_resolved_import_dir_default_and_explicit(tmp_path):
    cfg = _settings(VM_STORAGE_PATH=str(tmp_path))
    assert cfg.resolved_import_dir == tmp_path / "imports"
    cfg = _settings(IMPORT_DIR=str(tmp_path / "custom"))
    assert cfg.resolved_import_dir == tmp_path / "custom"


def test_bounds_are_enforced():
    with pytest.raises(ValidationError):
        _settings(MAX_UPLOAD_GB=0)
    with pytest.raises(ValidationError):
        _settings(RESOURCE_BUDGET_PERCENT=200)
    with pytest.raises(ValidationError):
        _settings(GRAPHICS_TYPE="sdl")


def test_vm_name_prefix_must_end_with_hyphen():
    with pytest.raises(ValidationError):
        _settings(VM_NAME_PREFIX="labs")


def test_agent_bind_loopback_helpers():
    assert _settings(AGENT_BIND="127.0.0.1", AGENT_PORT=8443).agent_bind_is_loopback
    cfg = _settings(AGENT_BIND="0.0.0.0", AGENT_PORT=8443)
    assert not cfg.agent_bind_is_loopback
    assert cfg.api_base() == "http://0.0.0.0:8443"


def test_api_key_defaults_to_none():
    assert _settings().api_key is None
    assert _settings(API_KEY="abc").api_key == "abc"


def test_resolve_version_prefers_env(monkeypatch):
    from app.config import _resolve_version

    monkeypatch.setenv("APP_VERSION", "1.2.3")
    assert _resolve_version() == "1.2.3"


def test_resolve_version_reads_version_file(monkeypatch):
    from app.config import _resolve_version

    monkeypatch.delenv("APP_VERSION", raising=False)
    resolved = _resolve_version()
    # The repo ships a VERSION file, so this must be a non-empty string.
    assert resolved and resolved != ""
