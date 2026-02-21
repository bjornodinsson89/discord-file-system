from __future__ import annotations

import asyncio
import io

import discord

from services.casino_games.slots import CasinoSlotsService, SlotsCooldownError, SlotsError
from utils.casino_slots_render import SlotsRenderError, render_slots_png

ITEM_NAMES = {
    206: "Xanax",
    366: "Erotic DVD",
    197: "Ecstasy",
    865: "Poison Mistletoe",
    394: "Brick",
    707: "Lump of Coal",
    274: "Panda Plushie",
    281: "Lion Plushie",
}


class SlotsBetModal(discord.ui.Modal, title="Set Slots Bet"):
    bet = discord.ui.TextInput(label="Bet (tokens)", required=True, max_length=12)

    def __init__(self, view: "SlotsPlayView"):
        super().__init__()
        self.view = view
        self.bet.default = str(view.current_bet)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(str(self.bet.value).strip())
        except Exception:
            await interaction.response.send_message("Please enter a valid integer bet.", ephemeral=True)
            return
        if value < self.view.min_bet or value > self.view.max_bet:
            await interaction.response.send_message(f"Bet must be between {self.view.min_bet} and {self.view.max_bet}.", ephemeral=True)
            return
        self.view.current_bet = value
        await interaction.response.send_message("✅ Bet updated.", ephemeral=True)


class SlotsPlayView(discord.ui.View):
    def __init__(self, guild_id: int, discord_id: int, service: CasinoSlotsService, config: dict, balance: int, pool_tokens: int, pool_millis: int):
        super().__init__(timeout=300)
        self.guild_id = int(guild_id)
        self.discord_id = int(discord_id)
        self.service = service
        self.config = dict(config or {})
        self.min_bet = int(self.config.get("min_bet") or 1)
        self.max_bet = int(self.config.get("max_bet") or self.min_bet)
        self.current_bet = self.min_bet
        self.balance = int(balance)
        self.pool_tokens = int(pool_tokens)
        self.pool_millis = int(pool_millis)

    def _pool_label(self) -> str:
        return f"{self.pool_tokens}.{self.pool_millis:03d}"

    def build_embed(self, title: str = "🎰 Slots", description: str | None = None) -> discord.Embed:
        em = discord.Embed(title=title, color=discord.Color.purple())
        em.description = description or "Set your bet, then hit **SPIN 🎰**."
        em.add_field(name="Balance", value=f"**{self.balance}** tokens", inline=True)
        em.add_field(name="Current Bet", value=f"**{self.current_bet}** tokens", inline=True)
        em.add_field(name="Jackpot Pool", value=f"**{self._pool_label()}**", inline=True)
        return em

    async def refresh_state(self):
        snapshot = await self.service.get_balance_and_pool(self.guild_id, self.discord_id)
        self.balance = int(snapshot["balance"])
        self.pool_tokens = int(snapshot["pool_tokens"])
        self.pool_millis = int(snapshot["pool_millis"])
        self.config = dict(snapshot["config"])
        self.min_bet = int(self.config.get("min_bet") or 1)
        self.max_bet = int(self.config.get("max_bet") or self.min_bet)
        if self.current_bet < self.min_bet:
            self.current_bet = self.min_bet
        if self.current_bet > self.max_bet:
            self.current_bet = self.max_bet

    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary)
    async def set_bet(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SlotsBetModal(self))

    @discord.ui.button(label="SPIN 🎰", style=discord.ButtonStyle.success)
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)

        try:
            if self.config.get("animate", True):
                frames = max(1, int(self.config.get("animation_frames") or 6))
                for _i in range(min(frames, 6)):
                    desc = " | ".join("❔" for _ in range(3))
                    await interaction.edit_original_response(embed=self.build_embed(description=f"Spinning... {desc}"), view=self)
                    await asyncio.sleep(0.2)

            result = await self.service.spin(self.guild_id, self.discord_id, self.current_bet)
            self.balance = int(result["balance_after"])
            self.pool_tokens = int(result["pool_after_tokens"])
            self.pool_millis = int(result["pool_after_millis"])
            reel_names = " | ".join(ITEM_NAMES.get(int(i), str(i)) for i in result["reels"])
            win_label = str(result["win_type"]).replace("_", " ").title()
            desc = f"{reel_names}\nBet **{result['bet']}** → Payout **{result['payout']}** (Net {result['net']:+d})"
            embed = self.build_embed(title=f"🎰 Slots — {win_label}", description=desc)

            files = []
            try:
                png = await render_slots_png(
                    reels=[int(x) for x in result["reels"]],
                    bet=int(result["bet"]),
                    payout=int(result["payout"]),
                    balance=int(result["balance_after"]),
                    pool_tokens=int(result["pool_after_tokens"]),
                    pool_millis=int(result["pool_after_millis"]),
                    win_label=win_label,
                )
                files = [discord.File(io.BytesIO(png), filename="slots.png")]
                embed.set_image(url="attachment://slots.png")
            except SlotsRenderError:
                pass

            await interaction.edit_original_response(embed=embed, attachments=files, view=self)
            await self.service.post_jackpot_announce(interaction, result)
        except SlotsCooldownError as exc:
            await interaction.edit_original_response(embed=self.build_embed(description=f"⏳ Cooldown active. Try again in {exc.remaining_seconds}s."), view=self)
        except SlotsError as exc:
            await interaction.edit_original_response(embed=self.build_embed(description=f"❌ {exc}"), view=self)
        except Exception as exc:
            await interaction.edit_original_response(embed=self.build_embed(description=f"❌ Spin failed: {exc}"), view=self)
        finally:
            button.disabled = False
            await interaction.edit_original_response(view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_state()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)
