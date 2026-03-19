import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import setup_panel
from setup_panel import EngagementRolesView, _respond_callback_error


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
