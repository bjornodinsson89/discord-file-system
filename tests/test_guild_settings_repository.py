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
            "jump_announce_channel_id": "456",
            "admin_role_ids": ["1", 2],
            "jump_ping_role_ids": ["11", 12, "11"],
            "reservation_timeout_minutes": "10",
        }
    )
    assert normalized["announce_channel_id"] == 123
    assert normalized["jump_announce_channel_id"] == 456
    assert normalized["admin_role_ids"] == [1, 2]
    assert normalized["jump_ping_role_ids"] == [11, 12]
    assert normalized["reservation_timeout_minutes"] == 10


def test_repo_normalizes_string_json_and_csv_role_lists():
    repo = GuildSettingsRepository(_DB())

    normalized = repo._normalize_updates(
        {
            "admin_role_ids": '["1", "2"]',
            "jump_ping_role_ids": "123,456",
        }
    )

    assert normalized["admin_role_ids"] == [1, 2]
    assert normalized["jump_ping_role_ids"] == [123, 456]


def test_repo_normalizes_jsonb_string_array_from_db_row():
    repo = GuildSettingsRepository(_DB())

    merged = repo._merge_defaults({"jump_ping_role_ids": "[]", "admin_role_ids": "[123,456]"}, guild_id=99)

    assert merged["jump_ping_role_ids"] == []
    assert merged["admin_role_ids"] == [123, 456]


def test_repo_bad_role_list_values_return_empty_list_and_do_not_raise():
    repo = GuildSettingsRepository(_DB())

    assert repo._normalize_role_id_list("[1,\"oops\"]", guild_id=123) == []
    assert repo._normalize_admin_role_ids("[1,\"oops\"]", guild_id=123) == []


def test_repo_merge_defaults_normalizes_jump_ping_roles():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({"jump_ping_role_ids": None}, guild_id=55)
    assert merged["jump_ping_role_ids"] == []


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
