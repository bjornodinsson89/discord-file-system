import asyncio
from types import SimpleNamespace

from setup_panel import ChannelsViewPage4, SetupPanelView, _channels_embed


class _Guild:
    def __init__(self):
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, _rid):
        return None


def test_channels_embed_mentions_who_can_jump_panel_channel():
    embed = _channels_embed()
    assert "Who Can Jump panel channel" in (embed.description or "")


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
        assert "Who Can Jump panel: Not set" in channels_field.value

    asyncio.run(_run())


def test_channels_view_page4_has_store_channel_selector():
    async def _run():
        page = ChannelsViewPage4(
            owner_id=1,
            db=SimpleNamespace(),
            settings={},
            guild=_Guild(),
            panel=SimpleNamespace(),
        )
        selectors = [
            child for child in page.children if child.__class__.__name__ == "ChannelSelect"
        ]
        assert len(selectors) == 4
        assert any(getattr(child, "placeholder", "") == "Set Store channel" for child in selectors)

    asyncio.run(_run())
