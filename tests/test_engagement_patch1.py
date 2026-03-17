from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.engagement_service import EngagementService, level_from_total_xp, required_total_xp


class _FakePrizeTokenService:
    def __init__(self):
        self.grants: list[tuple[int, int, int]] = []

    async def grant_level_up_token(self, guild_id: int, user_id: int, level: int) -> bool:
        key = (guild_id, user_id, level)
        if key in self.grants:
            return False
        self.grants.append(key)
        return True


class _FakeRepo:
    def __init__(self):
        self.settings = {
            "enabled": True,
            "message_xp_enabled": True,
            "reaction_xp_enabled": True,
            "message_xp_amount": 12,
            "message_xp_cooldown_seconds": 60,
            "reaction_xp_amount": 2,
            "reaction_xp_hourly_cap": 20,
            "ignored_channel_ids_json": [],
            "ignored_category_ids_json": [],
            "ignored_role_ids_json": [],
        }
        self.events = set()
        self.profiles: dict[tuple[int, int], dict] = {}
        self.msg_state: dict[tuple[int, int], dict] = {}
        self.reacts: set[tuple[int, int, int]] = set()
        self.reaction_hourly = 0

    async def get_or_create_guild_settings(self, _guild_id: int):
        return self.settings

    async def insert_event_ledger(self, *, guild_id: int, dedupe_key: str, **kwargs):
        key = (guild_id, dedupe_key)
        if key in self.events:
            return False
        self.events.add(key)
        return True

    async def apply_xp_delta(self, guild_id: int, user_id: int, xp_delta: int, increments: dict):
        key = (guild_id, user_id)
        p = self.profiles.setdefault(key, {"xp_total": 0, "level": 0})
        p["xp_total"] += xp_delta
        for k, v in increments.items():
            p[k] = p.get(k, 0) + int(v)
        return p

    async def update_level(self, guild_id: int, user_id: int, level: int):
        self.profiles[(guild_id, user_id)]["level"] = level

    async def get_message_state(self, guild_id: int, user_id: int):
        return self.msg_state.get((guild_id, user_id))

    async def upsert_message_state(self, guild_id: int, user_id: int, **kwargs):
        self.msg_state[(guild_id, user_id)] = kwargs

    async def get_hourly_reaction_xp(self, _guild_id: int, _target_user_id: int):
        return self.reaction_hourly

    async def add_reaction_marker(self, guild_id: int, message_id: int, reactor_user_id: int, _target_user_id: int):
        key = (guild_id, message_id, reactor_user_id)
        if key in self.reacts:
            return False
        self.reacts.add(key)
        return True


def test_level_calculation():
    assert required_total_xp(0) == 0
    assert required_total_xp(1) == 100
    assert required_total_xp(2) == 300
    assert level_from_total_xp(99) == 0
    assert level_from_total_xp(100) == 1
    assert level_from_total_xp(300) == 2


def test_migration_has_engagement_tables():
    src = open("migrations/2026_03_17_add_engagement_backend_foundation.sql", "r", encoding="utf-8").read()
    for table in [
        "engagement_profiles",
        "engagement_event_ledger",
        "prize_token_transactions",
        "engagement_guild_settings",
        "engagement_message_state",
        "engagement_reaction_state",
    ]:
        assert table in src


def test_command_groups_and_commands_present():
    src = open("cogs/engagement.py", "r", encoding="utf-8").read()
    assert 'Group(name="profile"' in src
    assert '@profile.command(name="view"' in src
    assert '@profile.command(name="rank"' in src
    assert '@tokens.command(name="balance"' in src
    assert '@engagement.command(name="debug"' in src


def test_message_xp_cooldown_and_similarity():
    import asyncio

    async def _run():
        repo = _FakeRepo()
        service = EngagementService(repo, _FakePrizeTokenService())
        now = datetime.now(timezone.utc)

        first = await service.message_xp_if_eligible(
            guild_id=1,
            user_id=2,
            content="Hello world!",
            channel_id=100,
            role_ids=[],
            category_id=None,
            now=now,
        )
        assert first is True

        second = await service.message_xp_if_eligible(
            guild_id=1,
            user_id=2,
            content="Hello world!",
            channel_id=100,
            role_ids=[],
            category_id=None,
            now=now + timedelta(seconds=61),
        )
        assert second is False

        third = await service.message_xp_if_eligible(
            guild_id=1,
            user_id=2,
            content="Different message body",
            channel_id=100,
            role_ids=[],
            category_id=None,
            now=now + timedelta(seconds=20),
        )
        assert third is False

    asyncio.run(_run())


def test_reaction_xp_duplicate_and_cap():
    import asyncio

    async def _run():
        repo = _FakeRepo()
        service = EngagementService(repo, _FakePrizeTokenService())
        ok = await service.reaction_xp_if_eligible(guild_id=1, reactor_user_id=10, target_user_id=20, message_id=30)
        assert ok is True

        dup = await service.reaction_xp_if_eligible(guild_id=1, reactor_user_id=10, target_user_id=20, message_id=30)
        assert dup is False

        repo.reaction_hourly = 20
        capped = await service.reaction_xp_if_eligible(guild_id=1, reactor_user_id=11, target_user_id=20, message_id=30)
        assert capped is False

    asyncio.run(_run())


def test_dispatch_consumers_award_expected_xp_and_no_xp_for_giveaway():
    import asyncio

    async def _run():
        repo = _FakeRepo()
        tokens = _FakePrizeTokenService()
        service = EngagementService(repo, tokens)

        await service.process_paid_raffle_purchase({"guild_id": 1, "user_id": 7, "entry_id": 1, "ticket_count": 4, "dedupe_key": "a"})
        await service.process_jump_purchase_verified({"guild_id": 1, "user_id": 7, "session_id": 9, "dedupe_key": "b"})
        await service.process_jump_completed({"guild_id": 1, "user_id": 7, "session_id": 9, "dedupe_key": "c"})
        profile = repo.profiles[(1, 7)]
        assert profile["paid_raffle_xp_total"] == 23
        assert profile["jump_purchase_xp_total"] == 40
        assert profile["jump_completion_xp_total"] == 75

    asyncio.run(_run())


def test_level_up_grants_exactly_one_token_per_level():
    import asyncio

    async def _run():
        repo = _FakeRepo()
        tokens = _FakePrizeTokenService()
        service = EngagementService(repo, tokens)

        await service.award_xp(
            guild_id=1,
            user_id=2,
            event_name="manual",
            source_type="test",
            source_id="1",
            dedupe_key="unique",
            xp_delta=301,
            increments={},
        )
        assert (1, 2, 1) in tokens.grants
        assert (1, 2, 2) in tokens.grants
        assert len(tokens.grants) == 2

    asyncio.run(_run())
