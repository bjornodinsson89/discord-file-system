from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import setup_panel
from cogs.engagement import EngagementCog
from cogs.free_raffle import FreeRaffleCog
from services.engagement_service import EngagementService
from views.free_raffle_views import EnterRaffleView


class _FakePrizeTokens:
    def __init__(self):
        self.calls = []

    async def grant_configured_reward(self, **kwargs):
        self.calls.append(kwargs)
        return True


class _FakeHJD:
    def __init__(self):
        self.calls = []

    async def grant_level_up_hjd(self, guild_id: int, user_id: int, level: int, amount: int = 100):
        self.calls.append({"guild_id": guild_id, "user_id": user_id, "level": level, "amount": amount})
        return True

    async def grant_configured_reward(self, **kwargs):
        self.calls.append(kwargs)
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
            "level_up_coin_reward": 3,
            "level_up_hjd_reward": 250,
            "paid_raffle_purchase_xp_base": 15,
            "paid_raffle_purchase_xp_per_ticket": 2,
            "paid_raffle_purchase_xp_cap": 50,
            "paid_raffle_purchase_coin_reward": 5,
            "paid_raffle_purchase_hjd_reward": 15,
            "jump_purchase_xp": 40,
            "jump_purchase_coin_reward": 6,
            "jump_purchase_hjd_reward": 16,
            "jump_completion_xp": 75,
            "jump_completion_coin_reward": 7,
            "jump_completion_hjd_reward": 17,
        }
        self.events = set()
        self.profiles = {(1, 2): {"xp_total": 99, "level": 0}, (1, 7): {"xp_total": 0, "level": 0}}

    async def get_or_create_guild_settings(self, _guild_id: int):
        return self.settings

    async def insert_event_ledger(self, *, guild_id: int, dedupe_key: str, **_kwargs):
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


class _FakeResponse:
    def __init__(self):
        self.sent = None

    def is_done(self):
        return self.sent is not None

    async def send_message(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}


class _FakeFollowup:
    def __init__(self):
        self.sent = None

    async def send(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}


def test_giveaway_embed_title_entries_and_timer_sections(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        repo = SimpleNamespace(get_winner=AsyncMock(return_value=None), get_entry_count=AsyncMock(return_value=24))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        now = datetime.now(timezone.utc)
        embed = await FreeRaffleCog.build_raffle_embed(
            cog,
            {
                "id": 9,
                "status": "active",
                "prize_text": "2 Xanax + 1 Erotic DVD",
                "note_text": "Host note",
                "button_join_enabled": False,
                "auto_entry_enabled": True,
                "weighted_odds_enabled": True,
                "auto_entry_max_per_user": 4,
                "host_discord_id": 99,
                "guild_id": 1,
                "created_at": now - timedelta(hours=1),
                "ends_at": now + timedelta(hours=3),
            },
        )
        assert embed.title == "Giveaway for 2 Xanax + 1 Erotic DVD"
        stats = next(field.value for field in embed.fields if field.name == "STATS")
        time_field = next(field.value for field in embed.fields if field.name == "TIME")
        assert "Entries:" in stats
        assert "Entrants" not in stats
        assert "Mode: **Auto Entry + Weighted**" in stats
        assert "Ends in:" in time_field

    asyncio.run(_run())


def test_giveaway_embed_hides_time_when_no_timer(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        repo = SimpleNamespace(get_winner=AsyncMock(return_value=None), get_entry_count=AsyncMock(return_value=8))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        embed = await FreeRaffleCog.build_raffle_embed(
            cog,
            {
                "id": 10,
                "status": "active",
                "prize_text": "Erotic DVD",
                "note_text": None,
                "button_join_enabled": True,
                "auto_entry_enabled": False,
                "weighted_odds_enabled": False,
                "host_discord_id": 77,
                "guild_id": 1,
                "created_at": datetime.now(timezone.utc),
                "ends_at": None,
            },
        )
        assert all(field.name != "TIME" for field in embed.fields)
        stats = next(field.value for field in embed.fields if field.name == "STATS")
        assert "Entries:" in stats

    asyncio.run(_run())


def test_info_button_exists_and_returns_ephemeral_progress(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        progress_repo = SimpleNamespace(
            get_raffle=AsyncMock(return_value={"id": 11, "guild_id": 1, "status": "active", "auto_entry_enabled": True, "button_join_enabled": False, "weighted_odds_enabled": True, "auto_entry_max_per_user": 5}),
            get_auto_entry_progress=AsyncMock(return_value={"qualifying_message_count": 9, "auto_entries_granted": 2}),
            get_entry=AsyncMock(return_value={"entry_weight": 4}),
        )
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: progress_repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        cog._get_coin_balance = AsyncMock(return_value=1)
        view = FreeRaffleCog.build_free_raffle_view(cog, 11, status="active", button_join_enabled=False)
        assert isinstance(view, EnterRaffleView)
        labels = [child.label for child in view.children]
        assert "ℹ️ Info" in labels
        interaction = SimpleNamespace(user=SimpleNamespace(id=22), response=_FakeResponse(), followup=_FakeFollowup())
        await FreeRaffleCog.handle_info(cog, interaction, 11)
        embed = interaction.response.sent["embed"]
        assert interaction.response.sent["ephemeral"] is True
        assert "Every 15 qualifying messages gives 1 entry." in embed.fields[0].value
        assert "Messages toward next entry: **9 / 15**" in embed.fields[1].value

    asyncio.run(_run())


def test_auto_entry_processing_enforces_coin_gate_and_counts_awards(monkeypatch):
    async def _run():
        cog = EngagementCog.__new__(EngagementCog)
        cog.repo = SimpleNamespace(
            get_or_create_guild_settings=AsyncMock(return_value={"auto_entry_giveaways_enabled": True}),
            get_or_create_profile=AsyncMock(side_effect=[{"prize_token_balance": 0, "level": 0}, {"prize_token_balance": 2, "level": 10}]),
        )
        cog.role_rewards = SimpleNamespace(giveaway_weight_for_level=lambda level: level + 1)
        raffle_repo = SimpleNamespace(
            list_active_auto_entry_raffles=AsyncMock(return_value=[{"id": 1}, {"id": 2}]),
            increment_auto_entry_progress=AsyncMock(side_effect=[{"awarded": True, "entries_granted": 1}, {"awarded": True, "entries_granted": 2}]),
        )
        monkeypatch.setattr("cogs.engagement.FreeRaffleRepository", lambda _pool: raffle_repo)
        monkeypatch.setattr("cogs.engagement.get_pool", lambda: object())
        blocked = await EngagementCog._process_message_auto_entries(cog, 1, 55)
        awarded = await EngagementCog._process_message_auto_entries(cog, 1, 55)
        assert blocked == 0
        assert awarded == 3
        assert raffle_repo.increment_auto_entry_progress.await_count == 2

    asyncio.run(_run())


def test_configured_rewards_are_used_by_backend_logic():
    async def _run():
        repo = _FakeRepo()
        tokens = _FakePrizeTokens()
        hjd = _FakeHJD()
        service = EngagementService(repo, tokens, hjd)
        await service.award_xp(guild_id=1, user_id=2, event_name="manual", source_type="test", source_id="1", dedupe_key="lvl", xp_delta=1)
        await service.process_paid_raffle_purchase({"guild_id": 1, "user_id": 7, "entry_id": 1, "ticket_count": 4, "dedupe_key": "raffle"})
        await service.process_jump_purchase_verified({"guild_id": 1, "user_id": 7, "session_id": 5, "dedupe_key": "jump-buy"})
        await service.process_jump_completed({"guild_id": 1, "user_id": 7, "session_id": 5, "dedupe_key": "jump-done"})
        assert any(call.get("amount") == 3 for call in tokens.calls)
        assert any(call.get("amount") == 250 for call in hjd.calls)
        reward_amounts = {call.get("amount") for call in tokens.calls if isinstance(call, dict)}
        assert {5, 6, 7}.issubset(reward_amounts)
        hjd_amounts = {call.get("amount") for call in hjd.calls if isinstance(call, dict)}
        assert {15, 16, 17}.issubset(hjd_amounts)

    asyncio.run(_run())


def test_setup_reward_controls_and_friendly_numeric_parsing():
    src = Path("setup_panel.py").read_text(encoding="utf-8")
    assert "Level-up Coin Reward" in src
    assert "Level-up HJD Reward" in src
    assert "Raffle Purchase Coin Reward" in src
    assert "Jump Completion HJD Reward" in src
    assert "replace(\",\", \"\")" in src

    assert setup_panel._parse_friendly_int("1,000", label="Amount") == 1000
    try:
        setup_panel._parse_friendly_int("abc", label="Amount")
    except ValueError as error:
        assert "whole number" in str(error)
    else:
        raise AssertionError("expected ValueError")


def test_stale_prize_coin_copy_is_gone():
    src = Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    assert "once you receive a prize coin" not in src.lower()
