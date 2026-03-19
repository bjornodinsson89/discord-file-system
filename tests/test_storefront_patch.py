from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


from cogs.store import AdminStorefrontView
from services.store_service import StoreService


class _Message:
    _next_id = 100

    def __init__(self, *, embed=None, view=None):
        self.id = _Message._next_id
        _Message._next_id += 1
        self.embed = embed
        self.view = view
        self.deleted = False
        self.edits = 0

    async def edit(self, *, embed=None, view=None):
        self.embed = embed
        self.view = view
        self.edits += 1

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.messages: dict[int, _Message] = {}
        self.sent = []

    async def send(self, *, embed=None, view=None):
        msg = _Message(embed=embed, view=view)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        return msg

    async def fetch_message(self, message_id: int):
        msg = self.messages.get(int(message_id))
        if msg is None or msg.deleted:
            raise LookupError("missing")
        return msg


class _Guild:
    def __init__(self, channels):
        self.id = 1
        self.owner_id = 11
        self._channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int):
        return self._channels.get(int(channel_id))


class _Repo:
    def __init__(self):
        self.settings = {
            "enabled": True,
            "store_channel_id": 10,
            "storefront_channel_id": None,
            "store_hub_message_id": None,
            "store_admin_message_id": None,
        }
        self.active_items = [
            {
                "id": 1,
                "name": "Xanax",
                "description": "heal",
                "category": "torn_item",
                "token_cost": 5,
                "stock": 3,
                "fulfillment_type": "admin_manual",
                "thumbnail_url": "https://img.example/xanax.png",
                "storefront_channel_id": None,
                "storefront_message_id": None,
                "is_active": True,
            }
        ]
        self.all_items = [dict(self.active_items[0])]

    async def get_or_create_guild_settings(self, _guild_id: int):
        return dict(self.settings)

    async def upsert_guild_settings(self, _guild_id: int, **changes):
        self.settings.update(changes)
        return dict(self.settings)

    async def get_storefront_items(self, _guild_id: int):
        return [dict(item) for item in self.active_items]

    async def list_all_guild_items(self, _guild_id: int):
        return [dict(item) for item in self.all_items]

    async def update_item(self, _guild_id: int, item_id: int, **changes):
        for item in self.all_items:
            if item["id"] == item_id:
                item.update(changes)
                return dict(item)
        return None

    async def lookup_torn_thumbnail(self, **_kwargs):
        return "https://img.example/xanax.png"


class _Cog:
    def build_admin_storefront_view(self):
        return SimpleNamespace(name="admin")

    def build_redeem_view(self, item_id: int):
        from cogs.store import StoreItemActionView

        return StoreItemActionView(self, item_id)

    async def send_item_detail(self, interaction, item_id: int):
        return None


class _Perms:
    def __init__(self, *, administrator=False, manage_guild=False):
        self.administrator = administrator
        self.manage_guild = manage_guild


class _Role:
    def __init__(self, rid):
        self.id = rid


class _Response:
    def __init__(self):
        self.message = None
        self.modal = None

    async def send_message(self, content=None, *, ephemeral=False, embed=None, view=None):
        self.message = {"content": content, "ephemeral": ephemeral, "embed": embed, "view": view}

    async def send_modal(self, modal):
        self.modal = modal

    def is_done(self):
        return False


class _Interaction:
    def __init__(self, *, guild, member):
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.user = member
        self.response = _Response()
        self.followup = SimpleNamespace(send=self.response.send_message)


def test_store_commands_removed_from_source_and_storefront_controls_present():
    src = Path("cogs/store.py").read_text(encoding="utf-8")
    assert '@app_commands.command(name="store"' not in src
    assert 'name="store_admin"' not in src
    assert "class AdminStorefrontView" in src
    assert 'title="Store Controls"' in src
    assert "Refresh Storefront" not in src


def test_store_channel_selector_lives_in_store_setup():
    src = Path("setup_panel.py").read_text(encoding="utf-8")
    assert "class StoreChannelSelect(discord.ui.ChannelSelect):" in src
    assert "self.add_item(StoreChannelSelect(self.panel))" in src
    assert 'ChannelSelect(self.panel, "store_channel_id", "Set Store channel"' not in src
    assert 'placeholder="Store Channel"' in src


def test_storefront_sync_creates_hub_admin_and_item_messages_without_duplicates():
    async def _run():
        repo = _Repo()
        channel = _Channel(10)
        guild = _Guild([channel])
        service = StoreService(repo, SimpleNamespace(), cog=_Cog())

        result1 = await service.sync_storefront(guild)
        assert result1["channel_id"] == 10
        assert repo.settings["store_hub_message_id"]
        assert repo.settings["store_admin_message_id"]
        assert repo.all_items[0]["storefront_message_id"]
        assert len(channel.sent) == 3
        assert channel.sent[0].view is None
        assert (
            channel.sent[0].embed.footer.text == "Scroll below to browse the live storefront items."
        )
        assert channel.sent[1].view.name == "admin"
        assert channel.sent[1].embed.title == "Store Controls"
        assert str(channel.sent[2].embed.thumbnail.url) == "https://img.example/xanax.png"

        result2 = await service.sync_storefront(guild)
        assert result2["hub_message_id"] == result1["hub_message_id"]
        assert len(channel.sent) == 3
        assert channel.messages[result1["hub_message_id"]].edits == 1

    asyncio.run(_run())


def test_storefront_sync_recreates_deleted_item_message_cleanly():
    async def _run():
        repo = _Repo()
        channel = _Channel(10)
        guild = _Guild([channel])
        service = StoreService(repo, SimpleNamespace(), cog=_Cog())
        await service.sync_storefront(guild)
        channel.messages[repo.all_items[0]["storefront_message_id"]].deleted = True

        await service.sync_storefront(guild)
        assert repo.all_items[0]["storefront_message_id"] in channel.messages
        assert len(channel.sent) == 4

    asyncio.run(_run())


def test_storefront_admin_controls_deny_non_admin_users():
    async def _run():
        view = AdminStorefrontView(SimpleNamespace())
        guild = SimpleNamespace(owner_id=11)
        member = SimpleNamespace(
            id=22,
            guild_permissions=_Perms(administrator=False, manage_guild=False),
            roles=[_Role(1)],
        )
        interaction = _Interaction(guild=guild, member=member)
        allowed = await view.interaction_check(interaction)
        assert allowed is False
        assert interaction.response.message["ephemeral"] is True
        assert "admins" in interaction.response.message["content"].lower()

    asyncio.run(_run())


def test_storefront_item_messages_include_admin_edit_button():
    async def _run():
        repo = _Repo()
        channel = _Channel(10)
        guild = _Guild([channel])
        service = StoreService(repo, SimpleNamespace(), cog=_Cog())

        await service.sync_storefront(guild)
        labels = {child.label for child in channel.sent[2].view.children}
        assert labels == {"Redeem", "Edit Item"}

    asyncio.run(_run())


def test_store_item_edit_button_denies_unauthorized_users():
    async def _run():
        from cogs.store import StoreItemActionView

        class _RepoWithItem:
            async def get_item(self, _guild_id, item_id):
                return {"id": item_id, "name": "Xanax", "token_cost": 5, "stock": 3}

        cog = SimpleNamespace(store_repo=_RepoWithItem())
        view = StoreItemActionView(cog, 1)
        guild = _Guild([_Channel(10)])
        member = SimpleNamespace(
            id=22,
            guild_permissions=_Perms(administrator=False, manage_guild=False),
        )
        interaction = _Interaction(guild=guild, member=member)

        await view.edit_item.callback(interaction)
        assert interaction.response.message["ephemeral"] is True
        assert "admins" in interaction.response.message["content"].lower()
        assert interaction.response.modal is None

    asyncio.run(_run())
