from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import setup_panel
from setup_panel import (
    ChannelsHubView,
    ConfirmActionView,
    MaintenanceHubView,
    SetupPanelView,
    StoreHubView,
    send_setup_panel,
)


class _FakeResponse:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modals = []
        self._done = False

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self._done = True
        self.sent.append({"content": content, "embed": embed, "view": view, "ephemeral": ephemeral})

    async def edit_message(self, *, embed=None, view=None):
        self._done = True
        self.edits.append({"embed": embed, "view": view})

    async def send_modal(self, modal):
        self._done = True
        self.modals.append(modal)

    def is_done(self):
        return self._done


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, embed=None, ephemeral=False):
        self.sent.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _Role:
    def __init__(self, role_id, mention):
        self.id = role_id
        self.mention = mention


class _Guild:
    def __init__(self):
        self.id = 123
        self.owner_id = 999
        self.channels = {}
        self.roles = {7: _Role(7, "<@&7>")}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return self.roles.get(role_id)


class _FakeGuildSettingsRepo:
    def __init__(self, _db):
        pass

    async def ensure_guild_exists(self, _guild_id):
        return None


async def _fake_ensure_setup_permission(_interaction, _db):
    return True, {"admin_role_ids": [7], "welcome_channel_id": None}


class _FakeEngagementRepo:
    def __init__(self, _pool):
        pass

    async def get_or_create_guild_settings(self, _guild_id):
        return {"enabled": True, "auto_entry_giveaways_enabled": True}


class _FakeStoreRepo:
    def __init__(self, _pool):
        pass

    async def get_or_create_guild_settings(self, _guild_id):
        return {"store_channel_id": 555, "enabled": True}


def _build_panel():
    guild = _Guild()
    return SetupPanelView(
        owner_id=1,
        db=SimpleNamespace(pool=None),
        settings={"admin_role_ids": [7]},
        guild=guild,
        engagement_settings={"enabled": True, "auto_entry_giveaways_enabled": True},
    )


def test_setup_dashboard_embed_and_sections():
    async def _run():
        panel = _build_panel()
        panel.store_settings = {"store_channel_id": 555, "enabled": True}

        embed = panel._build_embed()
        labels = [child.label for child in panel.children if getattr(child, "label", None)]

        assert embed.title.endswith("Admin Control Center")
        assert labels[:8] == [
        "Channels",
        "Roles",
        "Engagement",
        "Raffles",
        "Giveaways",
        "Store",
        "Welcome",
        "Maintenance",
    ]
        assert "Close" in labels

    asyncio.run(_run())


def test_setup_entrypoint_source_uses_send_setup_panel():
    src = Path("cogs/events.py").read_text(encoding="utf-8")
    assert '@bot.tree.command(name="setup", description="Open the interactive server setup panel")' in src
    assert "await send_setup_panel(interaction, db)" in src


def test_section_views_stay_within_component_limits():
    async def _run():
        panel = _build_panel()
        panel.store_settings = {"store_channel_id": 555, "enabled": True}
        views = [
            panel,
            ChannelsHubView(owner_id=1, db=SimpleNamespace(pool=None), settings={}, guild=panel.guild, panel=panel),
            StoreHubView(owner_id=1, db=SimpleNamespace(pool=None), settings={}, guild=panel.guild, panel=panel),
            MaintenanceHubView(owner_id=1, db=SimpleNamespace(pool=None), settings={}, guild=panel.guild, panel=panel),
        ]
        for view in views:
            rows = {}
            for child in view.children:
                rows.setdefault(getattr(child, "row", None) or 0, 0)
                rows[getattr(child, "row", None) or 0] += 1
            assert all(count <= 5 for count in rows.values())
            assert len(view.children) <= 25

    asyncio.run(_run())


def test_plain_english_labels_and_entries_wording():
    setup_src = Path("setup_panel.py").read_text(encoding="utf-8")
    giveaway_src = Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    view_src = Path("views/free_raffle_views.py").read_text(encoding="utf-8")

    assert "Store Channel" in setup_src
    assert "Reward Roles" in setup_src
    assert "Repair Roles" in setup_src
    assert "Entrants" not in giveaway_src
    assert "Entrants" not in view_src
    assert "View Entries" in view_src


def test_back_and_home_navigation(monkeypatch):
    async def _run():
        panel = _build_panel()
        panel.store_settings = {"store_channel_id": 555, "enabled": True}
        sent = []

        async def _fake_send_or_edit(_interaction, embed, view=None):
            sent.append((embed.title, view.__class__.__name__))

        monkeypatch.setattr(setup_panel, "_send_or_edit", _fake_send_or_edit)
        interaction = SimpleNamespace(response=_FakeResponse(), guild=panel.guild, guild_id=123, user=SimpleNamespace(id=1))

        channels = next(child for child in panel.children if getattr(child, "label", None) == "Channels")
        await channels.callback(interaction)
        hub = ChannelsHubView(owner_id=1, db=SimpleNamespace(pool=None), settings={}, guild=panel.guild, panel=panel)
        back = next(child for child in hub.children if getattr(child, "label", None) == "Back")
        home = next(child for child in hub.children if getattr(child, "label", None) == "Home")
        await back.callback(interaction)
        await home.callback(interaction)

        assert sent[0][0].endswith("Channels")
        assert sent[1][0].endswith("Admin Control Center")
        assert sent[2][0].endswith("Admin Control Center")

    asyncio.run(_run())


def test_dangerous_actions_require_confirmation(monkeypatch):
    async def _run():
        panel = _build_panel()
        interaction = SimpleNamespace(response=_FakeResponse(), guild=panel.guild, guild_id=123, user=SimpleNamespace(id=1))
        sent = []

        async def _fake_send_or_edit(_interaction, embed, view=None):
            sent.append((embed.title, view))

        monkeypatch.setattr(setup_panel, "_send_or_edit", _fake_send_or_edit)
        maintenance = MaintenanceHubView(owner_id=1, db=SimpleNamespace(pool=None), settings={}, guild=panel.guild, panel=panel)
        rebuild_tools = next(child for child in maintenance.children if getattr(child, "label", None) == "Rebuild Tools")
        await rebuild_tools.callback(interaction)
        confirm_view = sent[-1][1]
        assert isinstance(confirm_view, setup_panel.RebuildToolsView)
        rebuild_profile = next(child for child in confirm_view.children if getattr(child, "label", None) == "Rebuild Profile")
        await rebuild_profile.callback(interaction)
        assert isinstance(sent[-1][1], ConfirmActionView)

    asyncio.run(_run())


def test_send_setup_panel_launches_admin_control_center(monkeypatch):
    async def _run():
        monkeypatch.setattr(setup_panel, "GuildSettingsRepository", _FakeGuildSettingsRepo)
        monkeypatch.setattr(setup_panel, "ensure_setup_permission", _fake_ensure_setup_permission)
        monkeypatch.setattr(setup_panel, "EngagementRepository", _FakeEngagementRepo)
        monkeypatch.setattr(setup_panel, "StoreRepository", _FakeStoreRepo)
        guild = _Guild()
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=guild.id,
            user=SimpleNamespace(id=1),
            response=_FakeResponse(),
        )
        await send_setup_panel(interaction, SimpleNamespace(pool=None))
        message = interaction.response.sent[0]
        assert message["embed"].title.endswith("Admin Control Center")
        assert isinstance(message["view"], SetupPanelView)

    asyncio.run(_run())
