import asyncio
from types import SimpleNamespace

from setup_panel import SetupPanelView
from utils.discord_channels import resolve_guild_channel


class _FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=False, send_messages=False, embed_links=False)


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class _FakeGuild:
    def __init__(self, channel=None):
        self.me = object()
        self._channel = channel

    def get_channel(self, channel_id):
        if self._channel and self._channel.id == channel_id:
            return self._channel
        return None


class _FakeSelected:
    def __init__(self, channel_id: int):
        self.id = channel_id


class _FakeClient:
    async def fetch_channel(self, _channel_id):
        return None


def test_setup_set_channel_handles_channel_without_mention():
    async def _run():
        panel = SetupPanelView(
            owner_id=1, db=SimpleNamespace(), settings={}, guild=SimpleNamespace()
        )
        channel = _FakeChannel(123)
        panel._resolve_real_channel = lambda _interaction, _selected: asyncio.sleep(
            0, result=channel
        )
        panel._resolve_bot_member = lambda _interaction: object()

        interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(me=object()),
            response=_FakeResponse(),
        )

        await panel._set_channel(interaction, "raffle_channel_id", SimpleNamespace(id=123))

        assert interaction.response.messages, "Expected an error response for missing permissions"
        message = interaction.response.messages[0][1]["embed"].description
        assert "<#123>" in message

    asyncio.run(_run())


def test_resolve_guild_channel_from_lightweight_selection():
    async def _run():
        resolved = _FakeChannel(456)
        interaction = SimpleNamespace(
            guild=_FakeGuild(channel=resolved),
            client=_FakeClient(),
        )
        selected = _FakeSelected(456)

        channel = await resolve_guild_channel(interaction, selected)
        assert channel is resolved

    asyncio.run(_run())
