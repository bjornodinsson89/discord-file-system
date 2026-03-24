import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import setup_panel
from setup_panel import (
    EngagementRolesView,
    _respond_callback_error,
    AdminKeyModeSelect,
    SingleAdminKeySelect,
    ChannelsAlertsAccessView,
    AdminKeySettingsView,
    AdminKeySettingsModeSelect,
    AdminKeySettingsAddPoolMemberSelect,
    AdminKeySettingsRemovePoolMemberSelect,
    AdminKeySettingsSingleAdminSelect,
)


class _Response:
    def __init__(self, *, done: bool = False):
        self._done = done
        self.deferred = []
        self.messages = []

    async def defer(self, *, ephemeral=False, thinking=False):
        self._done = True
        self.deferred.append({"ephemeral": ephemeral, "thinking": thinking})

    async def send_message(self, content=None, *, embed=None, ephemeral=False):
        self._done = True
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})

    def is_done(self):
        return self._done


class _Followup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, ephemeral=False):
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral})


class _FakeRepo:
    def __init__(self, _pool):
        self.pool = _pool

    async def list_profiles_for_guild(self, _guild_id):
        return [{"user_id": 1001}, {"user_id": 1002}]


class _FakeService:
    def __init__(self, repo):
        self.repo = repo

    async def seed_default_ladders_if_missing(self, _guild_id):
        return None

    async def ensure_reward_roles(self, _guild):
        return 1, 2

    async def rewards_status(self, _guild_id, _guild):
        return {"linked": 3, "total": 4, "missing": 1}

    async def sync_member_roles(self, _guild, member, _profile):
        return {"granted": member.sync_value, "removed": 0, "failed": 0}


class _Guild:
    def __init__(self):
        self.id = 77
        self._members = {
            1001: SimpleNamespace(id=1001, sync_value=2),
            1002: SimpleNamespace(id=1002, sync_value=3),
        }

    def get_member(self, member_id):
        return self._members.get(member_id)


def _build_interaction(*, response_done: bool = False):
    return SimpleNamespace(
        guild=_Guild(),
        guild_id=77,
        user=SimpleNamespace(id=55),
        response=_Response(done=response_done),
        followup=_Followup(),
    )


def _build_view():
    return EngagementRolesView(
        owner_id=55,
        db=SimpleNamespace(pool=object()),
        settings={},
        guild=_Guild(),
        panel=SimpleNamespace(),
    )


async def _run_sync_reward_roles(monkeypatch):
    monkeypatch.setattr(setup_panel, "EngagementRepository", _FakeRepo)
    monkeypatch.setattr(setup_panel, "RoleRewardService", _FakeService)

    interaction = _build_interaction()
    view = _build_view()
    button = next(
        child for child in view.children if getattr(child, "label", None) == "Sync Reward Roles"
    )

    await button.callback(interaction)

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": False}]
    assert interaction.response.messages == []
    assert len(interaction.followup.messages) == 1
    followup = interaction.followup.messages[0]
    assert followup["ephemeral"] is True
    assert "Reward role sync completed." in followup["content"]
    assert "'granted': 5" in followup["content"]


async def _run_callback_error_when_done():
    interaction = _build_interaction(response_done=True)

    await _respond_callback_error(interaction, RuntimeError("boom"), "setup_callback_error")

    assert interaction.response.messages == []
    assert len(interaction.followup.messages) == 1
    assert interaction.followup.messages[0]["ephemeral"] is True
    assert interaction.followup.messages[0]["embed"].title.endswith("Setup failed")


async def _run_callback_error_when_not_done():
    interaction = _build_interaction(response_done=False)

    await _respond_callback_error(interaction, RuntimeError("boom"), "setup_callback_error")

    assert interaction.followup.messages == []
    assert len(interaction.response.messages) == 1
    assert interaction.response.messages[0]["ephemeral"] is True
    assert interaction.response.messages[0]["embed"].title.endswith("Setup failed")


def test_sync_reward_roles_defers_then_uses_followup(monkeypatch):
    asyncio.run(_run_sync_reward_roles(monkeypatch))


def test_setup_callback_error_uses_followup_after_acknowledgement():
    asyncio.run(_run_callback_error_when_done())


def test_setup_callback_error_uses_initial_response_before_acknowledgement():
    asyncio.run(_run_callback_error_when_not_done())


async def _run_save_store_changes_uses_store_repository_and_syncs(monkeypatch):
    fake_store_repo = SimpleNamespace(
        upsert_guild_settings=AsyncMock(return_value={"store_channel_id": 4321}),
    )
    fake_store_cog = SimpleNamespace(sync_storefront=AsyncMock())
    monkeypatch.setattr(setup_panel, "_send_or_edit", AsyncMock())

    panel = setup_panel.SetupPanelView(
        owner_id=55,
        db=SimpleNamespace(pool=object()),
        settings={},
        guild=SimpleNamespace(id=77),
    )
    panel.store_repo = fake_store_repo
    monkeypatch.setattr(panel, "_build_embed", lambda: "embed")

    interaction = SimpleNamespace(
        guild_id=77,
        guild=SimpleNamespace(id=77),
        user=SimpleNamespace(id=55),
        client=SimpleNamespace(get_cog=lambda name: fake_store_cog if name == "StoreCog" else None),
        response=_Response(done=False),
        followup=_Followup(),
    )

    await panel.save_store_changes(interaction, {"store_channel_id": 4321})

    fake_store_repo.upsert_guild_settings.assert_awaited_once_with(77, store_channel_id=4321)
    fake_store_cog.sync_storefront.assert_awaited_once_with(interaction.guild)
    assert panel.store_settings["store_channel_id"] == 4321


def test_setup_save_store_changes_uses_store_repository_and_syncs(monkeypatch):
    asyncio.run(_run_save_store_changes_uses_store_repository_and_syncs(monkeypatch))


async def _run_setup_summary_renders_store_channel_from_guild_settings():
    panel = setup_panel.SetupPanelView(
        owner_id=55,
        db=SimpleNamespace(pool=None),
        settings={},
        guild=SimpleNamespace(),
    )
    panel.store_settings = {"store_channel_id": 4321}

    embed = panel._build_embed()
    store_field = next(field for field in embed.fields if field.name == "Store")

    assert "Store channel: `4321`" in store_field.value


def test_setup_summary_renders_store_channel_from_guild_settings():
    asyncio.run(_run_setup_summary_renders_store_channel_from_guild_settings())


class _PanelGuild:
    def __init__(self):
        self.owner_id = 900
        self.members = [
            SimpleNamespace(
                id=900,
                display_name="Owner",
                guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
                roles=[],
            ),
            SimpleNamespace(
                id=901,
                display_name="Admin",
                guild_permissions=SimpleNamespace(administrator=True, manage_guild=False),
                roles=[],
            ),
            SimpleNamespace(
                id=902,
                display_name="SetupRole",
                guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
                roles=[SimpleNamespace(id=77)],
            ),
        ]

    def get_member(self, member_id):
        return next((member for member in self.members if member.id == member_id), None)


async def _run_admin_key_mode_select_saves_single(monkeypatch):
    panel = SimpleNamespace(settings={"admin_key_strategy": "pool"}, save_changes=AsyncMock())
    select = AdminKeyModeSelect(panel)
    interaction = _build_interaction()
    select._values = ["single"]

    await select.callback(interaction)

    panel.save_changes.assert_awaited_once_with(interaction, {"admin_key_strategy": "single"})


async def _run_single_admin_key_select_saves_selected_admin(monkeypatch):
    guild = _PanelGuild()
    panel = setup_panel.SetupPanelView(
        owner_id=55,
        db=SimpleNamespace(pool=None),
        settings={"admin_role_ids": [77], "admin_key_strategy": "single"},
        guild=guild,
    )
    panel.save_changes = AsyncMock()
    select = SingleAdminKeySelect(panel)
    interaction = _build_interaction()
    select._values = ["902"]

    await select.callback(interaction)

    panel.save_changes.assert_awaited_once_with(
        interaction, {"admin_key_single_discord_id": 902, "admin_key_strategy": "single"}
    )


def test_admin_key_mode_select_saves_single(monkeypatch):
    asyncio.run(_run_admin_key_mode_select_saves_single(monkeypatch))


def test_single_admin_key_select_saves_selected_admin(monkeypatch):
    asyncio.run(_run_single_admin_key_select_saves_selected_admin(monkeypatch))


class _SetupGuild:
    def __init__(self):
        self.id = 77
        self.owner_id = 900
        self.members = [
            SimpleNamespace(
                id=900,
                display_name="Owner",
                mention="<@900>",
                guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
                roles=[],
            ),
            SimpleNamespace(
                id=901,
                display_name="Admin",
                mention="<@901>",
                guild_permissions=SimpleNamespace(administrator=True, manage_guild=False),
                roles=[],
            ),
            SimpleNamespace(
                id=902,
                display_name="SetupRole",
                mention="<@902>",
                guild_permissions=SimpleNamespace(administrator=False, manage_guild=False),
                roles=[SimpleNamespace(id=77)],
            ),
        ]

    def get_member(self, member_id):
        return next((member for member in self.members if member.id == member_id), None)

    def get_channel(self, _channel_id):
        return None

    def get_role(self, _role_id):
        return None


def _build_setup_panel(*, strategy="pool", selected_admin=None):
    settings = {
        "admin_role_ids": [77],
        "admin_key_strategy": strategy,
        "admin_key_single_discord_id": selected_admin,
        "admin_key_pool_member_ids": [901],
    }
    return setup_panel.SetupPanelView(
        owner_id=55, db=SimpleNamespace(pool=None), settings=settings, guild=_SetupGuild()
    )


def _row_widths(view):
    widths = {}
    for child in view.children:
        row = child.row if child.row is not None else 0
        widths[row] = widths.get(row, 0) + child.width
    return widths


async def _run_alerts_access_view_constructs_without_row_overflow():
    panel = _build_setup_panel()
    view = ChannelsAlertsAccessView(
        owner_id=55, db=panel.db, settings=panel.settings, guild=panel.guild, panel=panel
    )

    assert view is not None
    assert all(width <= 5 for width in _row_widths(view).values())
    assert any(getattr(child, "label", None) == "Admin Key Settings" for child in view.children)


async def _run_admin_key_settings_ui_opens_from_alerts_page(monkeypatch):
    panel = _build_setup_panel()
    interaction = _build_interaction()
    sent = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    view = ChannelsAlertsAccessView(
        owner_id=55, db=panel.db, settings=panel.settings, guild=panel.guild, panel=panel
    )
    button = next(
        child for child in view.children if getattr(child, "label", None) == "Admin Key Settings"
    )

    await button.callback(interaction)

    _, embed, rendered_view = sent.await_args.args
    assert embed.title.endswith("Admin Key Settings")
    assert isinstance(rendered_view, AdminKeySettingsView)


async def _run_admin_key_settings_pool_mode_save(monkeypatch):
    panel = _build_setup_panel(strategy="single", selected_admin=902)
    interaction = _build_interaction()
    panel.save_changes = AsyncMock(side_effect=lambda _i, changes: panel.settings.update(changes))
    sent = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    select = AdminKeySettingsModeSelect(panel)
    select._values = ["pool"]

    await select.callback(interaction)

    panel.save_changes.assert_awaited_once_with(
        interaction, {"admin_key_strategy": "pool", "admin_key_single_discord_id": None}
    )
    _, _, rendered_view = sent.await_args.args
    assert isinstance(rendered_view, AdminKeySettingsView)
    assert panel.settings["admin_key_strategy"] == "pool"
    assert panel.settings["admin_key_single_discord_id"] is None


async def _run_admin_key_settings_single_mode_save(monkeypatch):
    panel = _build_setup_panel(strategy="pool")
    interaction = _build_interaction()
    panel.save_changes = AsyncMock(side_effect=lambda _i, changes: panel.settings.update(changes))
    sent = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    select = AdminKeySettingsModeSelect(panel)
    select._values = ["single"]

    await select.callback(interaction)

    panel.save_changes.assert_awaited_once_with(interaction, {"admin_key_strategy": "single"})
    _, _, rendered_view = sent.await_args.args
    assert isinstance(rendered_view, AdminKeySettingsView)
    assert panel.settings["admin_key_strategy"] == "single"


async def _run_admin_key_settings_selected_admin_save(monkeypatch):
    panel = _build_setup_panel(strategy="single")
    interaction = _build_interaction()
    panel.save_changes = AsyncMock(side_effect=lambda _i, changes: panel.settings.update(changes))
    sent = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    select = AdminKeySettingsSingleAdminSelect(panel)
    select._values = ["902"]

    await select.callback(interaction)

    panel.save_changes.assert_awaited_once_with(
        interaction, {"admin_key_single_discord_id": 902, "admin_key_strategy": "single"}
    )
    _, _, rendered_view = sent.await_args.args
    assert isinstance(rendered_view, AdminKeySettingsView)
    assert panel.settings["admin_key_single_discord_id"] == 902


async def _run_setup_views_respect_row_width_limits():
    pool_panel = _build_setup_panel()
    single_panel = _build_setup_panel(strategy="single")
    views = [
        ChannelsAlertsAccessView(
            owner_id=55,
            db=pool_panel.db,
            settings=pool_panel.settings,
            guild=pool_panel.guild,
            panel=pool_panel,
        ),
        AdminKeySettingsView(
            owner_id=55,
            db=pool_panel.db,
            settings=pool_panel.settings,
            guild=pool_panel.guild,
            panel=pool_panel,
        ),
        AdminKeySettingsView(
            owner_id=55,
            db=single_panel.db,
            settings=single_panel.settings,
            guild=single_panel.guild,
            panel=single_panel,
        ),
    ]

    for view in views:
        assert all(width <= 5 for width in _row_widths(view).values())


async def _run_admin_key_settings_add_pool_member(monkeypatch):
    panel = _build_setup_panel(strategy="pool")
    panel.db = SimpleNamespace(pool=object())
    interaction = _build_interaction()
    sent = AsyncMock()
    add_member = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    monkeypatch.setattr(
        setup_panel.GuildSettingsRepository, "add_admin_key_pool_member", add_member
    )
    select = AdminKeySettingsAddPoolMemberSelect(panel)
    select._values = [panel.guild.get_member(902)]

    await select.callback(interaction)

    add_member.assert_awaited_once_with(panel.guild.id, 902)
    assert panel.settings["admin_key_pool_member_ids"] == [901, 902]


async def _run_admin_key_settings_remove_pool_member(monkeypatch):
    panel = _build_setup_panel(strategy="pool")
    panel.db = SimpleNamespace(pool=object())
    interaction = _build_interaction()
    sent = AsyncMock()
    remove_member = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    monkeypatch.setattr(
        setup_panel.GuildSettingsRepository, "remove_admin_key_pool_member", remove_member
    )
    select = AdminKeySettingsRemovePoolMemberSelect(panel)
    select._values = ["901"]

    await select.callback(interaction)

    remove_member.assert_awaited_once_with(panel.guild.id, 901)
    assert panel.settings["admin_key_pool_member_ids"] == []


async def _run_admin_key_settings_embed_shows_pool_members():
    panel = _build_setup_panel(strategy="pool")
    embed = setup_panel._admin_key_settings_embed(panel)

    assert "<@901>" in embed.fields[0].value


async def _run_admin_key_settings_back_and_home_navigation(monkeypatch):
    panel = _build_setup_panel(strategy="single")
    interaction = _build_interaction()
    sent = AsyncMock()
    monkeypatch.setattr(setup_panel, "_send_or_edit", sent)
    view = AdminKeySettingsView(
        owner_id=55, db=panel.db, settings=panel.settings, guild=panel.guild, panel=panel
    )

    back_button = next(
        child
        for child in view.children
        if getattr(child, "label", None) == "Back to Alerts & Access"
    )
    await back_button.callback(interaction)
    _, back_embed, back_view = sent.await_args.args
    assert back_embed.title.endswith("Alerts & Access")
    assert isinstance(back_view, ChannelsAlertsAccessView)
    assert "Pool Members" in back_embed.fields[0].value

    sent.reset_mock()
    home_button = next(child for child in view.children if getattr(child, "label", None) == "Home")
    await home_button.callback(interaction)
    _, home_embed, home_view = sent.await_args.args
    assert home_embed.title.endswith("Admin Control Center")
    assert home_view is panel


def test_admin_key_settings_embed_shows_pool_members():
    asyncio.run(_run_admin_key_settings_embed_shows_pool_members())


def test_alerts_access_view_constructs_without_row_overflow():
    asyncio.run(_run_alerts_access_view_constructs_without_row_overflow())


def test_admin_key_settings_ui_opens_from_alerts_page(monkeypatch):
    asyncio.run(_run_admin_key_settings_ui_opens_from_alerts_page(monkeypatch))


def test_admin_key_settings_pool_mode_save(monkeypatch):
    asyncio.run(_run_admin_key_settings_pool_mode_save(monkeypatch))


def test_admin_key_settings_single_mode_save(monkeypatch):
    asyncio.run(_run_admin_key_settings_single_mode_save(monkeypatch))


def test_admin_key_settings_selected_admin_save(monkeypatch):
    asyncio.run(_run_admin_key_settings_selected_admin_save(monkeypatch))


def test_setup_views_respect_row_width_limits():
    asyncio.run(_run_setup_views_respect_row_width_limits())


def test_admin_key_settings_add_pool_member(monkeypatch):
    asyncio.run(_run_admin_key_settings_add_pool_member(monkeypatch))


def test_admin_key_settings_remove_pool_member(monkeypatch):
    asyncio.run(_run_admin_key_settings_remove_pool_member(monkeypatch))


def test_admin_key_settings_embed_pool_members():
    asyncio.run(_run_admin_key_settings_embed_shows_pool_members())


def test_admin_key_settings_back_and_home_navigation(monkeypatch):
    asyncio.run(_run_admin_key_settings_back_and_home_navigation(monkeypatch))
