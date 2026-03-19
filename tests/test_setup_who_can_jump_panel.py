import asyncio
from types import SimpleNamespace

from setup_panel import (
    ChannelConfigView,
    ChannelsDashboardView,
    SetupPanelView,
    StoreSetupView,
    build_channels_dashboard_embed,
)


class _Guild:
    def __init__(self):
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, _rid):
        return None


def test_channels_embed_mentions_who_can_jump_and_store_channels():
    async def _run():
        panel = SetupPanelView(owner_id=1, db=SimpleNamespace(), settings={}, guild=_Guild())
        embed = build_channels_dashboard_embed(panel)
        assert "Who Can Jump" in embed.fields[0].value
        assert "Store Channel" in embed.fields[0].value

    asyncio.run(_run())


def test_setup_summary_includes_who_can_jump_channel_line():
    async def _run():
        guild = _Guild()
        panel = SetupPanelView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={
                "who_can_jump_channel_id": None,
                "jump_ping_role_ids": [],
                "jewelry_alert_role_ids": [],
            },
            guild=guild,
        )
        embed = panel._build_embed()
        channels_field = next(field for field in embed.fields if field.name == "Channels")
        assert "Jump: **not set**" in channels_field.value

    asyncio.run(_run())


def test_channel_config_view_stays_within_discord_limits():
    async def _run():
        panel = SetupPanelView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=_Guild(),
        )
        page = ChannelConfigView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=_Guild(),
            panel=panel,
            channel_items=[
                ("insurance_channel_id", "Insurance Channel"),
                ("jewelry_alert_channel_id", "Jewelry Alerts"),
                ("who_can_jump_channel_id", "Who Can Jump"),
            ],
        )
        selectors = [
            child for child in page.children if child.__class__.__name__ == "ChannelSelect"
        ]
        assert len(selectors) == 3
        assert len([child for child in page.children if getattr(child, "label", None)]) == 2

    asyncio.run(_run())


def test_store_setup_view_has_store_channel_selector():
    async def _run():
        panel = SetupPanelView(
            owner_id=1,
            db=SimpleNamespace(pool=None),
            settings={},
            guild=_Guild(),
        )
        page = StoreSetupView(
            owner_id=1,
            db=SimpleNamespace(pool=None),
            settings={},
            guild=_Guild(),
            panel=panel,
        )
        selectors = [
            child for child in page.children if isinstance(getattr(child, "placeholder", None), str)
        ]
        assert any(getattr(child, "placeholder", "") == "Set Store channel" for child in selectors)

    asyncio.run(_run())


def test_channels_dashboard_has_back_and_home_navigation():
    async def _run():
        panel = SetupPanelView(owner_id=1, db=SimpleNamespace(), settings={}, guild=_Guild())
        view = ChannelsDashboardView(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=_Guild(),
            panel=panel,
        )
        labels = {getattr(child, "label", None) for child in view.children}
        assert "Back" in labels
        assert "Home" in labels

    asyncio.run(_run())
