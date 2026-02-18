from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from repositories.free_raffle_repo import FreeRaffleRepository
from repositories.torn_items import TornItemsRepository
from utils import get_database, require_api_key
from utils.database import get_pool
from views.free_raffle_views import EnterRaffleView, HostControlsView

log = logging.getLogger("happy_jumper.free_raffle")


def _status_label(status: str, winner_id: int | None) -> str:
    normalized = str(status or "").lower()
    if normalized == "active":
        return "Active"
    if normalized == "cancelled":
        return "Cancelled"
    if winner_id:
        return "Ended"
    return "Ended (no entrants)"


class FreeRaffleModal(discord.ui.Modal, title="Free Raffle"):
    prize = discord.ui.TextInput(label="Prize", required=True, max_length=200)
    note = discord.ui.TextInput(
        label="Note",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, cog: "FreeRaffleCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.channel is None:
            await interaction.response.send_message("❌ This command can only be used in a server channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=False)

        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.create_raffle(
            guild_id=int(interaction.guild_id),
            channel_id=int(interaction.channel_id),
            host_discord_id=int(interaction.user.id),
            prize_text=str(self.prize.value).strip(),
            note_text=(str(self.note.value).strip() or None),
        )
        raffle_id = int(raffle["id"])

        embed = await self.cog.build_raffle_embed(raffle_id)
        public_view = self.cog.public_view(raffle_id)
        public_message = await interaction.channel.send(embed=embed, view=public_view)
        await repo.set_message_id(raffle_id, int(public_message.id))

        host_view = self.cog.host_controls_view(raffle_id)
        await interaction.followup.send(
            "✅ Free raffle created. Host controls are below.",
            ephemeral=True,
            view=host_view,
        )


class FreeRaffleCog(commands.Cog):
    freeraffle = app_commands.Group(name="freeraffle", description="Free raffle commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        repo = FreeRaffleRepository(get_pool())
        try:
            for raffle in await repo.list_active_raffles():
                raffle_id = int(raffle["id"])
                self.bot.add_view(self.public_view(raffle_id))
        except Exception:
            log.exception("Failed registering free raffle views")

    @freeraffle.command(name="start", description="Start a free raffle")
    async def start(self, interaction: discord.Interaction) -> None:
        db = get_database()
        if not await require_api_key(interaction, db, "start a free raffle"):
            return
        await interaction.response.send_modal(FreeRaffleModal(self))

    def public_view(self, raffle_id: int, *, disabled: bool = False) -> EnterRaffleView:
        return EnterRaffleView(raffle_id=raffle_id, on_enter=self.handle_enter, disabled=disabled)

    def host_controls_view(self, raffle_id: int, *, disabled: bool = False) -> HostControlsView:
        return HostControlsView(
            raffle_id=raffle_id,
            on_draw=self.handle_draw,
            on_cancel=self.handle_cancel,
            disabled=disabled,
        )

    async def resolve_thumbnail(self, prize_text: str) -> str | None:
        item = await TornItemsRepository(get_pool()).get_item_meta_by_name(prize_text)
        if not item:
            return None
        image = str(item.get("image_url") or "").strip()
        return image or None

    async def build_raffle_embed(self, raffle_id: int) -> discord.Embed:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not raffle:
            return discord.Embed(title="🎟️ Free Raffle", description="Raffle not found.", color=discord.Color.red())

        winner_id = await repo.get_winner(raffle_id)
        entry_count = await repo.get_entry_count(raffle_id)

        embed = discord.Embed(title="🎟️ Free Raffle", color=discord.Color.blurple())
        embed.add_field(name="Prize", value=str(raffle.get("prize_text") or "Unknown"), inline=False)
        note_text = str(raffle.get("note_text") or "").strip()
        if note_text:
            embed.add_field(name="Note", value=note_text, inline=False)
        embed.add_field(name="Entries", value=str(entry_count), inline=True)
        embed.add_field(name="Status", value=_status_label(str(raffle.get("status") or ""), winner_id), inline=True)
        embed.add_field(name="Host", value=f"<@{int(raffle['host_discord_id'])}>", inline=True)
        if winner_id:
            embed.add_field(name="Winner", value=f"<@{winner_id}>", inline=False)

        thumbnail_url = await self.resolve_thumbnail(str(raffle.get("prize_text") or ""))
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        return embed

    async def refresh_public_message(self, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not raffle:
            return

        channel_id = raffle.get("channel_id")
        message_id = raffle.get("message_id")
        if not channel_id or not message_id:
            return

        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                return

        if not hasattr(channel, "fetch_message"):
            return

        try:
            message = await channel.fetch_message(int(message_id))
        except Exception:
            return

        embed = await self.build_raffle_embed(raffle_id)
        disabled = str(raffle.get("status") or "").lower() != "active"
        await message.edit(embed=embed, view=self.public_view(raffle_id, disabled=disabled))

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.response.send_message(message, ephemeral=True)

    async def handle_enter(self, interaction: discord.Interaction, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not raffle:
            await self._send_ephemeral(interaction, "Raffle not found.")
            return
        if str(raffle.get("status") or "").lower() != "active":
            await self._send_ephemeral(interaction, "Raffle is not active.")
            return

        inserted = await repo.add_entry(raffle_id, int(interaction.user.id))
        if inserted:
            await self._send_ephemeral(interaction, "✅ You’re entered.")
            await self.refresh_public_message(raffle_id)
            return
        await self._send_ephemeral(interaction, "You’re already entered.")

    async def _assert_host(self, interaction: discord.Interaction, raffle: dict | None) -> bool:
        if not raffle:
            await self._send_ephemeral(interaction, "Raffle not found.")
            return False
        if int(interaction.user.id) != int(raffle["host_discord_id"]):
            await self._send_ephemeral(interaction, "Only the host can do that.")
            return False
        return True

    async def handle_cancel(self, interaction: discord.Interaction, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return

        if str(raffle.get("status") or "").lower() != "active":
            await self._send_ephemeral(interaction, "Raffle is not active.")
            return

        await repo.set_status(raffle_id, "cancelled", datetime.now(timezone.utc))
        await self.refresh_public_message(raffle_id)

        if interaction.response.is_done():
            await interaction.edit_original_response(content="Raffle cancelled.", view=self.host_controls_view(raffle_id, disabled=True))
        else:
            await interaction.response.edit_message(content="Raffle cancelled.", view=self.host_controls_view(raffle_id, disabled=True))

    async def handle_draw(self, interaction: discord.Interaction, raffle_id: int) -> None:
        pool = get_pool()
        repo = FreeRaffleRepository(pool)

        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return

        async with pool.acquire() as conn:
            async with conn.transaction():
                raffle_row = await conn.fetchrow(
                    "SELECT * FROM free_raffles WHERE id = $1 FOR UPDATE",
                    raffle_id,
                )
                if not raffle_row:
                    await self._send_ephemeral(interaction, "Raffle not found.")
                    return
                raffle = dict(raffle_row)
                if str(raffle.get("status") or "").lower() != "active":
                    await self._send_ephemeral(interaction, "Raffle is not active.")
                    return

                existing_winner = await conn.fetchval(
                    "SELECT discord_id FROM free_raffle_winners WHERE raffle_id = $1",
                    raffle_id,
                )
                if existing_winner is not None:
                    await self._send_ephemeral(interaction, "Winner has already been drawn.")
                    return

                rows = await conn.fetch(
                    "SELECT discord_id FROM free_raffle_entries WHERE raffle_id = $1",
                    raffle_id,
                )
                entrant_ids = [int(row["discord_id"]) for row in rows]
                ended_at = datetime.now(timezone.utc)

                if not entrant_ids:
                    await conn.execute(
                        """
                        UPDATE free_raffles
                        SET status = 'ended',
                            ended_at = $2,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        raffle_id,
                        ended_at,
                    )
                    winner_id = None
                else:
                    winner_id = int(secrets.choice(entrant_ids))
                    await conn.execute(
                        """
                        INSERT INTO free_raffle_winners (raffle_id, discord_id)
                        VALUES ($1, $2)
                        ON CONFLICT (raffle_id) DO NOTHING
                        """,
                        raffle_id,
                        winner_id,
                    )
                    await conn.execute(
                        """
                        UPDATE free_raffles
                        SET status = 'ended',
                            ended_at = $2,
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        raffle_id,
                        ended_at,
                    )

        await self.refresh_public_message(raffle_id)

        raffle = await repo.get_raffle(raffle_id)
        if raffle and winner_id:
            channel = self.bot.get_channel(int(raffle["channel_id"]))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
                except Exception:
                    channel = None
            if channel and hasattr(channel, "send"):
                await channel.send(f"🎉 Winner: <@{winner_id}>")

        if winner_id:
            status_message = f"Winner drawn: <@{winner_id}>"
        else:
            status_message = "Raffle ended with no entrants."

        if interaction.response.is_done():
            await interaction.edit_original_response(content=status_message, view=self.host_controls_view(raffle_id, disabled=True))
        else:
            await interaction.response.edit_message(content=status_message, view=self.host_controls_view(raffle_id, disabled=True))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FreeRaffleCog(bot))
