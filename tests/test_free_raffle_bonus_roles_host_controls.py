from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.free_raffle import DraftRoleBonusConfigView, FreeRaffleCog
from repositories.torn_items import derive_lookup_candidates
from views.free_raffle_views import EnterRaffleView, HostControlsView


class _FakeResponse:
    def __init__(self):
        self.sent = None
        self.edited = None

    def is_done(self):
        return self.sent is not None or self.edited is not None

    async def send_message(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}

    async def edit_message(self, content=None, *, embed=None, view=None):
        self.edited = {"content": content, "embed": embed, "view": view}


class _FakeFollowup:
    def __init__(self):
        self.sent = None

    async def send(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}


def test_public_embed_shows_bonus_roles_and_info_is_not_duplicated(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.bot = SimpleNamespace(get_guild=lambda _gid: None)
        cog.resolve_thumbnail = AsyncMock(return_value=None)
        cog._get_coin_balance = AsyncMock(return_value=1)
        repo = SimpleNamespace(
            get_winner=AsyncMock(return_value=None),
            get_entry_count=AsyncMock(return_value=6),
            list_role_bonus_rules=AsyncMock(
                return_value=[{"role_id": 10, "bonus_entries_per_qualification": 2}]
            ),
            get_auto_entry_progress=AsyncMock(
                return_value={"qualifying_message_count": 4, "auto_entries_granted": 1}
            ),
            get_entry=AsyncMock(return_value={"entry_weight": 1}),
        )
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        raffle = {
            "id": 42,
            "guild_id": 1,
            "status": "active",
            "prize_text": "10 xanax",
            "button_join_enabled": False,
            "auto_entry_enabled": True,
            "weighted_odds_enabled": False,
            "messages_per_entry": 15,
            "auto_entry_max_per_user": 3,
        }
        embed = await FreeRaffleCog.build_raffle_embed(cog, raffle)
        info = await FreeRaffleCog._build_personal_info_embed(cog, raffle, 99)
        assert next(field.value for field in embed.fields if field.name == "BONUS ROLES") == "<@&10>: +2 Entries"
        how_it_works = next(field.value for field in info.fields if field.name == "HOW IT WORKS")
        assert info.description == "Your personal giveaway progress and eligibility."
        assert info.description not in how_it_works
        assert how_it_works.count("Every 15 qualifying messages gives 1 base entry.") == 1

    asyncio.run(_run())


def test_live_view_exposes_host_controls_button_and_handler_denies_non_host(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        repo = SimpleNamespace(get_raffle=AsyncMock(return_value={"id": 5, "host_discord_id": 10}))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        view = FreeRaffleCog.build_free_raffle_view(cog, 5, status="active", button_join_enabled=True)
        assert isinstance(view, EnterRaffleView)
        assert any(getattr(child, "custom_id", "") == "fr_host_controls:5" for child in view.children)
        interaction = SimpleNamespace(user=SimpleNamespace(id=99), response=_FakeResponse(), followup=_FakeFollowup())
        await FreeRaffleCog.handle_host_controls(cog, interaction, 5)
        assert interaction.response.sent["content"] == "Only the host can do that."
        assert interaction.response.sent["ephemeral"] is True

    asyncio.run(_run())


def test_host_controls_handler_allows_host_and_returns_ephemeral_view(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog._last_host_controls_raffles = {}
        cog.host_controls_view = lambda raffle_id, **_: HostControlsView(
            raffle_id,
            on_end_now=AsyncMock(),
            on_cancel=AsyncMock(),
            on_refresh=AsyncMock(),
            on_view_entries=AsyncMock(),
            on_reroll=AsyncMock(),
        )
        repo = SimpleNamespace(get_raffle=AsyncMock(return_value={"id": 7, "host_discord_id": 44, "status": "active"}), get_winner=AsyncMock(return_value=None))
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        interaction = SimpleNamespace(user=SimpleNamespace(id=44), response=_FakeResponse(), followup=_FakeFollowup())
        await FreeRaffleCog.handle_host_controls(cog, interaction, 7)
        assert interaction.response.sent["ephemeral"] is True
        assert isinstance(interaction.response.sent["view"], HostControlsView)
        assert "Host Controls for Giveaway #7" in interaction.response.sent["content"]

    asyncio.run(_run())


def test_thumbnail_candidate_parsing_handles_quantities_and_mixed_items():
    candidates = derive_lookup_candidates("2 Xanax + 1 Erotic DVD")
    assert candidates[:3] == ["2 Xanax + 1 Erotic DVD", "2 Xanax", "Xanax"]
    assert "Erotic DVD" in candidates
    assert derive_lookup_candidates("10 xanax") == ["10 xanax", "xanax"]


def test_draft_bonus_role_editor_rejects_duplicates_and_ignores_empty_slots():
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog._create_drafts = {5: {"role_bonus_rules": []}}
        cog.store_create_draft = FreeRaffleCog.store_create_draft.__get__(cog, FreeRaffleCog)
        cog.get_create_draft = FreeRaffleCog.get_create_draft.__get__(cog, FreeRaffleCog)
        cog.normalize_role_bonus_rules = FreeRaffleCog.normalize_role_bonus_rules.__get__(cog, FreeRaffleCog)
        cog.build_role_bonus_embed = FreeRaffleCog.build_role_bonus_embed.__get__(cog, FreeRaffleCog)
        cog.build_auto_entry_settings_embed = FreeRaffleCog.build_auto_entry_settings_embed.__get__(cog, FreeRaffleCog)
        cog._role_label = FreeRaffleCog._role_label.__get__(cog, FreeRaffleCog)
        view = DraftRoleBonusConfigView(cog, owner_id=5)
        view.slot_rules[0] = {"role_id": 100, "bonus_entries_per_qualification": 2}
        view.slot_rules[1] = {"role_id": 100, "bonus_entries_per_qualification": 3}
        duplicate_interaction = SimpleNamespace(user=SimpleNamespace(id=5), response=_FakeResponse(), guild=None)
        await view.save.callback(duplicate_interaction)
        assert duplicate_interaction.response.sent["content"] == "❌ Each bonus role can only be selected once."

        view.slot_rules[1] = {"role_id": None, "bonus_entries_per_qualification": 1}
        clean_interaction = SimpleNamespace(user=SimpleNamespace(id=5), response=_FakeResponse(), guild=None)
        await view.save.callback(clean_interaction)
        assert cog.get_create_draft(5)["role_bonus_rules"] == [
            {"role_id": 100, "bonus_entries_per_qualification": 2}
        ]

    asyncio.run(_run())


def test_resolve_thumbnail_uses_first_resolvable_candidate(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        repo = SimpleNamespace(
            get_item_meta_by_name=AsyncMock(
                side_effect=[None, {"image_url": "https://img.example/xanax.png"}, None, None]
            )
        )
        monkeypatch.setattr("cogs.free_raffle.TornItemsRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        image = await FreeRaffleCog.resolve_thumbnail(cog, "2 Xanax + 1 Erotic DVD")
        assert image == "https://img.example/xanax.png"
        assert repo.get_item_meta_by_name.await_args_list[1].args[0] == "2 Xanax"

    asyncio.run(_run())


def test_persisted_bonus_role_editor_replaces_saved_rules(monkeypatch):
    async def _run():
        from cogs.free_raffle import PersistedRoleBonusConfigView

        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog.normalize_role_bonus_rules = FreeRaffleCog.normalize_role_bonus_rules.__get__(cog, FreeRaffleCog)
        cog.build_role_bonus_embed = FreeRaffleCog.build_role_bonus_embed.__get__(cog, FreeRaffleCog)
        cog._role_label = FreeRaffleCog._role_label.__get__(cog, FreeRaffleCog)
        cog._can_manage_raffle = FreeRaffleCog._can_manage_raffle.__get__(cog, FreeRaffleCog)
        repo = SimpleNamespace(replace_role_bonus_rules=AsyncMock())
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        raffle = {"id": 8, "host_discord_id": 55}
        view = PersistedRoleBonusConfigView(cog, raffle, [{"role_id": 11, "bonus_entries_per_qualification": 2}])
        view.slot_rules[0] = {"role_id": 11, "bonus_entries_per_qualification": 4}
        view.slot_rules[1] = {"role_id": None, "bonus_entries_per_qualification": 1}
        interaction = SimpleNamespace(user=SimpleNamespace(id=55), response=_FakeResponse(), guild=None)
        await view.save.callback(interaction)
        repo.replace_role_bonus_rules.assert_awaited_once_with(8, [{"role_id": 11, "bonus_entries_per_qualification": 4}])

    asyncio.run(_run())
