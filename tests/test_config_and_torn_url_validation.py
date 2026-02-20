import importlib

import pytest

from cogs.events import _is_valid_torn_url


def _reload_config(monkeypatch, **env):
    for key in [
        "GUILD_ID",
        "DB_PORT",
        "DATABASE_URL",
        "DISCORD_TOKEN",
        "FERNET_KEY",
        "DB_HOST",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
    ]:
        monkeypatch.delenv(key, raising=False)

    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    import config

    return importlib.reload(config)


def test_is_valid_torn_url_strict_host_validation():
    assert _is_valid_torn_url("https://www.torn.com") is True
    assert _is_valid_torn_url("https://torn.com") is True
    assert _is_valid_torn_url("https://torn.com.evil.com") is False
    assert _is_valid_torn_url("https://evil.com?torn.com") is False


def test_guild_id_blank_string_is_none(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        GUILD_ID="",
        DB_PORT="6543",
    )

    assert cfg.GUILD_ID is None


def test_db_port_blank_string_falls_back_to_default(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        GUILD_ID="123",
        DB_PORT="",
    )

    assert cfg.DB_PORT == 6543


def test_validate_config_rejects_invalid_fernet_key(monkeypatch):
    cfg = _reload_config(
        monkeypatch,
        DATABASE_URL="postgresql://user:pass@localhost:5432/testdb",
        DISCORD_TOKEN="token",
        FERNET_KEY="definitely-not-a-valid-key",
    )

    with pytest.raises(RuntimeError, match="Invalid FERNET_KEY") as exc:
        cfg.validate_config()

    assert "definitely-not-a-valid-key" not in str(exc.value)
