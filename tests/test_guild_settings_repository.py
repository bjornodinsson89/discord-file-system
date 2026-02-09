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
            "admin_role_id": "55",
            "admin_role_ids": ["1", 2],
            "reservation_timeout_minutes": "10",
        }
    )
    assert normalized["announce_channel_id"] == 123
    assert normalized["admin_role_id"] == 55
    assert normalized["admin_role_ids"] == [1, 2]
    assert normalized["reservation_timeout_minutes"] == 10
