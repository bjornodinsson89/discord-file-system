import pytest

from utils.guild_settings_repository import GuildSettingsRepository


class _DB:
    pool = None


def test_repo_allowlist_rejects_unknown_fields():
    repo = GuildSettingsRepository(_DB())
    with pytest.raises(ValueError):
        repo._normalize_updates({"not_a_column": 1})


def test_repo_casts_bigints_and_admin_roles():
    repo = GuildSettingsRepository(_DB())
    normalized = repo._normalize_updates(
        {
            "announce_channel_id": "123",
            "admin_role_ids": ["1", 2],
            "reservation_timeout_minutes": "10",
        }
    )
    assert normalized["announce_channel_id"] == 123
    assert normalized["admin_role_ids"] == [1, 2]
    assert normalized["reservation_timeout_minutes"] == 10


def test_repo_get_guild_settings_alias_calls_get_settings(monkeypatch):
    repo = GuildSettingsRepository(_DB())

    async def fake_get_settings(guild_id):
        return {"guild_id": guild_id}

    monkeypatch.setattr(repo, "get_settings", fake_get_settings)

    import asyncio
    result = asyncio.run(repo.get_guild_settings(42))
    assert result["guild_id"] == 42


def test_repo_upsert_guild_settings_merges_dict_and_kwargs(monkeypatch):
    repo = GuildSettingsRepository(_DB())

    async def fake_upsert(guild_id, **fields):
        return {"guild_id": guild_id, **fields}

    monkeypatch.setattr(repo, "upsert_settings", fake_upsert)

    import asyncio
    result = asyncio.run(repo.upsert_guild_settings(7, {"welcome_enabled": True}, welcome_channel_id=99))
    assert result["welcome_enabled"] is True
    assert result["welcome_channel_id"] == 99
