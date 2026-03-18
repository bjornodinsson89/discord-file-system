from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.raffles import RaffleActionConfirmView, RaffleManageView, _build_paid_raffle_panel_embed
from cogs.free_raffle import FreeRaffleCog
from services.store_service import StoreService


class _FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages: list[dict] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **_kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeFollowup:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})
        return SimpleNamespace(content=content, **kwargs)


class _FakeInteraction:
    def __init__(self, user_id: int = 42):
        self.user = SimpleNamespace(id=user_id)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.client = SimpleNamespace(get_cog=lambda _name: None)
        self.guild = object()


class _FakeCreateRepo:
    def __init__(self):
        self.created_payload = None
        self.item = None

    async def create_item(self, **payload):
        self.created_payload = dict(payload)
        self.item = {"id": 99, **payload}
        return dict(self.item)

    async def get_item(self, guild_id: int, item_id: int):
        if self.item and guild_id == self.item["guild_id"] and item_id == self.item["id"]:
            return dict(self.item)
        return None

    async def update_item(self, guild_id: int, item_id: int, **changes):
        if not self.item or guild_id != self.item["guild_id"] or item_id != self.item["id"]:
            return None
        self.item.update(changes)
        return dict(self.item)


class _FakeTokenService:
    pass


class _FakeTornRepo:
    def __init__(self, result=None):
        self.result = result
        self.lookups: list[str] = []

    async def resolve_store_item_match_by_name(self, raw_name: str):
        self.lookups.append(raw_name)
        return self.result


def test_draw_button_requires_confirmation_before_executing(monkeypatch):
    async def _run():
        monkeypatch.setattr("cogs.raffles._is_raffle_admin", AsyncMock(return_value=True))
        view = RaffleManageView(raffle_id=7)
        view._perform_draw = AsyncMock()
        interaction = _FakeInteraction()

        await view.draw.callback(interaction)

        assert interaction.response.deferred is True
        assert view._perform_draw.await_count == 0
        prompt = interaction.followup.messages[-1]
        assert prompt["content"] == "Draw raffle now?"
        assert isinstance(prompt["view"], RaffleActionConfirmView)
        assert [child.label for child in prompt["view"].children] == ["Confirm Draw", "Keep Open / Back"]

    asyncio.run(_run())


def test_draw_confirmation_executes_only_after_confirm(monkeypatch):
    async def _run():
        monkeypatch.setattr("cogs.raffles._is_raffle_admin", AsyncMock(return_value=True))
        manage_view = RaffleManageView(raffle_id=8)
        manage_view._perform_draw = AsyncMock()
        confirm_view = RaffleActionConfirmView(manage_view, action="draw", owner_user_id=55)
        interaction = _FakeInteraction(user_id=55)

        await confirm_view.children[0].callback(interaction)

        assert manage_view._perform_draw.await_count == 1
        assert interaction.response.deferred is True

    asyncio.run(_run())


def test_cancel_button_requires_confirmation_before_executing(monkeypatch):
    async def _run():
        monkeypatch.setattr("cogs.raffles._is_raffle_admin", AsyncMock(return_value=True))
        view = RaffleManageView(raffle_id=9)
        view._perform_cancel = AsyncMock()
        interaction = _FakeInteraction()

        await view.cancel.callback(interaction)

        assert interaction.response.deferred is True
        assert view._perform_cancel.await_count == 0
        prompt = interaction.followup.messages[-1]
        assert prompt["content"] == "Cancel raffle?"
        assert [child.label for child in prompt["view"].children] == ["Confirm Cancel", "Keep Active / Back"]

    asyncio.run(_run())


def test_cancel_confirmation_executes_only_after_confirm(monkeypatch):
    async def _run():
        monkeypatch.setattr("cogs.raffles._is_raffle_admin", AsyncMock(return_value=True))
        manage_view = RaffleManageView(raffle_id=10)
        manage_view._perform_cancel = AsyncMock()
        confirm_view = RaffleActionConfirmView(manage_view, action="cancel", owner_user_id=77)
        interaction = _FakeInteraction(user_id=77)

        await confirm_view.children[0].callback(interaction)

        assert manage_view._perform_cancel.await_count == 1

    asyncio.run(_run())


def test_unauthorized_users_cannot_confirm_management_actions(monkeypatch):
    async def _run():
        monkeypatch.setattr("cogs.raffles._is_raffle_admin", AsyncMock(return_value=False))
        manage_view = RaffleManageView(raffle_id=11)
        manage_view._perform_draw = AsyncMock()
        confirm_view = RaffleActionConfirmView(manage_view, action="draw", owner_user_id=12)
        interaction = _FakeInteraction(user_id=12)

        await confirm_view.children[0].callback(interaction)

        assert manage_view._perform_draw.await_count == 0
        assert interaction.followup.messages[-1]["content"].startswith("You need the Admin role")

    asyncio.run(_run())


def test_store_name_lookup_resolves_xanax_and_thumbnail_without_manual_id():
    async def _run():
        repo = _FakeCreateRepo()
        torn_repo = _FakeTornRepo(
            result={"item_id": 206, "name": "Xanax", "image_url": "https://img.example/xanax.png"}
        )
        service = StoreService(repo, _FakeTokenService(), torn_repo)

        item, note = await service.create_store_item(
            guild_id=1,
            name="Xanax",
            description="desc",
            category="torn_item",
            token_cost=25,
            stock=2,
            fulfillment_type="admin_manual",
            created_by=5,
        )

        assert note is None
        assert torn_repo.lookups == ["Xanax"]
        assert repo.created_payload["torn_item_id"] == 206
        assert repo.created_payload["thumbnail_url"] == "https://img.example/xanax.png"
        assert "item_id" not in item or item["torn_item_id"] == 206

    asyncio.run(_run())


def test_store_update_reresolves_torn_metadata_when_name_changes():
    async def _run():
        repo = _FakeCreateRepo()
        repo.item = {
            "id": 99,
            "guild_id": 1,
            "name": "Old Name",
            "description": None,
            "category": "torn_item",
            "token_cost": 5,
            "stock": None,
            "fulfillment_type": "admin_manual",
            "discord_role_id": None,
            "torn_item_name": "Old Name",
            "torn_item_id": 1,
            "thumbnail_url": None,
        }
        torn_repo = _FakeTornRepo(
            result={"item_id": 206, "name": "Xanax", "image_url": "https://img.example/xanax.png"}
        )
        service = StoreService(repo, _FakeTokenService(), torn_repo)

        updated, note = await service.update_store_item(guild_id=1, item_id=99, name="Xanax", token_cost=10)

        assert note is None
        assert updated["name"] == "Xanax"
        assert updated["torn_item_id"] == 206
        assert updated["thumbnail_url"] == "https://img.example/xanax.png"

    asyncio.run(_run())


def test_raffle_panel_hides_time_row_when_no_end_time_exists():
    embed = _build_paid_raffle_panel_embed(
        {
            "raffle_id": 1,
            "prize": "Prize",
            "tickets_available": 0,
            "tickets_sold": 3,
            "created_at": datetime.now(timezone.utc),
            "end_time": None,
            "end_trigger": "manual",
            "ticket_payment_type": "xanax",
            "ticket_price": 2,
            "creator_discord_id": 7,
        }
    )
    live_stats = next(field.value for field in embed.fields if field.name == "LIVE STATS")
    assert "Time:" not in live_stats
    assert "remaining" not in (embed.footer.text or "").lower()


def test_raffle_panel_keeps_time_row_when_end_time_exists():
    now = datetime.now(timezone.utc)
    embed = _build_paid_raffle_panel_embed(
        {
            "raffle_id": 1,
            "prize": "Prize",
            "tickets_available": 10,
            "tickets_sold": 3,
            "created_at": now - timedelta(hours=1),
            "end_time": now + timedelta(hours=1),
            "end_trigger": "time",
            "ticket_payment_type": "xanax",
            "ticket_price": 2,
            "creator_discord_id": 7,
        }
    )
    live_stats = next(field.value for field in embed.fields if field.name == "LIVE STATS")
    assert "Time:" in live_stats


def test_giveaway_embed_hides_time_row_when_no_end_time_exists(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        repo = SimpleNamespace(get_winner=AsyncMock(return_value=None), get_entry_count=AsyncMock(return_value=4))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        embed = await FreeRaffleCog.build_raffle_embed(
            cog,
            {
                "id": 5,
                "status": "active",
                "prize_text": "Xanax",
                "note_text": None,
                "button_join_enabled": True,
                "auto_entry_enabled": False,
                "weighted_odds_enabled": False,
                "host_discord_id": 77,
                "created_at": datetime.now(timezone.utc),
                "ends_at": None,
            },
        )
        live_stats = next(field.value for field in embed.fields if field.name == "LIVE STATS")
        info = next(field.value for field in embed.fields if field.name == "GIVEAWAY INFO")
        assert "Time:" not in live_stats
        assert "Remaining:" not in info
        assert "Ends:" not in info

    asyncio.run(_run())


def test_giveaway_embed_keeps_time_row_when_end_time_exists(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        repo = SimpleNamespace(get_winner=AsyncMock(return_value=None), get_entry_count=AsyncMock(return_value=4))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        now = datetime.now(timezone.utc)
        embed = await FreeRaffleCog.build_raffle_embed(
            cog,
            {
                "id": 5,
                "status": "active",
                "prize_text": "Xanax",
                "note_text": None,
                "button_join_enabled": True,
                "auto_entry_enabled": False,
                "weighted_odds_enabled": False,
                "host_discord_id": 77,
                "created_at": now - timedelta(minutes=10),
                "ends_at": now + timedelta(minutes=10),
            },
        )
        live_stats = next(field.value for field in embed.fields if field.name == "LIVE STATS")
        info = next(field.value for field in embed.fields if field.name == "GIVEAWAY INFO")
        assert "Time:" in live_stats
        assert "Remaining:" in info
        assert "Ends:" in info

    asyncio.run(_run())


def test_store_add_flow_source_uses_name_field_without_manual_torn_item_id_prompt():
    src = Path("cogs/store.py").read_text(encoding="utf-8")
    assert 'label="Name"' in src
    assert 'label="Item ID"' not in src.split("class AddStoreItemModal", 1)[1].split("class UpdateItemModal", 1)[0]
