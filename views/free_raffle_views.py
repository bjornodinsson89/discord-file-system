from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord


EnterHandler = Callable[[discord.Interaction, int], Awaitable[None]]
InfoHandler = Callable[[discord.Interaction, int], Awaitable[None]]
HostHandler = Callable[[discord.Interaction, int], Awaitable[None]]


class EnterRaffleView(discord.ui.View):
    def __init__(
        self,
        raffle_id: int,
        on_enter: EnterHandler,
        *,
        disabled: bool = False,
        show_join_button: bool = True,
        on_info: InfoHandler | None = None,
        on_host_controls: HostHandler | None = None,
    ):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.on_enter = on_enter
        self.on_info = on_info
        self.on_host_controls = on_host_controls

        info_button = discord.ui.Button(
            label="ℹ️ Info",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fr_info:{raffle_id}",
            disabled=False,
        )
        info_button.callback = self._on_info
        self.add_item(info_button)

        host_controls_button = discord.ui.Button(
            label="🛠️ Host Controls",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fr_host_controls:{raffle_id}",
            disabled=False,
        )
        host_controls_button.callback = self._on_host_controls
        self.add_item(host_controls_button)

        if show_join_button:
            button = discord.ui.Button(
                label="🎟️ Enter Giveaway",
                style=discord.ButtonStyle.green,
                custom_id=f"fr_enter:{raffle_id}",
                disabled=disabled,
            )
            button.callback = self._on_enter
            self.add_item(button)

    async def _on_enter(self, interaction: discord.Interaction) -> None:
        await self.on_enter(interaction, self.raffle_id)

    async def _on_info(self, interaction: discord.Interaction) -> None:
        if self.on_info is None:
            await interaction.response.send_message("Info is unavailable right now.", ephemeral=True)
            return
        await self.on_info(interaction, self.raffle_id)

    async def _on_host_controls(self, interaction: discord.Interaction) -> None:
        if self.on_host_controls is None:
            await interaction.response.send_message(
                "Host controls are unavailable right now.", ephemeral=True
            )
            return
        await self.on_host_controls(interaction, self.raffle_id)


class HostControlsView(discord.ui.View):
    def __init__(
        self,
        raffle_id: int,
        *,
        on_end_now: HostHandler,
        on_cancel: HostHandler,
        on_refresh: HostHandler,
        on_view_entries: HostHandler,
        on_reroll: HostHandler,
        on_auto_settings: HostHandler | None = None,
        disabled: bool = False,
        can_reroll: bool = False,
        show_auto_settings: bool = False,
    ):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.on_end_now = on_end_now
        self.on_cancel = on_cancel
        self.on_refresh = on_refresh
        self.on_view_entries = on_view_entries
        self.on_reroll = on_reroll
        self.on_auto_settings = on_auto_settings

        end_button = discord.ui.Button(
            label="⏹️ End Giveaway Now",
            style=discord.ButtonStyle.primary,
            custom_id=f"fr_end:{raffle_id}",
            disabled=disabled,
            row=0,
        )
        end_button.callback = self._on_end_now
        self.add_item(end_button)

        cancel_button = discord.ui.Button(
            label="❌ Cancel Giveaway",
            style=discord.ButtonStyle.red,
            custom_id=f"fr_cancel:{raffle_id}",
            disabled=disabled,
            row=0,
        )
        cancel_button.callback = self._on_cancel
        self.add_item(cancel_button)

        refresh_button = discord.ui.Button(
            label="🔄 Refresh Panel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fr_refresh:{raffle_id}",
            disabled=False,
            row=1,
        )
        refresh_button.callback = self._on_refresh
        self.add_item(refresh_button)

        entries_button = discord.ui.Button(
            label="📋 View Entries",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fr_entrants:{raffle_id}",
            disabled=False,
            row=1,
        )
        entries_button.callback = self._on_view_entries
        self.add_item(entries_button)

        reroll_button = discord.ui.Button(
            label="🎲 Reroll Winner",
            style=discord.ButtonStyle.success,
            custom_id=f"fr_reroll:{raffle_id}",
            disabled=disabled or not can_reroll,
            row=2,
        )
        reroll_button.callback = self._on_reroll
        self.add_item(reroll_button)

        if show_auto_settings and on_auto_settings is not None:
            auto_settings_button = discord.ui.Button(
                label="⚙️ Auto Entry Settings",
                style=discord.ButtonStyle.secondary,
                custom_id=f"fr_auto_settings:{raffle_id}",
                disabled=False,
                row=2,
            )
            auto_settings_button.callback = self._on_auto_settings
            self.add_item(auto_settings_button)

    async def _on_end_now(self, interaction: discord.Interaction) -> None:
        await self.on_end_now(interaction, self.raffle_id)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self.on_cancel(interaction, self.raffle_id)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await self.on_refresh(interaction, self.raffle_id)

    async def _on_view_entries(self, interaction: discord.Interaction) -> None:
        await self.on_view_entries(interaction, self.raffle_id)

    async def _on_reroll(self, interaction: discord.Interaction) -> None:
        await self.on_reroll(interaction, self.raffle_id)

    async def _on_auto_settings(self, interaction: discord.Interaction) -> None:
        if self.on_auto_settings is None:
            await interaction.response.send_message(
                "Auto entry settings are unavailable right now.", ephemeral=True
            )
            return
        await self.on_auto_settings(interaction, self.raffle_id)
