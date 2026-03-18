from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.store import AdminStorefrontView, StoreCog, StoreItemActionView, parse_int_input


class _Bot:
    def __init__(self):
        self.registered_views = []

    def add_view(self, view):
        self.registered_views.append(view)


def test_store_cog_startup_registers_only_persistent_store_views(monkeypatch):
    async def _run():
        monkeypatch.setattr("cogs.store.get_pool", lambda: object())

        cog = StoreCog(_Bot())

        assert len(cog.bot.registered_views) == 1
        assert isinstance(cog.bot.registered_views[0], AdminStorefrontView)
        assert cog.bot.registered_views[0].is_persistent()

    asyncio.run(_run())


def test_admin_storefront_view_is_persistent_for_global_registration():
    async def _run():
        view = AdminStorefrontView(SimpleNamespace())

        assert view.timeout is None
        assert view.is_persistent()
        assert {child.custom_id for child in view.children} == {
            "storefront:admin:add",
            "storefront:admin:restock",
        }

    asyncio.run(_run())


def test_store_item_action_view_exposes_redeem_and_edit_buttons():
    async def _run():
        view = StoreItemActionView(SimpleNamespace(), 42)

        labels = {child.label for child in view.children}
        assert labels == {"Redeem", "Edit Item"}

    asyncio.run(_run())


def test_parse_int_input_accepts_commas_and_spaces():
    assert parse_int_input(" 10,000 ") == 10000
    assert parse_int_input("1,000,000") == 1000000


def test_parse_int_input_rejects_invalid_strings_cleanly():
    try:
        parse_int_input("10k")
    except ValueError as exc:
        assert str(exc) == "Please enter a valid whole number. Commas are allowed."
    else:
        raise AssertionError("Expected ValueError")


def test_update_item_modal_invalid_number_shows_friendly_error():
    from cogs.store import UpdateItemModal

    class _Svc:
        async def update_store_item(self, **_kwargs):
            raise AssertionError("should not reach service")

    class _Resp:
        def __init__(self):
            self.message = None

        async def send_message(self, content=None, *, ephemeral=False, **_kwargs):
            self.message = {"content": content, "ephemeral": ephemeral}

    class _Interaction:
        def __init__(self):
            self.guild_id = 1
            self.guild = SimpleNamespace(id=1)
            self.response = _Resp()

    async def _run():
        modal = UpdateItemModal(SimpleNamespace(store_service=_Svc(), sync_storefront=None))
        modal.item_id._value = "1"
        modal.name._value = "Xanax"
        modal.token_cost._value = "10k"
        modal.stock._value = "5"
        interaction = _Interaction()

        await modal.on_submit(interaction)
        assert interaction.response.message == {
            "content": "Please enter a valid whole number. Commas are allowed.",
            "ephemeral": True,
        }

    asyncio.run(_run())
