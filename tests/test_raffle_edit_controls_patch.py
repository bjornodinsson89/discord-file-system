from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord

from cogs import raffles as raffle_module


class _FakeResponse:
    def __init__(self):
        self.deferred = False
        self.done = False
        self.messages = []
        self.modals = []

    def is_done(self):
        return self.done

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = True
        self.done = True
        self.messages.append({"type": "defer", "ephemeral": ephemeral, "thinking": thinking})

    async def send_message(self, content=None, *, ephemeral=False, embed=None, view=None):
        self.done = True
        self.messages.append(
            {"content": content, "ephemeral": ephemeral, "embed": embed, "view": view}
        )

    async def send_modal(self, modal):
        self.done = True
        self.modals.append(modal)


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, ephemeral=False, embed=None, view=None, wait=False):
        payload = {
            "content": content,
            "ephemeral": ephemeral,
            "embed": embed,
            "view": view,
            "wait": wait,
        }
        self.messages.append(payload)
        return SimpleNamespace(**payload)


class _FakeRepo:
    instances: list["_FakeRepo"] = []
    raffle = {
        "raffle_id": 42,
        "creator_discord_id": 111,
        "admin_comments": "Old note",
        "prize_image_url": "https://imgur.com/original.png",
        "is_bundle": False,
    }

    def __init__(self, _pool):
        self.comment_updates = []
        self.image_updates = []
        type(self).instances.append(self)

    async def get_raffle(self, raffle_id):
        assert int(raffle_id) == 42
        return dict(type(self).raffle)

    async def update_raffle_comment(self, raffle_id, comment):
        self.comment_updates.append((int(raffle_id), comment))
        type(self).raffle["admin_comments"] = comment

    async def update_raffle_image(self, raffle_id, image_url):
        self.image_updates.append((int(raffle_id), image_url))
        type(self).raffle["prize_image_url"] = image_url


class _FakeCog:
    def __init__(self):
        self.refreshed = []

    async def refresh_raffle_public_panel(self, raffle_id):
        self.refreshed.append(int(raffle_id))


class _FakeClient:
    def __init__(self, cog):
        self._cog = cog

    def get_cog(self, name):
        if name == "RafflesCog":
            return self._cog
        return None


class _FakeInteraction:
    def __init__(self, user_id, cog=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild = SimpleNamespace(id=123)
        self.guild_id = 123
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.client = _FakeClient(cog or _FakeCog())


def _button(view: discord.ui.View, label: str):
    return next(child for child in view.children if getattr(child, "label", None) == label)


def test_edit_raffle_flow_exposes_add_photo_and_edit_comment_actions():
    async def _run():
        view = raffle_module.RaffleManageView(42)
        edit_button = _button(view, "Edit Raffle")
        assert edit_button is not None

        edit_view = raffle_module.RaffleEditView(42, 111)
        labels = {child.label for child in edit_view.children if getattr(child, "label", None)}
        assert {"Add Photo", "Edit Comment"} <= labels

    asyncio.run(_run())


def test_edit_comment_updates_stored_comment_and_refreshes_panel(monkeypatch):
    async def _run():
        _FakeRepo.instances.clear()
        _FakeRepo.raffle = {
            "raffle_id": 42,
            "creator_discord_id": 111,
            "admin_comments": "Old note",
            "prize_image_url": None,
            "is_bundle": False,
        }
        cog = _FakeCog()
        interaction = _FakeInteraction(user_id=111, cog=cog)
        monkeypatch.setattr(raffle_module, "RafflesRepository", _FakeRepo)
        monkeypatch.setattr(raffle_module, "get_pool", lambda: object())
        monkeypatch.setattr(
            raffle_module,
            "_can_manage_raffle_edit",
            lambda interaction, raffle: asyncio.sleep(0, result=True),
        )

        modal = raffle_module.RaffleEditCommentModal(42, 111, "Old note")
        modal.comment._value = "New admin note"
        await modal.on_submit(interaction)

        repo = _FakeRepo.instances[-1]
        assert repo.comment_updates == [(42, "New admin note")]
        assert cog.refreshed == [42]
        assert interaction.response.deferred is True
        assert interaction.followup.messages[-1]["content"] == "✅ Raffle comment updated."
        assert interaction.followup.messages[-1]["ephemeral"] is True

    asyncio.run(_run())


def test_add_photo_updates_stored_image_and_refreshes_panel(monkeypatch):
    async def _run():
        _FakeRepo.instances.clear()
        _FakeRepo.raffle = {
            "raffle_id": 42,
            "creator_discord_id": 111,
            "admin_comments": None,
            "prize_image_url": None,
            "is_bundle": False,
        }
        cog = _FakeCog()
        interaction = _FakeInteraction(user_id=111, cog=cog)
        monkeypatch.setattr(raffle_module, "RafflesRepository", _FakeRepo)
        monkeypatch.setattr(raffle_module, "get_pool", lambda: object())
        monkeypatch.setattr(
            raffle_module,
            "_can_manage_raffle_edit",
            lambda interaction, raffle: asyncio.sleep(0, result=True),
        )

        modal = raffle_module.RaffleEditImageUrlModal(42, 111, None)
        modal.prize_image_url._value = "https://imgur.com/new-image.png"
        await modal.on_submit(interaction)

        repo = _FakeRepo.instances[-1]
        assert repo.image_updates == [(42, "https://imgur.com/new-image.png")]
        assert cog.refreshed == [42]
        assert interaction.response.deferred is True
        assert interaction.followup.messages[-1]["content"] == "✅ Raffle photo updated."
        assert interaction.followup.messages[-1]["ephemeral"] is True

    asyncio.run(_run())


def test_unauthorized_users_cannot_use_edit_actions(monkeypatch):
    async def _run():
        _FakeRepo.instances.clear()
        monkeypatch.setattr(raffle_module, "RafflesRepository", _FakeRepo)
        monkeypatch.setattr(raffle_module, "get_pool", lambda: object())
        monkeypatch.setattr(
            raffle_module,
            "_can_manage_raffle_edit",
            lambda interaction, raffle: asyncio.sleep(0, result=False),
        )

        comment_interaction = _FakeInteraction(user_id=999)
        modal = raffle_module.RaffleEditCommentModal(42, 111, "Old note")
        modal.comment._value = "Blocked"
        await modal.on_submit(comment_interaction)
        assert _FakeRepo.instances[-1].comment_updates == []
        assert (
            comment_interaction.followup.messages[-1]["content"]
            == "Only the raffle host or a raffle admin can edit this raffle."
        )

        photo_interaction = _FakeInteraction(user_id=999)
        photo_modal = raffle_module.RaffleEditImageUrlModal(42, 111, None)
        photo_modal.prize_image_url._value = "https://imgur.com/nope.png"
        await photo_modal.on_submit(photo_interaction)
        assert _FakeRepo.instances[-1].image_updates == []
        assert (
            photo_interaction.followup.messages[-1]["content"]
            == "Only the raffle host or a raffle admin can edit this raffle."
        )

        edit_view = raffle_module.RaffleEditView(42, 111)
        edit_interaction = _FakeInteraction(user_id=999)
        await _button(edit_view, "Edit Comment").callback(edit_interaction)
        assert (
            edit_interaction.response.messages[-1]["content"]
            == "Only the raffle host or a raffle admin can edit this raffle."
        )

    asyncio.run(_run())
