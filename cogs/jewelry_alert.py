from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import GuildSettingsRepository, get_security_manager, get_torn_api
from utils.command_checks import require_command_access
from utils.database import get_database, is_initialized, wait_until_initialized
from utils.embeds import create_error_embed
from utils.torn_api import TornAPIError

PAUL_WALL_IMAGE_URL = "https://upload.wikimedia.org/wikipedia/commons/9/99/Paul_Wall.jpg"
SHOPLIFT_URL = "https://www.torn.com/page.php?sid=crimes#/shoplifting"
LOG_THROTTLE_SECONDS = 600

log = logging.getLogger("happy_jumper.jewelry_alert")


class JewelryAlertCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._db = get_database()
        self._repo = GuildSettingsRepository(self._db)
        self._log_throttle_until: dict[tuple[int, str], float] = {}
        self.jewelry_alert_poller.start()

    def cog_unload(self) -> None:
        self.jewelry_alert_poller.cancel()

    def _build_jewelry_embed_and_view(self) -> tuple[discord.Embed, discord.ui.View]:
        embed = discord.Embed(
            title="Jewlery store wide open",
            description="You have ~10 minutes for Cluster Ring merit",
            color=discord.Color.gold(),
        )
        embed.set_image(url=PAUL_WALL_IMAGE_URL)
        embed.set_footer(text="Paul Wall approves ✅")

        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label="SHOPLIFT NOW",
                url=SHOPLIFT_URL,
            )
        )
        return embed, view

    def _build_mentions(self, guild: discord.Guild, role_ids: list[int]) -> str | None:
        mentions: list[str] = []
        for role_id in role_ids:
            role = guild.get_role(int(role_id))
            if role is not None:
                mentions.append(role.mention)
        return " ".join(mentions) if mentions else None

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
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel | discord.NewsChannel):
            try:
                fetched = await guild.fetch_channel(channel_id)
            except Exception:
                fetched = None
            if isinstance(fetched, discord.TextChannel | discord.NewsChannel):
                channel = fetched

        if not isinstance(channel, discord.TextChannel | discord.NewsChannel):
            self._log_throttled(guild.id, "missing_channel", "Jewelry alert channel unavailable guild_id=%s", guild.id)
            return None

        embed, view = self._build_jewelry_embed_and_view()
        content = self._build_mentions(guild, role_ids)
        try:
            sent = await channel.send(content=content, embed=embed, view=view)
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

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel | discord.NewsChannel):
            try:
                fetched = await guild.fetch_channel(channel_id)
            except Exception:
                fetched = None
            if isinstance(fetched, discord.TextChannel | discord.NewsChannel):
                channel = fetched

        if not isinstance(channel, discord.TextChannel | discord.NewsChannel):
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

        last_clear = bool(settings.get("jewelry_alert_last_clear"))
        active_message_id = settings.get("jewelry_alert_active_message_id")

        if is_clear and not last_clear:
            message_id = await self._send_announcement(guild=guild, channel_id=channel_id, role_ids=role_ids)
            if message_id is not None:
                await self._repo.upsert_settings(
                    guild.id,
                    jewelry_alert_last_clear=True,
                    jewelry_alert_active_message_id=message_id,
                )
            return

        if (not is_clear) and last_clear:
            await self._delete_announcement(
                guild=guild,
                channel_id=channel_id,
                message_id=int(active_message_id) if active_message_id else None,
            )
            await self._repo.upsert_settings(
                guild.id,
                jewelry_alert_last_clear=False,
                jewelry_alert_active_message_id=None,
            )

    @tasks.loop(seconds=30)
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
        sent_id = await self._send_announcement(guild=guild, channel_id=channel_id, role_ids=role_ids)
        if sent_id is None:
            await interaction.response.send_message(
                embed=create_error_embed("Send failed", "Could not post in the configured jewelry alert channel."),
                ephemeral=True,
            )
            return

        channel = guild.get_channel(channel_id)
        channel_mention = channel.mention if isinstance(channel, discord.abc.GuildChannel) else "the configured channel"
        await interaction.response.send_message(
            f"Sent test jewelry announcement to {channel_mention}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(JewelryAlertCog(bot))
