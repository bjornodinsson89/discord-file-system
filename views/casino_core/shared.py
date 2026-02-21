from __future__ import annotations

import discord


def parse_snowflake(raw: str) -> int | None:
    value = str(raw or "").strip().replace("<@", "").replace("<#", "").replace("&", "").replace(">", "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ComingSoonView(discord.ui.View):
    def __init__(self, message: str = "Gameplay module not yet enabled."):
        super().__init__(timeout=60)
        self.message = message

    @discord.ui.button(label="Coming Soon", style=discord.ButtonStyle.secondary, disabled=True)
    async def coming_soon(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(self.message, ephemeral=True)
