from __future__ import annotations

import logging
from io import BytesIO

import discord

from services.casino_games.slots import CasinoSlotsService, SlotsCooldownError, SlotsError
from utils.jump_slots_gif import render_slots_gif

log = logging.getLogger("happy_jumper.casino.slots")


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
            await interaction.response.send_message("❌ Bet must be an integer.", ephemeral=True)
            return

        if value < self.view.min_bet or value > self.view.max_bet:
            await interaction.response.send_message(
                f"❌ Bet must be between {self.view.min_bet} and {self.view.max_bet} tokens.",
                ephemeral=True,
            )
            return

        self.view.current_bet = value
        await self.view.refresh_state()
        await interaction.response.edit_message(content=self.view.build_content(), embed=self.view.build_embed(), view=self.view)


class SlotsPlayView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        discord_id: int,
        service: CasinoSlotsService,
        config: dict,
        balance: int,
        pool_tokens: int,
        pool_millis: int,
    ):
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
        if self.pool_millis > 0:
            return f"{self.pool_tokens}.{self.pool_millis:03d}"
        return str(self.pool_tokens)

    def _status_embed(self, jackpot_str: str, status: str, payout: int | None) -> discord.Embed:
        em = discord.Embed(title="🎰 7️⃣7️⃣7️⃣  S L O T S  7️⃣7️⃣7️⃣ 🎰")
        desc = f"**Jackpot:** `{jackpot_str}`\n\n**{status}**"
        if payout is not None:
            desc += f"\n**Payout:** `{payout}`"
        em.description = desc
        return em

    def build_embed(self) -> discord.Embed:
        return self._status_embed(self._pool_label(), "R E A D Y", None)

    def build_content(self) -> str:
        return ""

    async def refresh_state(self) -> None:
        snapshot = await self.service.get_balance_and_pool(self.guild_id, self.discord_id)
        self.balance = int(snapshot["balance"])
        self.pool_tokens = int(snapshot["pool_tokens"])
        self.pool_millis = int(snapshot["pool_millis"])
        self.config = dict(snapshot["config"])
        self.min_bet = int(self.config.get("min_bet") or 1)
        self.max_bet = int(self.config.get("max_bet") or self.min_bet)
        self.current_bet = max(self.min_bet, min(self.current_bet, self.max_bet))

    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary)
    async def set_bet(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SlotsBetModal(self))

    @discord.ui.button(label="Spin 🎰", style=discord.ButtonStyle.success)
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.defer()
        await interaction.edit_original_response(view=self)

        try:
            result = await self.service.spin(self.guild_id, self.discord_id, self.current_bet)
            self.balance = int(result["balance_after"])
            self.pool_tokens = int(result["pool_after_tokens"])
            self.pool_millis = int(result["pool_after_millis"])
            self.config = dict(result.get("config") or self.config)

            final_reels = [int(v) for v in (result.get("reels") or [])][:3]
            while len(final_reels) < 3:
                final_reels.append(281)

            payout = int(result["payout"])
            bet = int(result["bet"])
            win_type = str(result.get("win_type") or "")
            if win_type == "jackpot":
                final_status = "J A C K P O T 💰"
            elif payout <= 0:
                final_status = "L O S E ☹️"
            elif payout == bet:
                final_status = "P U S H 😐"
            else:
                final_status = "W I N ✅"

            gif_bytes = render_slots_gif(final_reels)
            gif_file = discord.File(BytesIO(gif_bytes), filename="slots.gif")
            result_embed = discord.Embed(
                title="JUMP SLOTS",
                description=f"**{final_status}**\n**Payout:** `{payout}`",
            )
            result_embed.set_image(url="attachment://slots.gif")
            await interaction.followup.send(embed=result_embed, file=gif_file, ephemeral=True)

            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(self._pool_label(), "R E A D Y", None),
                view=self,
            )

            await self.service.post_jackpot_announce(interaction, result)
        except SlotsCooldownError as exc:
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(self._pool_label(), f"⏳ Wait {exc.remaining_seconds}s", None),
                view=self,
            )
        except SlotsError as exc:
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(self._pool_label(), f"❌ {exc}", None),
                view=self,
            )
        except Exception:
            log.exception("slots.spin_failed")
            await interaction.followup.send("❌ Slots error. Check logs.", ephemeral=True)
        finally:
            button.disabled = False
            await interaction.edit_original_response(view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_state()
        await interaction.edit_original_response(content="", embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
