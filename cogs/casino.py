from __future__ import annotations

import asyncio
import logging
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

import config
from repositories.slot_assets import has_combo, normalize_combo, upsert_slot_asset
from utils.jump_slots_gif import REEL_CYCLE, render_slots_gif
from views.casino_core.back_of_house import BackOfHouseView, back_of_house_embed
from views.casino_core.casino_home import CasinoHomeView, casino_home_embed
from views.casino_core.cashout_panel import CashoutRequestModal
from views.casino_core.deposit_panel import DepositPanelView, deposit_panel_embed
from views.casino_core.permissions import ensure_casino_admin

log = logging.getLogger("happy_jumper.casino")
DISCORD_SAFE_LIMIT = 7_800_000
FRAMES = 32
DURATION_MS = 110
QUALITY_LADDER = [
    {"max_w": 800, "palette": 96},
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
        combos = [normalize_combo([a, b, c]) for a in symbols for b in symbols for c in symbols]
        start_index = max(0, int(resume_from))
        end_index = len(combos) if int(limit) <= 0 else min(len(combos), start_index + int(limit))

        uploaded = 0
        skipped = 0
        failed: list[str] = []
        failed_details: list[str] = []
        processed = 0

        for idx in range(start_index, end_index):
            combo = combos[idx]
            processed += 1
            smallest_bytes: int | None = None
            last_exc: Exception | None = None
            last_rung: str | None = None

            try:
                if not force and await has_combo(combo):
                    skipped += 1
                    continue

                raw = combo.split(":", 1)[1] if ":" in combo else combo
                reels = [int(x) for x in raw.split(",")]
                success = False

                for rung in QUALITY_LADDER:
                    last_rung = f"{int(rung['max_w'])}/{int(rung['palette'])}"
                    try:
                        gif_bytes = await asyncio.to_thread(
                            render_slots_gif,
                            reels,
                            FRAMES,
                            DURATION_MS,
                            int(rung["max_w"]),
                            int(rung["palette"]),
                        )
                    except Exception as err:
                        last_exc = err
                        log.info(
                            "seed combo=%s rung=%s render_failed err=%s",
                            combo,
                            rung,
                            err,
                        )
                        continue

                    b = len(gif_bytes)
                    smallest_bytes = b if smallest_bytes is None else min(smallest_bytes, b)

                    if b > DISCORD_SAFE_LIMIT:
                        last_exc = ValueError(
                            f"gif_too_large bytes={b} limit={DISCORD_SAFE_LIMIT} rung={last_rung}"
                        )
                        log.info("seed combo=%s rung=%s bytes=%d", combo, rung, b)
                        continue

                    file = discord.File(
                        BytesIO(gif_bytes),
                        filename=f"slots_{reels[0]}_{reels[1]}_{reels[2]}.gif",
                    )

                    try:
                        msg = await channel.send(content=f"slot:{combo}", file=file)
                    except discord.HTTPException as err:
                        last_exc = err
                        if getattr(err, "status", None) == 413:
                            log.info("seed combo=%s rung=%s bytes=%d err=%s", combo, rung, b, err)
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
                    if last_exc is None:
                        last_exc = RuntimeError("seed_failed_unknown")
                    failure_line = (
                        f"{combo} smallest_bytes={smallest_bytes} "
                        f"last_rung={last_rung} err={type(last_exc).__name__}:{last_exc}"
                    )
                    failed_details.append(failure_line)
                    log.error("seed combo=%s failed_all_rungs detail=%s", combo, failure_line)

            except Exception as err:
                last_exc = err
                failed.append(combo)
                failure_line = (
                    f"{combo} smallest_bytes={smallest_bytes} "
                    f"last_rung={last_rung} err={type(last_exc).__name__}:{last_exc}"
                )
                failed_details.append(failure_line)
                log.exception("slot_assets_seed_failed combo=%s", combo)

            await asyncio.sleep(1.2)

        details_preview = "\n".join(failed_details[:5]) if failed_details else ""
        await interaction.followup.send(
            (
                f"Seeding complete. scanned={processed} uploaded={uploaded} "
                f"skipped={skipped} failed={len(failed)}"
                + (f"\nfailed_details:\n{details_preview}" if details_preview else "")
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
