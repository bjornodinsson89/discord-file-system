from __future__ import annotations

import asyncio

import discord
from utils.panel_edit_safety import PanelEditSafety


class DummyMessage:
    def __init__(self, message_id: int = 1):
        self.id = message_id
        self.content = "base"
        self.embeds: list[discord.Embed] = []
        self.components = []
        self.edits: list[dict] = []

    async def edit(self, *, content=None, embed=None, view=None):
        self.edits.append({"content": content, "embed": embed, "view": view})


def test_unchanged_payload_skips_edit():
    async def _run():
        safety = PanelEditSafety()
        msg = DummyMessage()
        changed = await safety.request_edit(msg, content="base", min_interval_seconds=5)
        assert changed is False
        assert len(msg.edits) == 0
    asyncio.run(_run())


def test_throttles_and_latest_payload_wins():
    async def _run():
        safety = PanelEditSafety()
        msg = DummyMessage()
        await safety.request_edit(msg, content="first", min_interval_seconds=0.2)
        await safety.request_edit(msg, content="second", min_interval_seconds=0.2)
        await safety.request_edit(msg, content="third", min_interval_seconds=0.2)
        await asyncio.sleep(0.25)
        assert len(msg.edits) == 2
        assert msg.edits[-1]["content"] == "third"
    asyncio.run(_run())


def test_forced_identical_update_still_skips():
    async def _run():
        safety = PanelEditSafety()
        msg = DummyMessage()
        await safety.request_edit(msg, content="new", min_interval_seconds=10)
        changed = await safety.request_edit(msg, content="new", min_interval_seconds=10, force=True)
        assert changed is False
        assert len(msg.edits) == 1
    asyncio.run(_run())



class ErrorMessage(DummyMessage):
    async def edit(self, *, content=None, embed=None, view=None):
        raise RuntimeError("boom")


def test_edit_exceptions_do_not_raise():
    async def _run():
        safety = PanelEditSafety()
        msg = ErrorMessage()
        changed = await safety.request_edit(msg, content="x", min_interval_seconds=0.1)
        assert changed is False
    asyncio.run(_run())
