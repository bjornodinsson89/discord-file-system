from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.free_raffle_repo import FreeRaffleRepository
from repositories.torn_items import TornItemsRepository
from utils import GuildSettingsRepository, get_database, require_api_key
from utils.database import get_pool, is_initialized as db_is_initialized, wait_until_initialized
from utils.advisory_lock import run_with_advisory_lock
from utils.worker_throttle import db_heavy_worker_slot, sleep_startup_jitter
from utils.panel_edit_safety import PANEL_EDIT_SAFETY
from views.free_raffle_views import EnterRaffleView, HostControlsView
from utils.embeds import clamp_percent, format_remaining_time, render_text_progress_bar

log = logging.getLogger("happy_jumper.free_raffle")

FREE_RAFFLE_MIN_DAYS = 1
FREE_RAFFLE_MAX_DAYS = 30
FREE_RAFFLE_EXPIRY_BATCH_SIZE = 10
GIVEAWAY_ENTRY_MODE_CHOICES = {
    "button": {
        "label": "Button join",
        "button_join_enabled": True,
        "auto_entry_enabled": False,
        "weighted_enabled": False,
    },
    "auto": {
        "label": "Auto entry",
        "button_join_enabled": False,
        "auto_entry_enabled": True,
        "weighted_enabled": False,
    },
    "button_weighted": {
        "label": "Button join + weighted",
        "button_join_enabled": True,
        "auto_entry_enabled": False,
        "weighted_enabled": True,
    },
    "auto_weighted": {
        "label": "Auto entry + weighted",
        "button_join_enabled": False,
        "auto_entry_enabled": True,
        "weighted_enabled": True,
    },
}


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


class FreeRaffleModal(discord.ui.Modal, title="Giveaway"):
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

        settings = await GuildSettingsRepository(get_database()).get_or_create(int(interaction.guild_id))
        default_channel_id = GuildSettingsRepository.resolve_raffle_giveaway_purchase_channel_id(settings) or int(interaction.channel_id)
        draft = {
            "guild_id": int(interaction.guild_id),
            "request_channel_id": int(interaction.channel_id),
            "default_post_channel_id": int(default_channel_id),
            "host_discord_id": int(interaction.user.id),
            "prize_text": str(self.prize.value).strip(),
            "note_text": (str(self.note.value).strip() or None),
            "duration_days": duration_days,
        }
        self.cog.store_create_draft(int(interaction.user.id), draft)
        await interaction.response.defer(ephemeral=True, thinking=False)
        await interaction.followup.send(
            "Choose the giveaway entry mode and posting channel.",
            ephemeral=True,
            view=GiveawayCreateFlowView(self.cog, int(interaction.user.id), int(default_channel_id)),
        )


class GiveawayEntryModeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=choice["label"], value=key)
            for key, choice in GIVEAWAY_ENTRY_MODE_CHOICES.items()
        ]
        super().__init__(placeholder="Select giveaway entry mode", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, GiveawayCreateFlowView):
            await view.set_mode(interaction, self.values[0])


class GiveawayPostChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, default_channel_id: int | None):
        super().__init__(
            placeholder="Choose where to post the giveaway",
            min_values=0,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=1,
            default_values=[discord.Object(id=default_channel_id)] if default_channel_id else [],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, GiveawayCreateFlowView):
            selected = self.values[0] if self.values else None
            await view.set_channel(interaction, getattr(selected, "id", None))


class GiveawayCreateFlowView(discord.ui.View):
    def __init__(self, cog: "FreeRaffleCog", owner_id: int, default_channel_id: int | None):
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.mode_key = "button"
        self.post_channel_id = default_channel_id
        self.add_item(GiveawayEntryModeSelect())
        self.add_item(GiveawayPostChannelSelect(default_channel_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the giveaway host can use this flow.", ephemeral=True)
            return False
        return True

    async def set_mode(self, interaction: discord.Interaction, mode_key: str) -> None:
        self.mode_key = mode_key if mode_key in GIVEAWAY_ENTRY_MODE_CHOICES else "button"
        await interaction.response.defer(ephemeral=True, thinking=False)

    async def set_channel(self, interaction: discord.Interaction, channel_id: int | None) -> None:
        if channel_id:
            self.post_channel_id = int(channel_id)
        await interaction.response.defer(ephemeral=True, thinking=False)

    @discord.ui.button(label="Create Giveaway", style=discord.ButtonStyle.success, row=2)
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self.cog.finish_giveaway_create(
            interaction,
            owner_id=self.owner_id,
            mode_key=self.mode_key,
            override_channel_id=self.post_channel_id,
        )


class FreeRaffleCog(commands.Cog):
    giveaway = app_commands.Group(name="giveaway", description="Giveaway commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._views_registered = False
        self._ready_init_lock = asyncio.Lock()
        self._create_drafts: dict[int, dict] = {}

    async def cog_load(self) -> None:
        self._views_registered = False
        self._ready_init_lock = asyncio.Lock()
        self._create_drafts = {}

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
                    self.bot.add_view(
                        self.build_free_raffle_view(
                            raffle_id,
                            status=str(raffle.get("status") or ""),
                            button_join_enabled=bool(raffle.get("button_join_enabled", False)),
                        )
                    )
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
        await self._send_ephemeral(interaction, "This giveaway is now drawn automatically when it ends.")

    @giveaway.command(name="start", description="Start a giveaway")
    async def start(self, interaction: discord.Interaction) -> None:
        db = get_database()
        if not await require_api_key(interaction, db, "start a giveaway"):
            return
        await interaction.response.send_modal(FreeRaffleModal(self))

    def public_view(self, raffle_id: int, *, disabled: bool = False) -> EnterRaffleView:
        return EnterRaffleView(raffle_id=raffle_id, on_enter=self.handle_enter, disabled=disabled)

    def build_free_raffle_view(
        self,
        raffle_id: int,
        host_discord_id: int | None = None,
        status: str | None = None,
        button_join_enabled: bool | None = None,
        **_: object,
    ) -> EnterRaffleView:
        disabled = str(status or "").lower() != "active"
        return EnterRaffleView(
            raffle_id=raffle_id,
            on_enter=self.handle_enter,
            disabled=disabled,
            show_join_button=bool(button_join_enabled),
        )

    def host_controls_view(self, raffle_id: int, *, disabled: bool = False, can_reroll: bool = False) -> HostControlsView:
        return HostControlsView(
            raffle_id=raffle_id,
            on_end_now=self.handle_end_now,
            on_cancel=self.handle_cancel,
            on_refresh=self.handle_refresh_controls,
            on_view_entrants=self.handle_view_entrants,
            on_reroll=self.handle_reroll,
            disabled=disabled,
            can_reroll=can_reroll,
        )

    def store_create_draft(self, user_id: int, draft: dict) -> None:
        self._create_drafts[int(user_id)] = dict(draft)

    def pop_create_draft(self, user_id: int) -> dict | None:
        return self._create_drafts.pop(int(user_id), None)

    async def _resolve_post_channel(
        self, interaction: discord.Interaction, channel_id: int
    ) -> discord.abc.Messageable | None:
        guild = interaction.guild
        if guild is None:
            return interaction.channel if hasattr(interaction.channel, "send") else None
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            try:
                fetched = await guild.fetch_channel(int(channel_id))
                if hasattr(fetched, "send"):
                    channel = fetched
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "send"):
            return None
        return channel

    async def finish_giveaway_create(
        self,
        interaction: discord.Interaction,
        *,
        owner_id: int,
        mode_key: str,
        override_channel_id: int | None,
    ) -> None:
        draft = self.pop_create_draft(owner_id)
        if not draft:
            await interaction.followup.send("❌ Giveaway creation expired. Please run `/giveaway start` again.", ephemeral=True)
            return
        mode = GIVEAWAY_ENTRY_MODE_CHOICES.get(mode_key, GIVEAWAY_ENTRY_MODE_CHOICES["button"])
        post_channel_id = int(override_channel_id or draft["default_post_channel_id"] or draft["request_channel_id"])
        post_channel = await self._resolve_post_channel(interaction, post_channel_id)
        if post_channel is None:
            await interaction.followup.send("❌ The selected giveaway channel is invalid or inaccessible.", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        ends_at = now + timedelta(days=int(draft["duration_days"]))
        try:
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.create_raffle(
                guild_id=int(draft["guild_id"]),
                channel_id=post_channel_id,
                host_discord_id=int(draft["host_discord_id"]),
                prize_text=str(draft["prize_text"]),
                note_text=draft.get("note_text"),
                ends_at=ends_at,
                button_join_enabled=bool(mode["button_join_enabled"]),
                auto_entry_enabled=bool(mode["auto_entry_enabled"]),
                weighted_enabled=bool(mode["weighted_enabled"]),
            )
            raffle_id = int(raffle["id"])
            embed = await self.build_raffle_embed(raffle)
            public_view = self.build_free_raffle_view(
                raffle_id,
                status=str(raffle.get("status") or ""),
                button_join_enabled=bool(raffle.get("button_join_enabled", False)),
            )
            public_message = await post_channel.send(embed=embed, view=public_view)
            await repo.set_message_id(raffle_id, int(public_message.id))
            interaction.client.dispatch(
                "giveaway_started",
                {"guild_id": int(draft["guild_id"]), "giveaway_id": raffle_id, "id": raffle_id},
            )
            await interaction.followup.send(
                f"✅ Giveaway created in <#{post_channel_id}> with **{mode['label']}** mode.",
                ephemeral=True,
                view=self.host_controls_view(raffle_id),
            )
        except Exception:
            log.exception("Failed creating free raffle")
            await interaction.followup.send("❌ Failed to create giveaway. Please try again.", ephemeral=True)

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
        button_join_enabled = bool(raffle.get("button_join_enabled", False))
        auto_entry_enabled = bool(raffle.get("auto_entry_enabled", False))
        weighted_enabled = bool(raffle.get("weighted_odds_enabled", False))
        if button_join_enabled and weighted_enabled:
            entry_mode = "Button join + weighted"
        elif button_join_enabled:
            entry_mode = "Button join"
        elif auto_entry_enabled and weighted_enabled:
            entry_mode = "Auto entry + weighted"
        elif auto_entry_enabled:
            entry_mode = "Auto entry"
        else:
            entry_mode = "Unknown"

        ends_at = raffle.get("ends_at")
        ends_line = ""
        if isinstance(ends_at, datetime):
            ends_unix = int(ends_at.astimezone(timezone.utc).timestamp())
            ends_line = f"\n**Ends:** <t:{ends_unix}:R> (<t:{ends_unix}:f>)"

        thumbnail_url = await self.resolve_thumbnail(prize_text)
        title = "🎉 GIVEAWAY 🎉" if thumbnail_url else "🎉 GIVEAWAY 🎉 🎁"
        now = datetime.now(timezone.utc)
        created_at = raffle.get("created_at")
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        end_time = ends_at if isinstance(ends_at, datetime) else None
        total_seconds = int((end_time - created_at).total_seconds()) if end_time and created_at else 0
        elapsed_seconds = int((now - created_at).total_seconds()) if end_time and created_at else 0
        time_percent = clamp_percent((elapsed_seconds / total_seconds) * 100) if total_seconds > 0 else 0

        embed = discord.Embed(
            title=title,
            description="Join now for a chance to win.",
            color=color,
        )
        embed.add_field(
            name="LIVE STATS",
            value=(
                f"Entrants: **{entry_count}**\n"
                f"Time: `{render_text_progress_bar(time_percent)}`\n"
                f"Status: **{status}**"
            ),
            inline=False,
        )
        embed.add_field(name="PRIZE", value=f"🪓 {prize_text}", inline=False)
        embed.add_field(
            name="GIVEAWAY INFO",
            value=(
                f"Host: <@{int(raffle['host_discord_id'])}>\n"
                f"Ends: {ends_line.strip() if ends_line else 'Unknown'}\n"
                f"Remaining: {format_remaining_time(end_time, now)}\n"
                f"Entry mode: **{entry_mode}**\n"
                f"Weighted odds: **{'Enabled' if weighted_enabled else 'Disabled'}**\n"
                f"Join button: **{'Available' if button_join_enabled else 'Disabled'}**"
            ),
            inline=False,
        )
        how_to_play = []
        if button_join_enabled:
            how_to_play.append("✅ Click **🎟️ Enter Giveaway**")
        if auto_entry_enabled:
            how_to_play.append("✅ Auto-entry giveaway: eligible users are entered automatically")
        how_to_play.append(f"✅ {'Weighted odds are enabled' if weighted_enabled else 'Equal odds for all entrants'}")
        how_to_play.append("✅ Winner announced automatically")
        embed.add_field(
            name="HOW TO PLAY",
            value="\n".join(how_to_play),
            inline=False,
        )
        embed.set_footer(text=f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
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
                button_join_enabled=bool(raffle.get("button_join_enabled", False)),
            )
            await PANEL_EDIT_SAFETY.request_edit(
                message,
                embed=embed,
                view=view,
                min_interval_seconds=5,
                force=str(raffle.get("status") or "").lower() != "active",
            )
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
            if not bool(raffle.get("button_join_enabled", False)):
                await self._send_ephemeral(interaction, "This giveaway uses auto-entry only.")
                return

            inserted = await repo.add_entry_with_source(
                raffle_id,
                int(interaction.user.id),
                entry_source="button",
                entry_weight=1,
                dedupe_key=None,
            )
            if inserted:
                interaction.client.dispatch(
                    "giveaway_joined",
                    {
                        "guild_id": int(raffle.get("guild_id") or interaction.guild_id or 0),
                        "user_id": int(interaction.user.id),
                        "giveaway_id": int(raffle_id),
                        "entry_id": None,
                        "joined_at": datetime.now(timezone.utc),
                        "dedupe_key": f"giveaway_join:{raffle_id}:{int(interaction.user.id)}",
                    },
                )
                if interaction.message is not None:
                    refreshed_raffle = await repo.get_raffle(raffle_id)
                    if refreshed_raffle:
                        embed = await self.build_raffle_embed(refreshed_raffle)
                        view = self.build_free_raffle_view(
                            raffle_id=int(refreshed_raffle["id"]),
                            host_discord_id=int(refreshed_raffle["host_discord_id"]),
                            status=str(refreshed_raffle.get("status") or ""),
                            button_join_enabled=bool(refreshed_raffle.get("button_join_enabled", False)),
                        )
                        await PANEL_EDIT_SAFETY.request_edit(
                            interaction.message,
                            embed=embed,
                            view=view,
                            min_interval_seconds=5,
                            force=False,
                        )
                    else:
                        await self.refresh_public_message(raffle_id)
                else:
                    await self.refresh_public_message(raffle_id)
                await self._send_ephemeral(interaction, "✅ You’re entered in the giveaway.")
                return
            await self._send_ephemeral(interaction, "You’re already entered in this giveaway.")
        except Exception:
            log.exception("Failed handling free raffle entry for raffle_id=%s", raffle_id)
            await self._send_ephemeral(interaction, "Failed to enter giveaway. Please try again.")

    async def _assert_host(self, interaction: discord.Interaction, raffle: dict | None) -> bool:
        if not raffle:
            await self._send_ephemeral(interaction, "Raffle not found.")
            return False
        is_host = int(interaction.user.id) == int(raffle["host_discord_id"])
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        if not is_host and not is_admin:
            await self._send_ephemeral(interaction, "Only the host can do that.")
            return False
        return True

    async def _host_controls_response(self, interaction: discord.Interaction, message: str, raffle: dict) -> None:
        disabled = str(raffle.get("status") or "").lower() != "active"
        can_reroll = str(raffle.get("status") or "").lower() == "ended" and await FreeRaffleRepository(get_pool()).get_winner(int(raffle["id"])) is not None
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True, view=self.host_controls_view(int(raffle["id"]), disabled=disabled, can_reroll=can_reroll))
            return
        await interaction.response.send_message(message, ephemeral=True, view=self.host_controls_view(int(raffle["id"]), disabled=disabled, can_reroll=can_reroll))

    async def handle_cancel(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not await self._assert_host(interaction, raffle):
                return

            if str(raffle.get("status") or "").lower() != "active":
                await self._send_ephemeral(interaction, "Raffle is not active.")
                return

            await repo.set_status(raffle_id, "cancelled", datetime.now(timezone.utc))
            await self.refresh_public_message(raffle_id)
            raffle = await repo.get_raffle(raffle_id) or raffle
            await self._host_controls_response(interaction, "Raffle cancelled.", raffle)
        except Exception:
            log.exception("Failed cancelling free raffle raffle_id=%s", raffle_id)
            await self._send_ephemeral(interaction, "Failed to cancel raffle. Please try again.")

    async def handle_end_now(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not await self._assert_host(interaction, raffle):
                return
            result = await repo.draw_raffle_now(raffle_id)
            if result is None:
                await interaction.followup.send("❌ Giveaway is not active.", ephemeral=True)
                return
            ended_raffle = result["raffle"]
            await self.announce_raffle_result(ended_raffle, result["winner_id"])
            await self.refresh_public_message(raffle_id)
            await self._host_controls_response(interaction, "✅ Giveaway finalized immediately.", ended_raffle)
        except Exception:
            log.exception("Failed ending free raffle raffle_id=%s", raffle_id)
            await interaction.followup.send("❌ Failed to end giveaway right now.", ephemeral=True)

    async def handle_refresh_controls(self, interaction: discord.Interaction, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return
        await self.refresh_public_message(raffle_id)
        await self._host_controls_response(interaction, "🔄 Giveaway panel refreshed.", raffle)

    async def handle_view_entrants(self, interaction: discord.Interaction, raffle_id: int) -> None:
        repo = FreeRaffleRepository(get_pool())
        raffle = await repo.get_raffle(raffle_id)
        if not await self._assert_host(interaction, raffle):
            return
        entries = await repo.list_entries(raffle_id)
        if not entries:
            await self._send_ephemeral(interaction, "No entrants yet.")
            return
        lines = [f"<@{int(entry['discord_id'])}> — weight {int(entry.get('entry_weight') or 1)} via {entry.get('entry_source') or 'unknown'}" for entry in entries[:50]]
        extra = "" if len(entries) <= 50 else f"\n…and {len(entries) - 50} more."
        await self._send_ephemeral(interaction, "📋 Entrants:\n" + "\n".join(lines) + extra)

    async def handle_reroll(self, interaction: discord.Interaction, raffle_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            repo = FreeRaffleRepository(get_pool())
            raffle = await repo.get_raffle(raffle_id)
            if not await self._assert_host(interaction, raffle):
                return
            if str(raffle.get("status") or "").lower() != "ended" or await repo.get_winner(raffle_id) is None:
                await interaction.followup.send("❌ Reroll is only available after the giveaway has ended with a winner.", ephemeral=True)
                return
            winner_id = await repo.reroll_winner(raffle_id)
            updated = await repo.get_raffle(raffle_id) or raffle
            await self.refresh_public_message(raffle_id)
            await self.announce_raffle_result(updated, winner_id)
            await self._host_controls_response(interaction, f"🎲 Winner rerolled to <@{winner_id}>.", updated)
        except Exception:
            log.exception("Failed rerolling free raffle raffle_id=%s", raffle_id)
            await interaction.followup.send("❌ Failed to reroll winner right now.", ephemeral=True)

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
            await channel.send("🎉 Giveaway ended: No entries — no winner.")
            return

        guild = self.bot.get_guild(int(raffle["guild_id"]))
        winner_mention = f"<@{winner_id}>"
        if guild is not None and guild.get_member(winner_id) is None:
            winner_mention = f"<@{winner_id}> (ID: {winner_id})"
        await channel.send(f"🎉 Giveaway winner: {winner_mention}")

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
