import pytest

from utils.guild_settings_repository import GuildSettingsRepository


class _DB:
    pool = None


def test_jsonb_helper_encodes_lists_and_dicts():
    from utils.guild_settings_repository import _jsonb

    assert _jsonb([1, 2]) == "[1, 2]"
    assert _jsonb({"a": 1}) == '{"a": 1}'
    assert _jsonb("[]") == "[]"
    assert _jsonb(None) is None


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
            "pool_channel_id": "789",
            "pools_post_channel_id": "790",
            "raffle_announcement_channel_id": "7901",
            "raffle_purchase_channel_id": "7902",
            "raffle_giveaway_purchase_channel_id": "791",
            "admin_role_ids": ["1", 2],
            "jump_ping_role_ids": ["11", 12, "11"],
            "reservation_timeout_minutes": "10",
        }
    )
    assert normalized["announce_channel_id"] == 123
    assert normalized["jump_announce_channel_id"] == 456
    assert normalized["pool_channel_id"] == 789
    assert normalized["pools_post_channel_id"] == 790
    assert normalized["raffle_announcement_channel_id"] == 7901
    assert normalized["raffle_purchase_channel_id"] == 7902
    assert normalized["raffle_giveaway_purchase_channel_id"] == 791
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

    merged = repo._merge_defaults(
        {"jump_ping_role_ids": "[]", "admin_role_ids": "[123,456]"}, guild_id=99
    )

    assert merged["jump_ping_role_ids"] == []
    assert merged["admin_role_ids"] == [123, 456]


def test_repo_bad_role_list_values_return_empty_list_and_do_not_raise():
    repo = GuildSettingsRepository(_DB())

    assert repo._normalize_role_id_list('[1,"oops"]', guild_id=123) == []
    assert repo._normalize_admin_role_ids('[1,"oops"]', guild_id=123) == []


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

    result = asyncio.run(
        repo.upsert_guild_settings(7, {"welcome_enabled": True}, welcome_channel_id=99)
    )
    assert result["welcome_enabled"] is True
    assert result["welcome_channel_id"] == 99


def test_repo_insert_or_get_guild_settings_alias_calls_get_settings(monkeypatch):
    repo = GuildSettingsRepository(_DB())

    async def fake_get_settings(guild_id):
        return {"guild_id": guild_id, "welcome_enabled": False}

    monkeypatch.setattr(repo, "get_settings", fake_get_settings)

    import asyncio

    result = asyncio.run(repo.insert_or_get_guild_settings(73))
    assert result["guild_id"] == 73


def test_repo_ensure_guild_exists_creates_when_missing(monkeypatch):
    class _Conn:
        async def fetchrow(self, query, guild_id):
            return None

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    class _DbWithPool:
        pool = _Pool()

    repo = GuildSettingsRepository(_DbWithPool())

    called = {"count": 0}

    async def fake_create_defaults(guild_id):
        called["count"] += 1
        return {"guild_id": guild_id}

    monkeypatch.setattr(repo, "create_default_guild_settings", fake_create_defaults)

    import asyncio

    asyncio.run(repo.ensure_guild_exists(12345))
    assert called["count"] == 1


def test_repo_merge_defaults_includes_pool_channel_id():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({}, guild_id=101)
    assert "pool_channel_id" in merged
    assert merged["pool_channel_id"] is None


def test_repo_merge_defaults_includes_pools_post_channel_id():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({}, guild_id=102)
    assert "pools_post_channel_id" in merged
    assert merged["pools_post_channel_id"] is None


def test_repo_merge_defaults_includes_raffle_giveaway_purchase_channel_id():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({}, guild_id=103)
    assert "raffle_giveaway_purchase_channel_id" in merged
    assert merged["raffle_giveaway_purchase_channel_id"] is None


def test_repo_merge_defaults_includes_disable_99k_announcements_default_false():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({}, guild_id=104)
    assert "disable_99k_announcements" in merged
    assert merged["disable_99k_announcements"] is False


def test_repo_normalizes_disable_99k_announcements_bool_values():
    repo = GuildSettingsRepository(_DB())
    normalized = repo._normalize_updates({"disable_99k_announcements": "true"})
    assert normalized["disable_99k_announcements"] is True


def test_repo_supports_store_channel_id():
    repo = GuildSettingsRepository(_DB())

    normalized = repo._normalize_updates({"store_channel_id": "12345"})
    assert normalized["store_channel_id"] == 12345

    merged = repo._merge_defaults({}, guild_id=9002)
    assert "store_channel_id" in merged
    assert merged["store_channel_id"] is None


def test_repo_supports_who_can_jump_panel_fields():
    repo = GuildSettingsRepository(_DB())
    normalized = repo._normalize_updates(
        {
            "who_can_jump_channel_id": "12345",
            "who_can_jump_message_id": "67890",
            "who_can_jump_page_index": "2",
        }
    )
    assert normalized["who_can_jump_channel_id"] == 12345
    assert normalized["who_can_jump_message_id"] == 67890
    assert normalized["who_can_jump_page_index"] == 2

    merged = repo._merge_defaults({}, guild_id=9001)
    assert "who_can_jump_channel_id" in merged
    assert "who_can_jump_message_id" in merged
    assert "who_can_jump_page_index" in merged
    assert merged["who_can_jump_channel_id"] is None
    assert merged["who_can_jump_message_id"] is None
    assert merged["who_can_jump_page_index"] == 0


def test_repo_defaults_admin_key_strategy_to_pool():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({}, guild_id=999)

    assert merged["admin_key_strategy"] == "pool"
    assert merged["admin_key_single_discord_id"] is None


def test_repo_normalizes_admin_key_single_settings():
    repo = GuildSettingsRepository(_DB())
    normalized = repo._normalize_updates(
        {"admin_key_strategy": "single", "admin_key_single_discord_id": "12345"}
    )

    assert normalized["admin_key_strategy"] == "single"
    assert normalized["admin_key_single_discord_id"] == 12345


def test_repo_merge_defaults_normalizes_admin_key_strategy_aliases():
    repo = GuildSettingsRepository(_DB())

    merged_single = repo._merge_defaults({"admin_key_strategy": "Single Admin Key"}, guild_id=123)
    merged_pool = repo._merge_defaults({"admin_key_strategy": "Admin Key Pool"}, guild_id=124)

    assert merged_single["admin_key_strategy"] == "single"
    assert merged_pool["admin_key_strategy"] == "pool"


def test_repo_merge_defaults_does_not_require_pool_member_ids_column():
    repo = GuildSettingsRepository(_DB())
    merged = repo._merge_defaults({}, guild_id=1000)

    assert "admin_key_pool_member_ids" not in merged


def test_repo_replace_admin_key_pool_members(monkeypatch):
    executed = []
    executemany_calls = []

    class _Txn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Conn:
        async def execute(self, query, *args):
            executed.append((query, args))

        async def executemany(self, query, values):
            executemany_calls.append((query, list(values)))

        def transaction(self):
            return _Txn()

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Pool:
        pass

    class _Db:
        pool = _Pool()

    monkeypatch.setattr(
        "utils.guild_settings_repository.acquire_conn", lambda _pool, _timeout: _Acquire()
    )
    repo = GuildSettingsRepository(_Db())

    import asyncio

    asyncio.run(repo.replace_admin_key_pool_members(77, [901, 902, 901]))

    assert any(
        "DELETE FROM public.guild_admin_key_pool_members" in query for query, _args in executed
    )
    assert executemany_calls[0][1] == [(77, 901), (77, 902)]
