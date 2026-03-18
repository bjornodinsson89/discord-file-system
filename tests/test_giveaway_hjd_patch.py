from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.free_raffle import FreeRaffleCog
from services.engagement_service import EngagementService
from services.store_service import StoreService


class _FakePrizeTokens:
    def __init__(self):
        self.grants: list[tuple[int, int, int]] = []

    async def grant_level_up_token(self, guild_id: int, user_id: int, level: int) -> bool:
        self.grants.append((guild_id, user_id, level))
        return True


class _FakeHJDService:
    def __init__(self):
        self.level_grants: list[tuple[int, int, int]] = []
        self.spent = 0
        self.refunded = 0

    async def grant_level_up_hjd(self, guild_id: int, user_id: int, level: int) -> bool:
        self.level_grants.append((guild_id, user_id, level))
        return True

    async def spend_store_hjd(self, *, amount: int, **_kwargs) -> bool:
        self.spent += amount
        return True

    async def refund_store_hjd(self, *, amount: int, **_kwargs) -> bool:
        self.refunded += amount
        return True


class _FakeEngagementRepo:
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

    async def get_message_state(self, guild_id: int, user_id: int):
        return self.msg_state.get((guild_id, user_id))

    async def upsert_message_state(self, guild_id: int, user_id: int, **kwargs):
        self.msg_state[(guild_id, user_id)] = kwargs


class _ConnTx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def transaction(self):
        return _ConnTx()


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StoreRepo:
    def __init__(self):
        self.settings = {"enabled": True, "torn_item_store_enabled": True, "discord_perk_store_enabled": True}
        self.item = {
            "id": 5,
            "name": "Xanax",
            "category": "torn_item",
            "token_cost": 25,
            "stock": 3,
            "is_active": True,
            "fulfillment_type": "admin_manual",
        }
        self.redemptions = []
        self.stock_delta = 0

    def acquire(self):
        return _Acquire(_Conn())

    async def upsert_guild_settings_with_conn(self, _conn, _guild_id: int, **_changes):
        return self.settings

    async def get_item(self, _guild_id: int, item_id: int, **_kwargs):
        return dict(self.item) if item_id == self.item["id"] else None

    async def count_user_redemptions_for_item(self, *_args, **_kwargs):
        return 0

    async def create_redemption(self, **payload):
        row = {"id": 1, **payload, "created_at": datetime.now(timezone.utc)}
        self.redemptions.append(row)
        return row

    async def adjust_stock(self, _guild_id: int, _item_id: int, delta: int, **_kwargs):
        self.stock_delta += delta
        self.item["stock"] += delta
        return dict(self.item)

    async def get_redemption(self, _guild_id: int, redemption_id: int, **_kwargs):
        for row in self.redemptions:
            if row["id"] == redemption_id:
                return row
        return None

    async def update_redemption(self, _guild_id: int, redemption_id: int, **changes):
        row = await self.get_redemption(_guild_id, redemption_id)
        row.update(changes)
        return row


class _Guild:
    def __init__(self, guild_id: int):
        self.id = guild_id

    def get_role(self, _role_id: int):
        return None


class _Member:
    def __init__(self, member_id: int):
        self.id = member_id
        self.roles = []


def test_hjd_migration_contains_currency_and_auto_entry_tables():
    src = Path("migrations/2026_03_18_giveaway_hjd_message_auto_entry.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS auto_entry_max_per_user" in src
    assert "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ" in src
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle_user_unique" in src
    assert "CREATE TABLE IF NOT EXISTS public.giveaway_auto_progress" in src
    assert "ADD COLUMN IF NOT EXISTS hjd_balance" in src
    assert "CREATE TABLE IF NOT EXISTS public.happy_jump_dollar_transactions" in src


def test_level_up_grants_coin_and_100_hjd():
    async def _run():
        repo = _FakeEngagementRepo()
        repo.profiles[(1, 2)] = {"xp_total": 99, "level": 0}
        coins = _FakePrizeTokens()
        hjd = _FakeHJDService()
        service = EngagementService(repo, coins, hjd)

        applied = await service.award_xp(
            guild_id=1,
            user_id=2,
            event_name="manual",
            source_type="test",
            source_id="1",
            dedupe_key="lvl",
            xp_delta=1,
        )

        assert applied is True
        assert coins.grants == [(1, 2, 1)]
        assert hjd.level_grants == [(1, 2, 1)]

    asyncio.run(_run())


def test_store_redemption_spends_hjd_not_coins():
    async def _run():
        hjd = _FakeHJDService()
        service = StoreService(_StoreRepo(), hjd)
        redemption, err = await service.redeem_item(guild=_Guild(1), user=_Member(2), item_id=5)
        assert err is None
        assert redemption is not None
        assert hjd.spent == 25

    asyncio.run(_run())


def test_giveaway_embed_describes_message_based_auto_entry(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        repo = SimpleNamespace(get_winner=AsyncMock(return_value=None), get_entry_count=AsyncMock(return_value=7))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        embed = await FreeRaffleCog.build_raffle_embed(
            cog,
            {
                "id": 1,
                "status": "active",
                "prize_text": "Xanax",
                "note_text": None,
                "button_join_enabled": False,
                "auto_entry_enabled": True,
                "weighted_odds_enabled": True,
                "auto_entry_max_per_user": 4,
                "host_discord_id": 99,
                "created_at": datetime.now(timezone.utc),
                "ends_at": datetime.now(timezone.utc),
            },
        )
        field = next(f.value for f in embed.fields if f.name == "HOW TO PLAY")
        assert "1 coin" in field
        assert "15 qualifying chat messages" in field
        assert "4" in field

    asyncio.run(_run())


def test_storefront_copy_references_hjd():
    store_src = Path("cogs/store.py").read_text(encoding="utf-8")
    service_src = Path("services/store_service.py").read_text(encoding="utf-8")
    assert "Happy Jump Dollars (HJD)" in store_src
    assert "100 HJD on every level-up" in service_src


def test_message_based_auto_entry_wiring_is_present_in_source():
    engagement_src = Path("cogs/engagement.py").read_text(encoding="utf-8")
    raffle_src = Path("repositories/free_raffle_repo.py").read_text(encoding="utf-8")
    assert "_process_message_auto_entries" in engagement_src
    assert "if applied:" in engagement_src
    assert "qualifying_message_count" in raffle_src
    assert "banked // 15" in raffle_src
    assert "auto_entry_max_per_user" in raffle_src


def test_refresh_public_message_recreates_missing_message(monkeypatch):
    class _Channel:
        def __init__(self):
            self.sent = []

        def get_partial_message(self, _message_id: int):
            raise AssertionError("partial messages should not be used for recreation")

        async def fetch_message(self, _message_id: int):
            raise LookupError("missing")

        async def send(self, *, embed, view):
            msg = SimpleNamespace(id=555, embed=embed, view=view)
            self.sent.append(msg)
            return msg

    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        channel = _Channel()
        cog.bot = SimpleNamespace(get_channel=lambda _cid: channel)
        cog.build_raffle_embed = AsyncMock(return_value=SimpleNamespace())
        cog.build_free_raffle_view = lambda *args, **kwargs: SimpleNamespace()
        repo = SimpleNamespace(
            get_raffle=AsyncMock(return_value={
                "id": 3,
                "channel_id": 1,
                "message_id": 99,
                "host_discord_id": 2,
                "button_join_enabled": True,
                "status": "active",
            }),
            set_message_id=AsyncMock(),
        )
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        await FreeRaffleCog.refresh_public_message(cog, 3)
        assert len(channel.sent) == 1
        repo.set_message_id.assert_awaited_once_with(3, 555)

    asyncio.run(_run())
