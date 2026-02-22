from __future__ import annotations

import asyncio
import logging
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

import config
from repositories.slot_assets import has_combo, upsert_slot_asset
from utils.jump_slots_gif import REEL_CYCLE, render_slots_gif
from views.casino_core.back_of_house import BackOfHouseView, back_of_house_embed
from views.casino_core.casino_home import CasinoHomeView, casino_home_embed
from views.casino_core.cashout_panel import CashoutRequestModal
from views.casino_core.deposit_panel import DepositPanelView, deposit_panel_embed
from views.casino_core.permissions import ensure_casino_admin

log = logging.getLogger("happy_jumper.casino")
DISCORD_SAFE_LIMIT = 7_800_000
FRAMES = 40
DURATION_MS = 110
QUALITY_LADDER = [
    {"max_w": 900, "palette": 128},
    {"max_w": 850, "palette": 112},
    {"max_w": 800, "palette": 96},
    {"max_w": 750, "palette": 80},
]


class CasinoCog(commands.Cog):
    jump = app_commands.Group(name="jump", description="Jump commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @jump.command(name="casino", description="Open casino home")
    async def casino(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("Guild only command.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=await casino_home_embed(interaction.guild_id, interaction.user.id),
            view=CasinoHomeView(interaction.guild_id),
            ephemeral=True,
        )

    @jump.command(name="casino_deposit", description="Open casino deposit panel")
    async def casino_deposit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await deposit_panel_embed(interaction.guild_id),
            view=DepositPanelView(interaction.guild_id),
            ephemeral=True,
        )

    @jump.command(name="casino_cashout", description="Request casino cashout")
    async def casino_cashout(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CashoutRequestModal(interaction.guild_id))

    @app_commands.command(name="back_of_house", description="Casino back of house")
    @app_commands.guild_only()
    async def back_of_house(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message(
            embed=await back_of_house_embed(interaction.guild_id),
            view=BackOfHouseView(interaction.guild_id),
            ephemeral=True,
        )

    @app_commands.command(
        name="seed_slot_assets", description="Seed slot machine GIF assets into the assets channel"
    )
    @app_commands.guild_only()
    async def seed_slot_assets(
        self,
        interaction: discord.Interaction,
        resume_from: int = 0,
        limit: int = 0,
        force: bool = False,
    ):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        if interaction.guild_id != config.SLOT_ASSETS_GUILD_ID:
            await interaction.response.send_message(
                "❌ This command is only allowed in the configured assets guild.", ephemeral=True
            )
            return
        if not config.SLOT_ASSETS_CHANNEL_ID:
            await interaction.response.send_message(
                "❌ SLOT_ASSETS_CHANNEL_ID is not configured.", ephemeral=True
            )
            return

        await interaction.response.send_message("Seeding started...", ephemeral=True)

        guild = self.bot.get_guild(int(config.SLOT_ASSETS_GUILD_ID))
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(int(config.SLOT_ASSETS_GUILD_ID))
            except Exception:
                await interaction.followup.send("❌ Could not load assets guild.", ephemeral=True)
                return

        channel = guild.get_channel(int(config.SLOT_ASSETS_CHANNEL_ID)) if guild else None
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(config.SLOT_ASSETS_CHANNEL_ID))
            except Exception:
                await interaction.followup.send("❌ Could not load assets channel.", ephemeral=True)
                return

        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Assets channel must be a text channel.", ephemeral=True
            )
            return

        perms = channel.permissions_for(channel.guild.me) if channel.guild and channel.guild.me else None
        if perms and not (perms.send_messages and perms.attach_files):
            await interaction.followup.send(
                "❌ Bot missing Send Messages/Attach Files in assets channel.", ephemeral=True
            )
            return

        symbols = list(REEL_CYCLE)
        combos = [f"{a},{b},{c}" for a in symbols for b in symbols for c in symbols]
        start_index = max(0, int(resume_from))
        end_index = len(combos) if int(limit) <= 0 else min(len(combos), start_index + int(limit))

        uploaded = 0
        skipped = 0
        failed: list[str] = []
        processed = 0

        for idx in range(start_index, end_index):
            combo = combos[idx]
            processed += 1
            smallest_bytes: int | None = None

            try:
                if not force and await has_combo(combo):
                    skipped += 1
                    continue

                reels = [int(x) for x in combo.split(",")]
                success = False

                for rung in QUALITY_LADDER:
                    gif_bytes = await asyncio.to_thread(
                        render_slots_gif,
                        reels,
                        FRAMES,
                        DURATION_MS,
                        int(rung["max_w"]),
                        int(rung["palette"]),
                    )

                    gif_len = len(gif_bytes)
                    if smallest_bytes is None or gif_len < smallest_bytes:
                        smallest_bytes = gif_len

                    if gif_len > DISCORD_SAFE_LIMIT:
                        log.info("seed combo=%s rung=%s bytes=%d", combo, rung, gif_len)
                        continue

                    file = discord.File(
                        BytesIO(gif_bytes),
                        filename=f"slots_{reels[0]}_{reels[1]}_{reels[2]}.gif",
                    )

                    try:
                        msg = await channel.send(content=f"slot:{combo}", file=file)
                    except discord.HTTPException as err:
                        if getattr(err, "status", None) == 413:
                            log.info("seed combo=%s rung=%s bytes=%d", combo, rung, gif_len)
                            continue
                        raise

                    if not msg.attachments or not msg.attachments[0].url:
                        raise RuntimeError("Missing attachment URL from sent message")

                    await upsert_slot_asset(
                        combo,
                        msg.attachments[0].url,
                        msg.id,
                        frames=FRAMES,
                        duration_ms=DURATION_MS,
                        max_w=int(rung["max_w"]),
                        palette_colors=int(rung["palette"]),
                    )
                    uploaded += 1
                    success = True
                    if uploaded % 10 == 0:
                        await interaction.followup.send(
                            (
                                f"Progress: uploaded={uploaded} processed={processed} "
                                f"skipped={skipped} failed={len(failed)}"
                            ),
                            ephemeral=True,
                        )
                    break

                if not success:
                    failed.append(combo)
                    log.error(
                        "seed combo=%s failed_all_rungs smallest_bytes=%s",
                        combo,
                        smallest_bytes,
                    )

            except Exception:
                failed.append(combo)
                log.exception("slot_assets_seed_failed combo=%s", combo)

            await asyncio.sleep(1.2)

        await interaction.followup.send(
            (
                f"Seeding complete. scanned={processed} uploaded={uploaded} "
                f"skipped={skipped} failed={len(failed)}"
                + (f" failed_combos={', '.join(failed[:20])}" if failed else "")
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    cog = CasinoCog(bot)
    await bot.add_cog(cog)
    try:
        bot.tree.add_command(cog.jump)
    except Exception:
        pass
    try:
        bot.tree.add_command(cog.back_of_house)
    except Exception:
        pass
    try:
        bot.tree.add_command(cog.seed_slot_assets)
    except Exception:
        pass
