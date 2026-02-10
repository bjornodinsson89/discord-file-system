import asyncio

from utils.guild_settings_repository import GuildSettingsRepository


class _Conn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, *_args, **_kwargs):
        return None

    async def fetchrow(self, *_args, **_kwargs):
        return {
            "guild_id": 123,
            "announce_channel_id": 456,
            "admin_role_ids": [789],
            "welcome_enabled": False,
            "welcome_message_template": None,
            "jump_99k_channel_id": None,
            "raffle_channel_id": None,
            "insurance_channel_id": None,
            "welcome_channel_id": None,
            "host99k_role_id": None,
            "insurer_role_id": None,
            "auto_complete_enabled": True,
            "reservation_timeout_minutes": 5,
        }


class _Pool:
    def acquire(self):
        return _Conn()


class _DB:
    pool = _Pool()


def test_settings_repo_get_and_upsert_smoke():
    async def _run():
        repo = GuildSettingsRepository(_DB())
        created = await repo.get_or_create(123)
        assert created["guild_id"] == 123

        updated = await repo.upsert_settings(123, announce_channel_id="456", admin_role_ids=["789"])
        assert updated["announce_channel_id"] == 456
        assert updated["admin_role_ids"] == [789]

    asyncio.run(_run())
