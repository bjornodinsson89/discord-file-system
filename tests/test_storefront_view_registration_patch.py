from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.store import AdminStorefrontView, StoreBrowseView, StoreCog


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


def test_store_browse_view_remains_dynamic_and_not_persistent():
    async def _run():
        view = StoreBrowseView(SimpleNamespace())

        assert view.timeout == 300
        assert not view.is_persistent()

    asyncio.run(_run())


def test_admin_storefront_view_is_persistent_for_global_registration():
    async def _run():
        view = AdminStorefrontView(SimpleNamespace())

        assert view.timeout is None
        assert view.is_persistent()
        assert {child.custom_id for child in view.children} == {
            "storefront:admin:add",
            "storefront:admin:edit",
            "storefront:admin:restock",
            "storefront:admin:disable",
            "storefront:admin:pending",
            "storefront:admin:fulfill",
            "storefront:admin:refund",
            "storefront:admin:refresh",
        }

    asyncio.run(_run())
