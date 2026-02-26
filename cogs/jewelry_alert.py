from __future__ import annotations

import logging
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from utils import GuildSettingsRepository, get_security_manager, get_torn_api
from utils.command_checks import require_command_access
from utils.database import get_database, is_initialized, wait_until_initialized
from utils.embeds import create_error_embed
from utils.torn_api import TornAPIError

SHOPLIFT_URL = "https://www.torn.com/page.php?sid=crimes#/shoplifting"
LOG_THROTTLE_SECONDS = 600
POLL_INTERVAL_SECONDS = 30
MEME_TOP_TEXT = "Hit the jewelry store — tell 'em I need a grill."
MEME_BOTTOM_TEXT = "Paul Wall approves ✅"

log = logging.getLogger("happy_jumper.jewelry_alert")


class JewelryAlertCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db = get_database()
        self._repo = GuildSettingsRepository(self._db)
        self._log_throttle_until: dict[tuple[int, str], float] = {}
        self._meme_image_bytes: bytes | None = None
        self.jewelry_alert_poller.start()

    def cog_unload(self) -> None:
        self.jewelry_alert_poller.cancel()

    def _build_jewelry_embed_and_view(self, blocked_roles: list[str]) -> tuple[discord.Embed, discord.ui.View]:
        description = "You have a 10 minute window for the Cluster Ring merit."
        if blocked_roles:
            description += f"\n\n⚠️ Not mentionable: {', '.join(blocked_roles)}"

        embed = discord.Embed(
            title="Jewelry store wide open",
            description=description,
            color=discord.Color.gold(),
        )
        embed.set_image(url="attachment://paul_wall.jpg")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="SHOPLIFT NOW",
                url=SHOPLIFT_URL,
            )
        )
        return embed, view

    def _build_mentions_and_unmentionables(
        self,
        guild: discord.Guild,
        role_ids: list[int],
    ) -> tuple[str | None, list[str]]:
        me = guild.me or guild.get_member(guild.client.user.id)
        can_bypass = bool(getattr(me.guild_permissions, "mention_everyone", False)) if me else False
        mentions: list[str] = []
        blocked: list[str] = []
        for rid in role_ids or []:
            try:
                rid_int = int(rid)
            except Exception:
                continue
            role = guild.get_role(rid_int)
            if not role:
                continue
            if role.mentionable or can_bypass:
                mentions.append(role.mention)
            else:
                blocked.append(role.name)
        return (" ".join(mentions).strip() or None, blocked)

    @staticmethod
    def _has_send_permissions(channel: discord.abc.GuildChannel, me: discord.Member) -> bool:
        perms = channel.permissions_for(me)
        return bool(perms.view_channel and perms.send_messages and perms.embed_links and perms.attach_files)

    @staticmethod
    def _has_thread_send_permissions(channel: discord.Thread, me: discord.Member) -> bool:
        perms = channel.permissions_for(me)
        return bool(perms.view_channel and perms.send_messages_in_threads and perms.embed_links and perms.attach_files)

    async def _resolve_messageable_channel(self, guild: discord.Guild, channel_id: int) -> discord.abc.Messageable | None:
        channel: discord.abc.GuildChannel | discord.Thread | None = guild.get_channel(channel_id)
        if channel is None:
            try:
                fetched = await guild.fetch_channel(channel_id)
            except Exception:
                fetched = None
            if isinstance(fetched, (discord.abc.GuildChannel, discord.Thread)):
                channel = fetched

        if channel is None or not isinstance(channel, discord.abc.Messageable):
            return None

        me = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        if me and isinstance(channel, discord.Thread) and not self._has_thread_send_permissions(channel, me):
            return None
        if me and isinstance(channel, discord.abc.GuildChannel) and not isinstance(channel, discord.Thread) and not self._has_send_permissions(channel, me):
            return None
        return channel

    def _get_meme_asset_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "assets" / "paul_wall.jpg"

    def _render_meme_bytes(self) -> bytes:
        if self._meme_image_bytes is not None:
            return self._meme_image_bytes

        asset_path = self._get_meme_asset_path()
        with Image.open(asset_path).convert("RGB") as base_image:
            draw = ImageDraw.Draw(base_image)
            font = ImageFont.load_default()
            width, height = base_image.size
            margin = max(12, width // 30)

            def draw_caption(text: str, y: int) -> None:
                text_bbox = draw.textbbox((0, 0), text, font=font, stroke_width=2)
                text_w = text_bbox[2] - text_bbox[0]
                x = max(margin, (width - text_w) // 2)
                draw.text(
                    (x, y),
                    text,
                    fill="white",
                    stroke_fill="black",
                    stroke_width=2,
                    font=font,
                )

            draw_caption(MEME_TOP_TEXT, margin)
            bottom_bbox = draw.textbbox((0, 0), MEME_BOTTOM_TEXT, font=font, stroke_width=2)
            bottom_h = bottom_bbox[3] - bottom_bbox[1]
            draw_caption(MEME_BOTTOM_TEXT, max(margin, height - bottom_h - margin))

            payload = BytesIO()
            base_image.save(payload, format="JPEG", quality=88, optimize=True)
            self._meme_image_bytes = payload.getvalue()
        return self._meme_image_bytes

    def _build_meme_file(self) -> discord.File | None:
        try:
            meme_bytes = self._render_meme_bytes()
        except Exception:
            log.exception("Failed to render jewelry alert meme asset")
            return None
        return discord.File(BytesIO(meme_bytes), filename="paul_wall.jpg")

    def _log_throttled(self, guild_id: int, error_type: str, message: str, *args: Any) -> None:
        now = time.monotonic()
        key = (guild_id, error_type)
        expiry = self._log_throttle_until.get(key, 0.0)
        if now < expiry:
            return
        self._log_throttle_until[key] = now + LOG_THROTTLE_SECONDS
        log.warning(message, *args)

    async def _send_announcement(
        self,
        *,
        guild: discord.Guild,
        channel_id: int,
        role_ids: list[int],
    ) -> int | None:
        channel = await self._resolve_messageable_channel(guild, channel_id)
        if channel is None:
            self._log_throttled(guild.id, "missing_channel", "Jewelry alert channel unavailable guild_id=%s", guild.id)
            return None

        content, blocked = self._build_mentions_and_unmentionables(guild, role_ids)
        embed, view = self._build_jewelry_embed_and_view(blocked)
        meme_file = self._build_meme_file()
        send_kwargs: dict[str, Any] = {"content": content, "embed": embed, "view": view}
        if meme_file is not None:
            send_kwargs["file"] = meme_file
        try:
            sent = await channel.send(**send_kwargs)
        except discord.Forbidden:
            self._log_throttled(guild.id, "send_forbidden", "Missing permissions to send jewelry alert guild_id=%s", guild.id)
            return None
        except discord.HTTPException:
            log.exception("Failed to send jewelry alert message guild_id=%s", guild.id)
            return None
        return sent.id

    async def _delete_announcement(self, *, guild: discord.Guild, channel_id: int, message_id: int | None) -> None:
        if not message_id:
            return

        channel = await self._resolve_messageable_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            self._log_throttled(guild.id, "missing_channel_delete", "Jewelry alert delete skipped; channel unavailable guild_id=%s", guild.id)
            return

        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            self._log_throttled(guild.id, "delete_forbidden", "Missing permissions to delete jewelry alert guild_id=%s", guild.id)
        except discord.HTTPException:
            log.exception("Failed deleting jewelry alert message guild_id=%s", guild.id)

    async def _poll_guild(self, guild: discord.Guild) -> None:
        settings = await self._repo.get_or_create(guild.id)
        encrypted_key = settings.get("bank_rates_api_key_encrypted")
        channel_id = int(settings.get("jewelry_alert_channel_id") or 0)
        role_ids = GuildSettingsRepository._normalize_role_id_list(
            settings.get("jewelry_alert_role_ids"),
            guild_id=guild.id,
            field_name="jewelry_alert_role_ids",
        )

        if channel_id <= 0:
            return

        if not encrypted_key:
            self._log_throttled(guild.id, "missing_key", "Jewelry alert skipped; API key missing guild_id=%s", guild.id)
            return

        try:
            api_key = get_security_manager().decrypt_api_key(str(encrypted_key))
        except Exception:
            self._log_throttled(guild.id, "decrypt_error", "Jewelry alert skipped; API key decrypt failed guild_id=%s", guild.id)
            return

        torn_success = False
        is_clear = False
        try:
            shoplifting = await get_torn_api().get_shoplifting(api_key)
            torn_success = True
            jewelry_store = shoplifting.get("jewelry_store") if isinstance(shoplifting, dict) else None
            if isinstance(jewelry_store, list) and len(jewelry_store) >= 2:
                cameras_disabled = bool((jewelry_store[0] or {}).get("disabled"))
                guard_disabled = bool((jewelry_store[1] or {}).get("disabled"))
                is_clear = cameras_disabled and guard_disabled
        except TornAPIError:
            self._log_throttled(guild.id, "torn_error", "Jewelry alert poll failed for guild_id=%s", guild.id)
            return
        except Exception:
            log.exception("Unexpected jewelry alert poll error guild_id=%s", guild.id)
            return

        if not torn_success:
            return

        last_clear = bool(settings.get("jewelry_alert_last_is_open") or settings.get("jewelry_alert_last_clear"))
        active_message_id = settings.get("jewelry_alert_last_announcement_message_id") or settings.get("jewelry_alert_active_message_id")
        active_channel_id = settings.get("jewelry_alert_last_announcement_channel_id") or channel_id

        if last_clear and not active_message_id:
            last_clear = False

        if is_clear and not last_clear:
            message_id = await self._send_announcement(guild=guild, channel_id=channel_id, role_ids=role_ids)
            if message_id is not None:
                await self._repo.upsert_settings(
                    guild.id,
                    jewelry_alert_last_is_open=True,
                    jewelry_alert_last_announcement_channel_id=channel_id,
                    jewelry_alert_last_announcement_message_id=message_id,
                    jewelry_alert_last_clear=True,
                    jewelry_alert_active_message_id=message_id,
                )
            return

        if (not is_clear) and last_clear:
            await self._delete_announcement(
                guild=guild,
                channel_id=int(active_channel_id),
                message_id=int(active_message_id) if active_message_id else None,
            )
            await self._repo.upsert_settings(
                guild.id,
                jewelry_alert_last_is_open=False,
                jewelry_alert_last_announcement_channel_id=None,
                jewelry_alert_last_announcement_message_id=None,
                jewelry_alert_last_clear=False,
                jewelry_alert_active_message_id=None,
            )

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def jewelry_alert_poller(self) -> None:
        if not is_initialized():
            return
        for guild in self.bot.guilds:
            try:
                await self._poll_guild(guild)
            except Exception:
                log.exception("Jewelry alert guild poll failed guild_id=%s", guild.id)

    @jewelry_alert_poller.before_loop
    async def before_jewelry_alert_poller(self) -> None:
        await wait_until_initialized(timeout=30.0)
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="test_jewlery_announce",
        description="Send a test jewelry store announcement to the configured channel.",
    )
    @require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
    async def test_jewlery_announce(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=create_error_embed("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        settings = await self._repo.get_or_create(guild.id)
        channel_id = int(settings.get("jewelry_alert_channel_id") or 0)
        if channel_id <= 0:
            await interaction.response.send_message(
                embed=create_error_embed("Not configured", "Set a jewelry alert channel in `/setup` first."),
                ephemeral=True,
            )
            return

        encrypted_key = settings.get("bank_rates_api_key_encrypted")
        if not encrypted_key:
            await interaction.response.send_message(
                embed=create_error_embed("Not configured", "Set the Torn API key in `/setup` → Feature Toggles first."),
                ephemeral=True,
            )
            return

        try:
            get_security_manager().decrypt_api_key(str(encrypted_key))
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed("Configuration error", "Stored API key could not be read. Re-save it in setup."),
                ephemeral=True,
            )
            return

        role_ids = GuildSettingsRepository._normalize_role_id_list(
            settings.get("jewelry_alert_role_ids"),
            guild_id=guild.id,
            field_name="jewelry_alert_role_ids",
        )

        target_channel = await self._resolve_messageable_channel(guild, channel_id)
        if target_channel is None:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Send failed",
                    "Configured jewelry alert channel is missing or I cannot post there. Update `/setup` first.",
                ),
                ephemeral=True,
            )
            return

        sent_id = await self._send_announcement(guild=guild, channel_id=channel_id, role_ids=role_ids)
        if sent_id is None:
            await interaction.response.send_message(
                embed=create_error_embed("Send failed", "Could not post in the configured jewelry alert channel."),
                ephemeral=True,
            )
            return

        channel_mention = getattr(target_channel, "mention", "the configured channel")
        await interaction.response.send_message(
            f"Sent test jewelry announcement to {channel_mention}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JewelryAlertCog(bot))
