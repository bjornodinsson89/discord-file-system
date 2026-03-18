from __future__ import annotations

import asyncio

from services.engagement_service import EngagementService
from services.role_reward_service import RoleRewardService


class _FakePrizeTokenService:
    def __init__(self):
        self.grants: list[tuple[int, int, int]] = []

    async def grant_level_up_token(self, guild_id: int, user_id: int, level: int) -> bool:
        self.grants.append((guild_id, user_id, level))
        return True


class _FakeRepo:
    def __init__(self):
        self.settings = {
            "enabled": True,
            "message_xp_enabled": True,
            "message_xp_amount": 12,
            "message_xp_cooldown_seconds": 60,
            "ignored_channel_ids_json": [],
            "ignored_category_ids_json": [],
            "ignored_role_ids_json": [],
        }
        self.events: set[tuple[int, str]] = set()
        self.profiles: dict[tuple[int, int], dict] = {}
        self.msg_state: dict[tuple[int, int], dict] = {}
        self.level_rewards = [
            {
                "level_required": 1,
                "role_id": 100,
                "role_name": "Pickpocket",
                "role_color": "95A5A6",
            },
            {"level_required": 2, "role_id": 101, "role_name": "Mugger", "role_color": "7F8C8D"},
        ]
        self.prize_rewards: list[dict] = []

    async def get_or_create_guild_settings(self, _guild_id: int):
        return self.settings

    async def insert_event_ledger(self, *, guild_id: int, dedupe_key: str, **kwargs):
        key = (guild_id, dedupe_key)
        if key in self.events:
            return False
        self.events.add(key)
        return True

    async def apply_xp_delta(self, guild_id: int, user_id: int, xp_delta: int, increments: dict):
        profile = self.profiles.setdefault((guild_id, user_id), {"xp_total": 0, "level": 0})
        profile["xp_total"] += int(xp_delta)
        for key, value in increments.items():
            profile[key] = profile.get(key, 0) + int(value)
        return profile

    async def update_level(self, guild_id: int, user_id: int, level: int):
        self.profiles[(guild_id, user_id)]["level"] = level

    async def get_message_state(self, guild_id: int, user_id: int):
        return self.msg_state.get((guild_id, user_id))

    async def upsert_message_state(self, guild_id: int, user_id: int, **kwargs):
        self.msg_state[(guild_id, user_id)] = kwargs

    async def seed_default_reward_ladders(self, _guild_id: int):
        return None

    async def list_level_role_rewards(self, _guild_id: int):
        return self.level_rewards

    async def list_prize_roles(self, _guild_id: int):
        return self.prize_rewards

    async def set_level_reward_role_id(self, _guild_id: int, level_required: int, role_id: int):
        for reward in self.level_rewards:
            if int(reward["level_required"]) == level_required:
                reward["role_id"] = role_id

    async def set_prize_reward_role_id(
        self, _guild_id: int, milestone_type: str, milestone_value: int, role_id: int
    ):
        return None

    async def get_or_create_profile(self, guild_id: int, user_id: int) -> dict:
        return self.profiles.setdefault((guild_id, user_id), {"xp_total": 0, "level": 0})


class _FakeRole:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class _FakeMember:
    def __init__(self, member_id: int, roles=None):
        self.id = member_id
        self.roles = list(roles or [])
        self.mention = f"<@{member_id}>"

    async def add_roles(self, role, reason: str | None = None):
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, reason: str | None = None):
        if role in self.roles:
            self.roles.remove(role)


class _FakeChannel:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str):
        self.messages.append(content)


class _FakeGuild:
    def __init__(
        self, guild_id: int, member: _FakeMember, roles: list[_FakeRole], channel: _FakeChannel
    ):
        self.id = guild_id
        self._member = member
        self._roles = {role.id: role for role in roles}
        self._channel = channel

    def get_member(self, member_id: int):
        return self._member if self._member.id == member_id else None

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self._channel if channel_id == 999 else None


class _FakeBot:
    def __init__(self, guild: _FakeGuild):
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild if self._guild.id == guild_id else None


class _Harness:
    def __init__(self, repo: _FakeRepo, bot: _FakeBot):
        self.repo = repo
        self.bot = bot
        self.role_rewards = RoleRewardService(repo)
        self.sync_calls: list[tuple[int, int]] = []

    async def _sync_roles_for_member(self, guild_id: int, user_id: int) -> dict:
        self.sync_calls.append((guild_id, user_id))
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id)
        profile = await self.repo.get_or_create_profile(guild_id, user_id)
        return await self.role_rewards.sync_member_roles(guild, member, profile)

    async def _post_levelup_announcement(self, guild_id: int, user_id: int, level: int) -> None:
        settings = await self.repo.get_or_create_guild_settings(guild_id)
        channel = self.bot.get_guild(guild_id).get_channel(int(settings["levelup_channel_id"]))
        member = self.bot.get_guild(guild_id).get_member(user_id)
        await channel.send(f"🎉 {member.mention} reached Level {level} and earned 1 Prize Token.")


def test_level_up_triggers_immediate_role_sync_and_announcement():
    async def _run():
        repo = _FakeRepo()
        repo.settings["levelup_channel_id"] = 999
        repo.profiles[(1, 42)] = {"xp_total": 290, "level": 1}
        tokens = _FakePrizeTokenService()
        service = EngagementService(repo, tokens)

        old_role = _FakeRole(100, "Pickpocket")
        new_role = _FakeRole(101, "Mugger")
        member = _FakeMember(42, roles=[old_role])
        channel = _FakeChannel()
        guild = _FakeGuild(1, member, [old_role, new_role], channel)
        harness = _Harness(repo, _FakeBot(guild))

        applied = await service.award_xp(
            guild_id=1,
            user_id=42,
            event_name="manual",
            source_type="test",
            source_id="1",
            dedupe_key="level-up",
            xp_delta=10,
            on_level_up=harness._post_levelup_announcement,
            on_role_sync_needed=harness._sync_roles_for_member,
        )

        assert applied is True
        assert repo.profiles[(1, 42)]["level"] == 2
        assert tokens.grants == [(1, 42, 2)]
        assert harness.sync_calls == [(1, 42)]
        assert new_role in member.roles
        assert old_role not in member.roles
        assert channel.messages == ["🎉 <@42> reached Level 2 and earned 1 Prize Token."]

    asyncio.run(_run())


def test_message_xp_without_level_change_skips_role_sync():
    async def _run():
        repo = _FakeRepo()
        tokens = _FakePrizeTokenService()
        service = EngagementService(repo, tokens)
        repo.profiles[(1, 50)] = {"xp_total": 0, "level": 0}
        sync_calls: list[tuple[int, int]] = []
        levelups: list[tuple[int, int, int]] = []

        applied = await service.message_xp_if_eligible(
            guild_id=1,
            user_id=50,
            content="enough content to qualify",
            channel_id=321,
            role_ids=[],
            category_id=None,
            on_level_up=lambda guild_id, user_id, level: levelups.append(
                (guild_id, user_id, level)
            ),
            on_role_sync_needed=lambda guild_id, user_id: sync_calls.append((guild_id, user_id)),
        )

        assert applied is True
        assert repo.profiles[(1, 50)]["level"] == 0
        assert tokens.grants == []
        assert sync_calls == []
        assert levelups == []

    asyncio.run(_run())
