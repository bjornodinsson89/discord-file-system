from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord


EnterHandler = Callable[[discord.Interaction, int], Awaitable[None]]
HostHandler = Callable[[discord.Interaction, int], Awaitable[None]]


class EnterRaffleView(discord.ui.View):
    def __init__(self, raffle_id: int, on_enter: EnterHandler, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.on_enter = on_enter
        button = discord.ui.Button(
            label="🎟️ Enter",
            style=discord.ButtonStyle.green,
            custom_id=f"fr_enter:{raffle_id}",
            disabled=disabled,
        )
        button.callback = self._on_enter
        self.add_item(button)

    async def _on_enter(self, interaction: discord.Interaction) -> None:
        await self.on_enter(interaction, self.raffle_id)


class HostControlsView(discord.ui.View):
    def __init__(self, raffle_id: int, on_draw: HostHandler, on_cancel: HostHandler, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.on_draw = on_draw
        self.on_cancel = on_cancel

        draw_button = discord.ui.Button(
            label="🏁 Draw Winner",
            style=discord.ButtonStyle.blurple,
            custom_id=f"fr_draw:{raffle_id}",
            disabled=disabled,
        )
        draw_button.callback = self._on_draw
        self.add_item(draw_button)

        cancel_button = discord.ui.Button(
            label="❌ Cancel",
            style=discord.ButtonStyle.red,
            custom_id=f"fr_cancel:{raffle_id}",
            disabled=disabled,
        )
        cancel_button.callback = self._on_cancel
        self.add_item(cancel_button)

    async def _on_draw(self, interaction: discord.Interaction) -> None:
        await self.on_draw(interaction, self.raffle_id)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self.on_cancel(interaction, self.raffle_id)
