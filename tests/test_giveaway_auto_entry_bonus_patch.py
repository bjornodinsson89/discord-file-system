from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.engagement import EngagementCog
from cogs.free_raffle import (
    AutoEntryRoleBonusManageView,
    AutoEntrySettingsModal,
    DraftRoleBonusRemovalView,
    FreeRaffleCog,
    FreeRaffleModal,
    PersistedRoleBonusRemovalView,
)
from repositories.free_raffle_repo import FreeRaffleRepository


class _FakeResponse:
    def __init__(self):
        self.sent = None
        self.modal = None
        self.deferred = False

    def is_done(self):
        return self.sent is not None or self.modal is not None or self.deferred

    async def send_message(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = True

    async def edit_message(self, content=None, *, embed=None, view=None):
        self.sent = {"content": content, "embed": embed, "view": view, "edited": True}


class _FakeFollowup:
    def __init__(self):
        self.sent = None

    async def send(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.sent = {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}


def test_creation_modal_uses_allowed_entries_per_user_label():
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        modal = FreeRaffleModal(cog)
        assert modal.allowed_entries_per_user.label == "Allowed Entries Per User"
        labels = [child.label for child in modal.children]
        assert "Allowed Entries Per User" in labels
        assert "Auto-entry max per user" not in labels

    asyncio.run(_run())


def test_merge_role_bonus_rule_updates_one_rule_per_role():
    cog = FreeRaffleCog.__new__(FreeRaffleCog)
    rules = cog.merge_role_bonus_rule(
        [{"role_id": 123, "bonus_entries_per_qualification": 1}],
        role_id=456,
        bonus_entries=4,
    )
    assert rules == [
        {"role_id": 123, "bonus_entries_per_qualification": 1},
        {"role_id": 456, "bonus_entries_per_qualification": 4},
    ]
    assert cog.parse_positive_int("15", label="Messages Per Entry") == 15


def test_auto_entry_settings_modal_only_asks_for_messages_per_entry():
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        modal = AutoEntrySettingsModal(
            cog,
            owner_id=1,
            draft={"messages_per_entry": 12, "auto_entry_max_per_user": 5, "role_bonus_rules": []},
        )
        labels = [child.label for child in modal.children]
        assert labels == ["Messages Per Entry"]
        assert all("Max" not in label for label in labels)

    asyncio.run(_run())


def test_info_embed_shows_messages_per_entry_and_matching_bonus_roles(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        vip_role = SimpleNamespace(id=10, mention="<@&10>")
        elite_role = SimpleNamespace(id=20, mention="<@&20>")
        guild = SimpleNamespace(
            roles=[vip_role, elite_role],
            get_member=lambda _uid: SimpleNamespace(
                guild=SimpleNamespace(roles=[vip_role, elite_role]), roles=[vip_role]
            ),
        )
        cog.bot = SimpleNamespace(get_guild=lambda _gid: guild)
        cog._get_coin_balance = AsyncMock(return_value=2)
        repo = SimpleNamespace(
            get_auto_entry_progress=AsyncMock(
                return_value={"qualifying_message_count": 3, "auto_entries_granted": 2}
            ),
            get_entry=AsyncMock(return_value={"entry_weight": 2}),
            list_role_bonus_rules=AsyncMock(
                return_value=[
                    {"role_id": 10, "bonus_entries_per_qualification": 1},
                    {"role_id": 20, "bonus_entries_per_qualification": 2},
                ]
            ),
        )
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        embed = await FreeRaffleCog._build_personal_info_embed(
            cog,
            {
                "id": 7,
                "guild_id": 1,
                "status": "active",
                "auto_entry_enabled": True,
                "button_join_enabled": False,
                "weighted_odds_enabled": False,
                "auto_entry_max_per_user": 5,
                "messages_per_entry": 12,
            },
            99,
        )
        how_it_works = next(field.value for field in embed.fields if field.name == "HOW IT WORKS")
        bonus_roles = next(field.value for field in embed.fields if field.name == "BONUS ROLES")
        progress = next(field.value for field in embed.fields if field.name == "YOUR PROGRESS")
        assert "Every 12 qualifying messages gives 1 base entry." in how_it_works
        assert "<@&10>: **+1 Entry**" in bonus_roles
        assert "<@&20>: **+2 Entries**" in bonus_roles
        assert "Your bonus this cycle: **+1 Entries**" in progress
        assert "Messages toward next entry: **3 / 12**" in progress

    asyncio.run(_run())


def test_auto_entry_processing_passes_member_roles_to_repo(monkeypatch):
    async def _run():
        cog = EngagementCog.__new__(EngagementCog)
        cog.repo = SimpleNamespace(
            get_or_create_guild_settings=AsyncMock(
                return_value={"auto_entry_giveaways_enabled": True}
            ),
            get_or_create_profile=AsyncMock(return_value={"prize_token_balance": 2, "level": 5}),
        )
        cog.role_rewards = SimpleNamespace(giveaway_weight_for_level=lambda level: level + 1)
        raffle_repo = SimpleNamespace(
            list_active_auto_entry_raffles=AsyncMock(return_value=[{"id": 1}]),
            increment_auto_entry_progress=AsyncMock(
                return_value={"awarded": True, "entries_granted": 3}
            ),
        )
        monkeypatch.setattr("cogs.engagement.FreeRaffleRepository", lambda _pool: raffle_repo)
        monkeypatch.setattr("cogs.engagement.get_pool", lambda: object())
        awarded = await EngagementCog._process_message_auto_entries(cog, 1, 55, role_ids=[10, 20])
        assert awarded == 3
        assert raffle_repo.increment_auto_entry_progress.await_args.kwargs["member_role_ids"] == [
            10,
            20,
        ]

    asyncio.run(_run())


def test_host_admin_role_bonus_view_supports_edit_and_remove(monkeypatch):
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        raffle = {
            "id": 77,
            "host_discord_id": 5,
            "messages_per_entry": 15,
            "auto_entry_max_per_user": 4,
        }
        view = AutoEntryRoleBonusManageView(
            cog, raffle, [{"role_id": 123, "bonus_entries_per_qualification": 2}]
        )
        view.selected_role_id = 123
        interaction = SimpleNamespace(user=SimpleNamespace(id=5), response=_FakeResponse())
        await view.edit_limits.callback(interaction)
        assert interaction.response.modal is not None

        repo = SimpleNamespace(
            list_role_bonus_rules=AsyncMock(
                return_value=[{"role_id": 123, "bonus_entries_per_qualification": 2}]
            )
        )
        monkeypatch.setattr("cogs.free_raffle.FreeRaffleRepository", lambda _pool: repo)
        monkeypatch.setattr("cogs.free_raffle.get_pool", lambda: object())
        remove_interaction = SimpleNamespace(
            user=SimpleNamespace(id=5),
            response=_FakeResponse(),
            guild=SimpleNamespace(get_role=lambda role_id: SimpleNamespace(id=role_id, name="VIP")),
        )
        await view.remove.callback(remove_interaction)
        assert "Role Bonuses for Giveaway #77" == remove_interaction.response.sent["embed"].title
        assert isinstance(remove_interaction.response.sent["view"], PersistedRoleBonusRemovalView)

    asyncio.run(_run())


def test_draft_role_bonus_removal_updates_draft():
    async def _run():
        cog = FreeRaffleCog.__new__(FreeRaffleCog)
        cog._create_drafts = {
            5: {
                "messages_per_entry": 15,
                "auto_entry_max_per_user": 4,
                "role_bonus_rules": [{"role_id": 123, "bonus_entries_per_qualification": 2}],
            }
        }
        view = DraftRoleBonusRemovalView(
            cog,
            owner_id=5,
            bonus_rules=[{"role_id": 123, "bonus_entries_per_qualification": 2}],
        )
        interaction = SimpleNamespace(user=SimpleNamespace(id=5), response=_FakeResponse())
        await view.remove_selected(interaction, 123)
        assert cog.get_create_draft(5)["role_bonus_rules"] == []
        assert interaction.response.sent["embed"].title == "Auto Entry Settings"

    asyncio.run(_run())


def test_create_summary_shows_new_values_clearly():
    cog = FreeRaffleCog.__new__(FreeRaffleCog)
    embed = cog.build_create_summary_embed(
        {
            "prize_text": "VIP Crate",
            "duration_days": 3,
            "note_text": "Weekend drop",
            "auto_entry_max_per_user": 4,
            "messages_per_entry": 12,
            "role_bonus_rules": [{"role_id": 123, "bonus_entries_per_qualification": 2}],
        },
        mode_key="auto",
        channel_id=999,
    )
    summary = embed.fields[0].value
    assert "Allowed Entries Per User: **4**" in summary
    assert "Messages Per Entry: **12**" in summary
    assert "Role Bonuses:" in summary


def test_touched_giveaway_ui_uses_entries_wording_only():
    src = open("cogs/free_raffle.py", encoding="utf-8").read()
    assert "Entrants" not in src


def test_source_uses_entries_language_only():
    src = (
        open("cogs/free_raffle.py", encoding="utf-8").read()
        + open("views/free_raffle_views.py", encoding="utf-8").read()
    )
    assert "Entrants" not in src


def test_migration_adds_message_threshold_and_role_bonus_table():
    src = open("migrations/2026_03_20_giveaway_auto_entry_bonus_rules.sql", encoding="utf-8").read()
    assert "messages_per_entry INTEGER NOT NULL DEFAULT 15" in src
    assert "CREATE TABLE IF NOT EXISTS public.free_raffle_role_bonuses" in src


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, *, raffle, balance=1, progress=None, bonus_rows=None, dedupe_allows=True):
        self.raffle = dict(raffle)
        self.balance = balance
        self.progress = progress or {
            "raffle_id": int(raffle["id"]),
            "guild_id": int(raffle["guild_id"]),
            "user_id": 55,
            "qualifying_message_count": 0,
            "auto_entries_granted": 0,
        }
        self.bonus_rows = list(bonus_rows or [])
        self.dedupe_allows = dedupe_allows

    def transaction(self):
        return _FakeTransaction()

    async def fetchrow(self, query, *args):
        if "FROM free_raffles" in query:
            return self.raffle
        if "SELECT prize_token_balance" in query:
            return {"prize_token_balance": self.balance}
        if "INSERT INTO giveaway_auto_progress" in query:
            self.progress["qualifying_message_count"] += int(args[3])
            return dict(self.progress)
        return None

    async def fetch(self, query, *args):
        if "FROM free_raffle_role_bonuses" in query:
            return self.bonus_rows
        return []

    async def execute(self, query, *args):
        if "INSERT INTO engagement_profiles" in query:
            return "INSERT 0 1"
        if "UPDATE giveaway_auto_progress" in query:
            self.progress["qualifying_message_count"] = int(args[3])
            self.progress["auto_entries_granted"] = int(args[4])
            self.progress["last_award_dedupe_key"] = args[5]
            return "UPDATE 1"
        if "INSERT INTO free_raffle_entries" in query:
            return "INSERT 0 1" if self.dedupe_allows else "INSERT 0 0"
        return "OK"


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _repo_with_conn(conn):
    repo = FreeRaffleRepository.__new__(FreeRaffleRepository)
    repo.acquire = lambda: _AcquireCtx(conn)
    return repo


def test_increment_auto_entry_progress_grants_base_and_stacked_bonus_entries():
    async def _run():
        conn = _FakeConn(
            raffle={
                "id": 1,
                "guild_id": 1,
                "auto_entry_enabled": True,
                "status": "active",
                "messages_per_entry": 15,
                "auto_entry_max_per_user": 10,
            },
            progress={
                "raffle_id": 1,
                "guild_id": 1,
                "user_id": 55,
                "qualifying_message_count": 14,
                "auto_entries_granted": 0,
            },
            bonus_rows=[
                {"role_id": 10, "bonus_entries_per_qualification": 1},
                {"role_id": 20, "bonus_entries_per_qualification": 2},
            ],
        )
        repo = _repo_with_conn(conn)
        result = await FreeRaffleRepository.increment_auto_entry_progress(
            repo,
            guild_id=1,
            raffle_id=1,
            user_id=55,
            entry_weight=1,
            member_role_ids=[10, 20],
            qualifying_messages=1,
            progress_dedupe_key="k",
        )
        assert result["entries_granted"] == 4
        assert result["auto_entries_granted"] == 4
        assert result["qualifying_message_count"] == 0

    asyncio.run(_run())


def test_increment_auto_entry_progress_applies_base_entry_without_bonus_and_enforces_cap():
    async def _run():
        conn = _FakeConn(
            raffle={
                "id": 2,
                "guild_id": 1,
                "auto_entry_enabled": True,
                "status": "active",
                "messages_per_entry": 12,
                "auto_entry_max_per_user": 5,
            },
            progress={
                "raffle_id": 2,
                "guild_id": 1,
                "user_id": 55,
                "qualifying_message_count": 11,
                "auto_entries_granted": 4,
            },
            bonus_rows=[{"role_id": 10, "bonus_entries_per_qualification": 1}],
        )
        repo = _repo_with_conn(conn)
        result = await FreeRaffleRepository.increment_auto_entry_progress(
            repo,
            guild_id=1,
            raffle_id=2,
            user_id=55,
            entry_weight=1,
            member_role_ids=[10],
            qualifying_messages=1,
            progress_dedupe_key="k2",
        )
        assert result["entries_granted"] == 1
        assert result["auto_entries_granted"] == 5
        assert result["qualifying_message_count"] == 0

    asyncio.run(_run())
