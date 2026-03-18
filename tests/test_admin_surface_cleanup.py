from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import setup_panel
from setup_panel import (
    EngagementMaintenanceView,
    EngagementEventXPView,
    EngagementRolesView,
    EngagementRolesStatusView,
    StoreAdminPageView,
    StoreFulfillmentPageView,
)


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, *, ephemeral=False, embed=None, view=None):
        self.messages.append(
            {"content": content, "ephemeral": ephemeral, "embed": embed, "view": view}
        )

    async def send_modal(self, modal):
        self.messages.append({"modal": modal})


class _FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.guild_id = guild.id
        self.user = SimpleNamespace(id=1)
        self.response = _FakeResponse()
        self.client = SimpleNamespace(
            get_cog=lambda name: SimpleNamespace() if name == "StoreCog" else None
        )


class _FakeGuild:
    def __init__(self, guild_id=123, members=None):
        self.id = guild_id
        self._members = {m.id: m for m in (members or [])}

    def get_member(self, member_id):
        return self._members.get(member_id)


class _FakeRepo:
    instances = []

    def __init__(self, _pool):
        self.calls = []
        _FakeRepo.instances.append(self)

    async def list_profiles_for_guild(self, _guild_id):
        self.calls.append("list_profiles_for_guild")
        return []

    async def get_or_create_guild_settings(self, _guild_id):
        return {
            "enabled": True,
            "leaderboard_enabled": True,
            "profile_cards_enabled": True,
            "message_xp_enabled": True,
            "reaction_xp_enabled": True,
            "voice_xp_enabled": True,
            "ignored_channel_ids_json": [],
            "ignored_category_ids_json": [],
            "ignored_role_ids_json": [],
        }

    async def seed_default_reward_ladders(self, _guild_id):
        self.calls.append("seed_default_reward_ladders")


class _FakeRoleRewardService:
    instances = []

    def __init__(self, repo):
        self.repo = repo
        self.calls = []
        _FakeRoleRewardService.instances.append(self)

    async def seed_default_ladders_if_missing(self, _guild_id):
        self.calls.append("seed_default_ladders_if_missing")

    async def ensure_reward_roles(self, _guild):
        self.calls.append("ensure_reward_roles")
        return 2, 1

    async def rewards_status(self, _guild_id, _guild):
        self.calls.append("rewards_status")
        return {"total": 7, "linked": 6, "missing": 1}

    async def sync_member_roles(self, _guild, _member, _profile):
        self.calls.append("sync_member_roles")
        return {"granted": 1, "removed": 0, "failed": 0}


def _labels(view):
    return {
        getattr(child, "label", None) for child in view.children if getattr(child, "label", None)
    }


def test_removed_and_kept_command_surface_source_markers():
    engagement_src = Path("cogs/engagement.py").read_text(encoding="utf-8")
    store_src = Path("cogs/store.py").read_text(encoding="utf-8")
    raffle_src = Path("cogs/raffles.py").read_text(encoding="utf-8")

    for removed in [
        'name="debug"',
        'name="config"',
        'name="sync_roles"',
        'name="reverse_event"',
        'name="rebuild_profile"',
        'name="store_admin"',
        'name="raffle_draw"',
        'name="raffle_cancel"',
        'name="raffle_list"',
    ]:
        assert removed not in engagement_src + store_src + raffle_src

    for kept in [
        '@profile.command(name="view"',
        '@profile.command(name="rank"',
        '@profile.command(name="rewards"',
        '@tokens.command(name="balance"',
        '@tokens.command(name="history"',
        '@leaderboard.command(name="xp"',
        '@leaderboard.command(name="levels"',
        '@leaderboard.command(name="tokens"',
        '@leaderboard.command(name="jumps"',
        '@leaderboard.command(name="raffles"',
        '@app_commands.command(name="store"',
        '@app_commands.command(name="raffle_create"',
        '@raffle.command(name="controls"',
    ]:
        assert kept in engagement_src + store_src + raffle_src


def test_setup_pages_expose_expected_admin_actions():
    async def _run():
        common = dict(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(),
            panel=SimpleNamespace(),
        )
        assert _labels(EngagementRolesStatusView(**common)) >= {
            "Create/Repair Reward Roles",
            "Sync Reward Roles",
            "View Engagement Config",
            "View Reward Role Status",
        }
        assert _labels(EngagementMaintenanceView(**common)) >= {
            "Debug Member Engagement",
            "Rebuild Member Profile",
            "Reverse Event",
            "Reseed Reward Definitions",
        }
        assert _labels(StoreAdminPageView(**common)) >= {
            "Add Item",
            "Edit Item",
            "Restock Item",
            "Disable Item",
        }
        assert _labels(StoreFulfillmentPageView(**common)) >= {
            "View Pending Redemptions",
            "Fulfill Redemption",
            "Refund Redemption",
            "View Store Status",
        }

    asyncio.run(_run())




def test_engagement_roles_view_exists_and_exposes_reward_actions():
    async def _run():
        common = dict(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(),
            panel=SimpleNamespace(),
        )
        labels = _labels(EngagementRolesView(**common))
        assert labels >= {
            "Create/Repair Reward Roles",
            "Sync Reward Roles",
            "View Engagement Config",
            "View Reward Role Status",
            "Next →",
            "← Back",
        }

    asyncio.run(_run())


def test_engagement_setup_navigation_moves_between_event_roles_and_maintenance(monkeypatch):
    async def _run():
        sent = []

        async def _fake_send_or_edit(_interaction, _embed, view=None):
            sent.append(view)

        monkeypatch.setattr(setup_panel, "_send_or_edit", _fake_send_or_edit)
        common = dict(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(id=123),
            panel=SimpleNamespace(engagement_settings={}),
        )
        interaction = _FakeInteraction(_FakeGuild())

        event_view = EngagementEventXPView(**common)
        next_button = next(child for child in event_view.children if getattr(child, "label", None) == "Next →")
        await next_button.callback(interaction)
        assert isinstance(sent[-1], EngagementRolesView)

        roles_view = sent[-1]
        next_button = next(child for child in roles_view.children if getattr(child, "label", None) == "Next →")
        await next_button.callback(interaction)
        assert isinstance(sent[-1], EngagementMaintenanceView)

        maintenance_view = sent[-1]
        back_button = next(child for child in maintenance_view.children if getattr(child, "label", None) == "← Back")
        await back_button.callback(interaction)
        assert isinstance(sent[-1], EngagementRolesView)

        roles_back = next(child for child in sent[-1].children if getattr(child, "label", None) == "← Back")
        await roles_back.callback(interaction)
        assert isinstance(sent[-1], EngagementEventXPView)

    asyncio.run(_run())

def test_create_repair_reward_roles_reports_created_and_repaired(monkeypatch):
    async def _run():
        monkeypatch.setattr(setup_panel, "EngagementRepository", _FakeRepo)
        monkeypatch.setattr(setup_panel, "RoleRewardService", _FakeRoleRewardService)
        _FakeRepo.instances.clear()
        _FakeRoleRewardService.instances.clear()
        view = EngagementRolesStatusView(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(),
            panel=SimpleNamespace(),
        )
        interaction = _FakeInteraction(_FakeGuild())
        button = next(
            child
            for child in view.children
            if getattr(child, "label", None) == "Create/Repair Reward Roles"
        )
        await button.callback(interaction)
        service = _FakeRoleRewardService.instances[-1]
        assert service.calls[:2] == ["seed_default_ladders_if_missing", "ensure_reward_roles"]
        assert "Created: 2" in interaction.response.messages[-1]["content"]
        assert "Repaired: 1" in interaction.response.messages[-1]["content"]

    asyncio.run(_run())


def test_sync_reward_roles_calls_create_repair_first(monkeypatch):
    async def _run():
        monkeypatch.setattr(setup_panel, "EngagementRepository", _FakeRepo)
        monkeypatch.setattr(setup_panel, "RoleRewardService", _FakeRoleRewardService)
        _FakeRepo.instances.clear()
        _FakeRoleRewardService.instances.clear()
        view = EngagementRolesStatusView(
            owner_id=1,
            db=SimpleNamespace(pool=object()),
            settings={},
            guild=SimpleNamespace(),
            panel=SimpleNamespace(),
        )
        interaction = _FakeInteraction(_FakeGuild())
        button = next(
            child for child in view.children if getattr(child, "label", None) == "Sync Reward Roles"
        )
        await button.callback(interaction)
        service = _FakeRoleRewardService.instances[-1]
        assert service.calls[:2] == ["seed_default_ladders_if_missing", "ensure_reward_roles"]
        assert "Member sync totals" in interaction.response.messages[-1]["content"]

    asyncio.run(_run())


def test_raffle_controls_command_and_panel_source_markers_exist():
    raffle_src = Path("cogs/raffles.py").read_text(encoding="utf-8")
    assert 'raffle = app_commands.Group(name="raffle"' in raffle_src
    assert '@raffle.command(name="controls"' in raffle_src
    assert "class RaffleControlsView" in raffle_src
    assert "Refresh Active Raffles" in raffle_src
    assert "View Active Raffles" in raffle_src
