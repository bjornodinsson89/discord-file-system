from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.engagement import EngagementCog
from services.engagement_service import EngagementService


class _FakePrizeTokenService:
    async def grant_level_up_token(self, guild_id: int, user_id: int, level: int) -> bool:
        return True


class _FakeRepo:
    def __init__(self):
        self.settings = {
            "enabled": True,
            "voice_xp_enabled": True,
            "voice_xp_per_minute": 5,
            "message_xp_enabled": True,
            "reaction_xp_enabled": True,
            "ignored_channel_ids_json": [],
            "ignored_category_ids_json": [],
            "ignored_role_ids_json": [],
        }
        self.events = set()
        self.profiles = {}

    async def get_or_create_guild_settings(self, _guild_id: int):
        return self.settings

    async def insert_event_ledger(self, *, guild_id: int, dedupe_key: str, **kwargs):
        key = (guild_id, dedupe_key)
        if key in self.events:
            return False
        self.events.add(key)
        return True

    async def apply_xp_delta(self, guild_id: int, user_id: int, xp_delta: int, increments: dict):
        p = self.profiles.setdefault(
            (guild_id, user_id), {"xp_total": 0, "level": 0, "voice_xp_total": 0}
        )
        p["xp_total"] += int(xp_delta)
        p["voice_xp_total"] += int(increments.get("voice_xp_total", 0))
        return p

    async def update_level(self, guild_id: int, user_id: int, level: int):
        self.profiles[(guild_id, user_id)]["level"] = level


class _FakeMember:
    def __init__(
        self, member_id: int, *, bot: bool = False, self_mute: bool = False, self_deaf: bool = False
    ):
        self.id = member_id
        self.bot = bot
        self.roles = []
        self.voice = SimpleNamespace(self_mute=self_mute, self_deaf=self_deaf)


class _FakeChannel:
    def __init__(self, channel_id: int, members, category_id=None):
        self.id = channel_id
        self.members = members
        self.category = SimpleNamespace(id=category_id) if category_id else None


class _FakeGuild:
    def __init__(self, guild_id: int, channels, afk_channel_id=None):
        self.id = guild_id
        self.voice_channels = channels
        self.afk_channel = SimpleNamespace(id=afk_channel_id) if afk_channel_id else None


class _FakeBot:
    def __init__(self, guilds):
        self.guilds = guilds


def _build_cog(bot, repo, service):
    cog = EngagementCog.__new__(EngagementCog)
    cog.bot = bot
    cog.repo = repo
    cog.service = service
    return cog


def test_voice_xp_awards_only_eligible_members_and_skips_afk():
    async def _run():
        repo = _FakeRepo()
        service = EngagementService(repo, _FakePrizeTokenService())

        member_ok = _FakeMember(10)
        member_ok2 = _FakeMember(11)
        muted_deaf = _FakeMember(12, self_mute=True, self_deaf=True)
        bot_member = _FakeMember(13, bot=True)

        normal = _FakeChannel(100, [member_ok, member_ok2, muted_deaf, bot_member])
        afk = _FakeChannel(200, [member_ok, member_ok2])
        guild = _FakeGuild(1, [normal, afk], afk_channel_id=200)

        cog = _build_cog(_FakeBot([guild]), repo, service)
        cog._post_levelup_announcement = lambda *_args, **_kwargs: asyncio.sleep(0)

        await cog._run_voice_tick()

        assert repo.profiles[(1, 10)]["xp_total"] == 5
        assert repo.profiles[(1, 10)]["voice_xp_total"] == 5
        assert repo.profiles[(1, 11)]["xp_total"] == 5
        assert (1, 12) not in repo.profiles

    asyncio.run(_run())


def test_voice_xp_dedupe_minute_bucket():
    async def _run():
        repo = _FakeRepo()
        service = EngagementService(repo, _FakePrizeTokenService())
        ok1 = await service.voice_xp_if_eligible(
            guild_id=1,
            user_id=22,
            channel_id=333,
            role_ids=[],
            category_id=None,
            minute_bucket=12345,
        )
        ok2 = await service.voice_xp_if_eligible(
            guild_id=1,
            user_id=22,
            channel_id=333,
            role_ids=[],
            category_id=None,
            minute_bucket=12345,
        )
        assert ok1 is True
        assert ok2 is False
        assert repo.profiles[(1, 22)]["xp_total"] == 5

    asyncio.run(_run())


def test_patch2_commands_and_setup_pages_exist():
    engagement_src = open("cogs/engagement.py", encoding="utf-8").read()
    for expected in [
        '@profile.command(name="rewards"',
        '@leaderboard.command(name="xp"',
        '@leaderboard.command(name="levels"',
        '@leaderboard.command(name="tokens"',
        '@leaderboard.command(name="jumps"',
        '@leaderboard.command(name="raffles"',
        '@tokens.command(name="history"',
        'Group(name="engagement"',
        "reached Level {level} and earned 1 Prize Token",
    ]:
        if expected == 'Group(name="engagement"':
            assert expected not in engagement_src
        else:
            assert expected in engagement_src

    setup_src = open("setup_panel.py", encoding="utf-8").read()
    for expected in [
        "class EngagementCoreView",
        "class EngagementChatVoiceReactionView",
        "class EngagementEventXPView",
        "class EngagementRolesStatusView",
        "class EngagementMaintenanceView",
        'label="Engagement"',
        "ignored_channel_ids_json",
        "ignored_category_ids_json",
        "ignored_role_ids_json",
        "Create/Repair Reward Roles",
        "Sync Reward Roles",
        "View Engagement Config",
        "View Reward Role Status",
        "Debug Member Engagement",
        "Rebuild Member Profile",
        "Reverse Event",
        "Reseed Reward Definitions",
    ]:
        assert expected in setup_src
