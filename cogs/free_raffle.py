from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.free_raffle_repo import FreeRaffleRepository
from repositories.torn_items import TornItemsRepository
from utils import get_database, require_api_key
from utils.database import get_pool, is_initialized as db_is_initialized, wait_until_initialized
from utils.advisory_lock import run_with_advisory_lock
from utils.worker_throttle import db_heavy_worker_slot, sleep_startup_jitter
from views.free_raffle_views import EnterRaffleView, HostControlsView

log = logging.getLogger("happy_jumper.free_raffle")

FREE_RAFFLE_MIN_DAYS = 1
FREE_RAFFLE_MAX_DAYS = 30
FREE_RAFFLE_EXPIRY_BATCH_SIZE = 10


def _status_label(status: str, winner_id: int | None) -> str:
    normalized = str(status or "").lower()
    if normalized == "active":
        return "Active"
    if normalized == "cancelled":
        return "Cancelled"
    if winner_id:
        return "Ended"
    return "Ended (no entrants)"


def _status_color(status: str, winner_id: int | None) -> discord.Color:
    normalized = str(status or "").lower()
    if normalized == "active":
        return discord.Color.from_rgb(46, 204, 113)
    if normalized == "cancelled":
        return discord.Color.from_rgb(231, 76, 60)
    if winner_id:
        return discord.Color.light_grey()
    return discord.Color.dark_grey()


class FreeRaffleModal(discord.ui.Modal, title="Free Raffle"):
    prize = discord.ui.TextInput(label="Prize", required=True, max_length=200)
    ends_in_days = discord.ui.TextInput(label="Ends in (days)", required=True, min_length=1, max_length=2)
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

        days_raw = str(self.ends_in_days.value).strip()
        if not days_raw.isdigit():
            await interaction.response.send_message("❌ Ends in (days) must be a whole number from 1 to 30.", ephemeral=True)
            return
        duration_days = int(days_raw)
        if duration_days < FREE_RAFFLE_MIN_DAYS or duration_days > FREE_RAFFLE_MAX_DAYS:
            await interaction.response.send_message(
                f"❌ Ends in (days) must be between {FREE_RAFFLE_MIN_DAYS} and {FREE_RAFFLE_MAX_DAYS}.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=False)

        try:
            now = datetime.now(timezone.utc)
            ends_at = now + timedelta(days=duration_days)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.create_raffle(
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                host_discord_id=int(interaction.user.id),
                prize_text=str(self.prize.value).strip(),
                note_text=(str(self.note.value).strip() or None),
                ends_at=ends_at,
            )
            raffle_id = int(raffle["id"])

            embed = await self.cog.build_raffle_embed(raffle)
            public_view = self.cog.public_view(raffle_id)
            public_message = await interaction.channel.send(embed=embed, view=public_view)
            await repo.set_message_id(raffle_id, int(public_message.id))

            host_view = self.cog.host_controls_view(raffle_id)
            await interaction.followup.send(
                "✅ Free raffle created. Host controls are below.",
                ephemeral=True,
                view=host_view,
            )
        except Exception:
            log.exception("Failed creating free raffle")
            await interaction.followup.send("❌ Failed to create free raffle. Please try again.", ephemeral=True)


class FreeRaffleCog(commands.Cog):
    freeraffle = app_commands.Group(name="freeraffle", description="Free raffle commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._views_registered = False
        self._ready_init_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self._views_registered = False
        self._ready_init_lock = asyncio.Lock()

    async def cog_unload(self) -> None:
        if self.free_raffle_expiration_worker.is_running():
            self.free_raffle_expiration_worker.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._views_registered:
            if not self.free_raffle_expiration_worker.is_running():
                self.free_raffle_expiration_worker.start()
            return
        async with self._ready_init_lock:
            if self._views_registered:
                if not self.free_raffle_expiration_worker.is_running():
                    self.free_raffle_expiration_worker.start()
                return
            try:
                await wait_until_initialized(timeout=30.0)
                repo = FreeRaffleRepository(get_pool())
                backfilled = await repo.backfill_missing_ends_at()
                if backfilled > 0:
                    log.warning("Backfilled ends_at for %s active free raffles using created_at + 1 day", backfilled)
                for raffle in await repo.list_active_raffles():
                    raffle_id = int(raffle["id"])
                    self.bot.add_view(self.public_view(raffle_id))
                self._views_registered = True
                if not self.free_raffle_expiration_worker.is_running():
                    self.free_raffle_expiration_worker.start()
                await self.process_expired_raffles()
            except Exception:
                log.exception("Failed registering free raffle views")


    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = str(data.get("custom_id") or "")
        if not custom_id.startswith("fr_draw:"):
            return
        await self._send_ephemeral(interaction, "This free raffle is now drawn automatically when it ends.")

    @freeraffle.command(name="start", description="Start a free raffle")
    async def start(self, interaction: discord.Interaction) -> None:
        db = get_database()
        if not await require_api_key(interaction, db, "start a free raffle"):
            return
        await interaction.response.send_modal(FreeRaffleModal(self))

    def public_view(self, raffle_id: int, *, disabled: bool = False) -> EnterRaffleView:
        return EnterRaffleView(raffle_id=raffle_id, on_enter=self.handle_enter, disabled=disabled)

    def build_free_raffle_view(
        self,
        raffle_id: int,
        host_discord_id: int | None = None,
        status: str | None = None,
        **_: object,
    ) -> EnterRaffleView:
        disabled = str(status or "").lower() != "active"
        return self.public_view(raffle_id=raffle_id, disabled=disabled)

    def host_controls_view(self, raffle_id: int, *, disabled: bool = False) -> HostControlsView:
        return HostControlsView(
            raffle_id=raffle_id,
            on_cancel=self.handle_cancel,
            disabled=disabled,
        )

    async def resolve_thumbnail(self, prize_text: str) -> str | None:
        try:
            item = await TornItemsRepository(get_pool()).get_item_meta_by_name(prize_text)
        except Exception:
            log.exception("Failed resolving free raffle thumbnail for prize '%s'", prize_text)
            return None
        if not item:
            return None
        image = str(item.get("image_url") or "").strip()
        return image or None

    async def build_raffle_embed(self, raffle: dict) -> discord.Embed:
        raffle_id = int(raffle["id"])
        repo = FreeRaffleRepository(get_pool())

        winner_id = await repo.get_winner(raffle_id)
        entry_count = await repo.get_entry_count(raffle_id)
        status = _status_label(str(raffle.get("status") or ""), winner_id)
        color = _status_color(str(raffle.get("status") or ""), winner_id)
        prize_text = str(raffle.get("prize_text") or "Unknown Prize").strip() or "Unknown Prize"
        note_text = str(raffle.get("note_text") or "").strip()

        ends_at = raffle.get("ends_at")
        ends_line = ""
        if isinstance(ends_at, datetime):
            ends_unix = int(ends_at.astimezone(timezone.utc).timestamp())
            ends_line = f"\n**Ends:** <t:{ends_unix}:R> (<t:{ends_unix}:f>)"

        thumbnail_url = await self.resolve_thumbnail(prize_text)
        title = "🎉 FREE RAFFLE 🎉" if thumbnail_url else "🎉 FREE RAFFLE 🎉 🎁"
        description = (
            "Tap **🎟️ Enter** for a chance to win!"
            f"{ends_line}\n\n"
            f"**🪓 Prize: {prize_text}**\n\n"
            "**How to Enter**\n"
            "✅ Click **🎟️ Enter**\n"
            "✅ One entry per person\n"
            "✅ Winner announced automatically"
        )

        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="🎟️ Entries", value=f"**{entry_count}**", inline=True)
        embed.add_field(name="⏳ Status", value=f"**{status}**", inline=True)
        embed.add_field(name="👑 Host", value=f"<@{int(raffle['host_discord_id'])}>", inline=False)
        if note_text:
            embed.add_field(name="📝 Note", value=note_text, inline=False)
        if winner_id:
            embed.add_field(name="🏆 Winner", value=f"<@{winner_id}>", inline=False)

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
            except Exception as exc:
                log.error(
                    "Failed refreshing free raffle message: raffle_id=%s channel_id=%s message_id=%s error_type=%s error=%s",
                    raffle_id,
                    channel_id,
                    message_id,
                    type(exc).__name__,
                    exc,
                )
                return

        if not hasattr(channel, "get_partial_message"):
            log.error(
                "Failed refreshing free raffle message: raffle_id=%s channel_id=%s message_id=%s error_type=%s error=%s",
                raffle_id,
                channel_id,
                message_id,
                "UnsupportedChannel",
                "channel does not support partial messages",
            )
            return

        try:
            message = channel.get_partial_message(int(message_id))
            embed = await self.build_raffle_embed(raffle)
            view = self.build_free_raffle_view(
                raffle_id=int(raffle["id"]),
                host_discord_id=int(raffle["host_discord_id"]),
                status=str(raffle.get("status") or ""),
            )
            await message.edit(embed=embed, view=view)
        except Exception as exc:
            log.error(
                "Failed refreshing free raffle message: raffle_id=%s channel_id=%s message_id=%s error_type=%s error=%s",
                raffle_id,
                channel_id,
                message_id,
                type(exc).__name__,
                exc,
            )
            return

    async def _send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.response.send_message(message, ephemeral=True)

    async def handle_enter(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
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
                if interaction.message is not None:
                    refreshed_raffle = await repo.get_raffle(raffle_id)
                    if refreshed_raffle:
                        embed = await self.build_raffle_embed(refreshed_raffle)
                        view = self.build_free_raffle_view(
                            raffle_id=int(refreshed_raffle["id"]),
                            host_discord_id=int(refreshed_raffle["host_discord_id"]),
                            status=str(refreshed_raffle.get("status") or ""),
                        )
                        await interaction.message.edit(embed=embed, view=view)
                    else:
                        await self.refresh_public_message(raffle_id)
                else:
                    await self.refresh_public_message(raffle_id)
                await self._send_ephemeral(interaction, "✅ You’re entered.")
                return
            await self._send_ephemeral(interaction, "You’re already entered.")
        except Exception:
            log.exception("Failed handling free raffle entry for raffle_id=%s", raffle_id)
            await self._send_ephemeral(interaction, "Failed to enter raffle. Please try again.")

    async def _assert_host(self, interaction: discord.Interaction, raffle: dict | None) -> bool:
        if not raffle:
            await self._send_ephemeral(interaction, "Raffle not found.")
            return False
        if int(interaction.user.id) != int(raffle["host_discord_id"]):
            await self._send_ephemeral(interaction, "Only the host can do that.")
            return False
        return True

    async def handle_cancel(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
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
        except Exception:
            log.exception("Failed cancelling free raffle raffle_id=%s", raffle_id)
            await self._send_ephemeral(interaction, "Failed to cancel raffle. Please try again.")

    async def handle_draw(self, interaction: discord.Interaction, raffle_id: int) -> None:
        await self._send_ephemeral(interaction, "This free raffle is now drawn automatically when it ends.")

    async def announce_raffle_result(self, raffle: dict, winner_id: int | None) -> None:
        channel = self.bot.get_channel(int(raffle["channel_id"]))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(raffle["channel_id"]))
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            return

        if winner_id is None:
            await channel.send("🎉 Free raffle ended: No entries — no winner.")
            return

        guild = self.bot.get_guild(int(raffle["guild_id"]))
        winner_mention = f"<@{winner_id}>"
        if guild is not None and guild.get_member(winner_id) is None:
            winner_mention = f"<@{winner_id}> (ID: {winner_id})"
        await channel.send(f"🎉 Free raffle winner: {winner_mention}")

    async def process_expired_raffles(self) -> int:
        repo = FreeRaffleRepository(get_pool())
        now = datetime.now(timezone.utc)
        expired = await repo.list_expired_active_raffles(now=now, limit=FREE_RAFFLE_EXPIRY_BATCH_SIZE)
        processed = 0
        for raffle in expired:
            raffle_id = int(raffle["id"])
            try:
                result = await repo.draw_expired_raffle(raffle_id, now=now)
                if result is None:
                    continue

                ended_raffle = result["raffle"]
                winner_id = result["winner_id"]
                entries_count = int(result["entries_count"])
                log.info(
                    "Auto-drew free raffle id=%s winner_id=%s entries=%s",
                    raffle_id,
                    winner_id,
                    entries_count,
                )
                await self.announce_raffle_result(ended_raffle, winner_id)
                await self.refresh_public_message(raffle_id)
                processed += 1
            except Exception:
                log.exception("Failed processing automatic free raffle draw raffle_id=%s", raffle_id)
        if processed > 0:
            log.info("Free raffle expiration worker processed=%s", processed)
        else:
            log.debug("Free raffle expiration worker processed=0")
        return processed

    @tasks.loop(seconds=60)
    async def free_raffle_expiration_worker(self) -> None:
        if not db_is_initialized():
            return

        db = get_database()
        worker_slot = db_heavy_worker_slot("free_raffle.free_raffle_expiration_worker")
        await worker_slot.__aenter__()

        async def _run_once() -> int:
            return await self.process_expired_raffles()

        try:
            acquired, processed = await run_with_advisory_lock(db, "worker:free_raffle:expire", _run_once)
            if not acquired:
                return
            # process_expired_raffles already logs appropriately based on processed
            _ = processed
        except Exception:
            log.exception("Free raffle expiration worker error")
        finally:
            await worker_slot.__aexit__(None, None, None)

    @free_raffle_expiration_worker.before_loop
    async def before_free_raffle_expiration_worker(self) -> None:
        await wait_until_initialized(timeout=30.0)
        await self.bot.wait_until_ready()
        await sleep_startup_jitter("free_raffle.free_raffle_expiration_worker")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FreeRaffleCog(bot))
