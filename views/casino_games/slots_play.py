from __future__ import annotations

import io

import discord

from services.casino_games.slots import CasinoSlotsService, SlotsCooldownError, SlotsError
from utils.casino_slots_render import render_slots_png


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
        await interaction.response.edit_message(embed=self.view.build_embed(), view=self.view)


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

    def _symbol_name(self, item_id: int) -> str:
        symbols = self.config.get("symbols") or []
        for symbol in symbols:
            if int(symbol.get("item_id") or 0) == int(item_id):
                return str(symbol.get("name") or item_id)
        return str(item_id)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Slots", color=discord.Color.purple())
        embed.add_field(name="Balance", value=f"**{self.balance}** tokens", inline=True)
        embed.add_field(name="Current Bet", value=f"**{self.current_bet}** tokens", inline=True)
        embed.add_field(name="Jackpot Pool", value=f"**{self._pool_label()}**", inline=True)
        embed.set_footer(text="Use Set Bet then Spin")
        return embed

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

            reels = [int(v) for v in (result.get("reels") or [])][:3]
            reel_names = " | ".join(self._symbol_name(item_id) for item_id in reels)
            result_embed = self.build_embed()
            result_embed.description = (
                f"{reel_names}\n"
                f"Bet **{int(result['bet'])}** → Payout **{int(result['payout'])}** "
                f"(Net {int(result['net']):+d})"
            )

            files = []
            try:
                png = await render_slots_png(
                    reels=reels,
                    bet=int(result["bet"]),
                    payout=int(result["payout"]),
                    balance=int(result["balance_after"]),
                    pool_tokens=int(result["pool_after_tokens"]),
                    pool_millis=int(result["pool_after_millis"]),
                )
                files = [discord.File(io.BytesIO(png), filename="slots.png")]
                result_embed.set_image(url="attachment://slots.png")
            except Exception:
                pass

            await interaction.edit_original_response(embed=result_embed, view=self, attachments=files)
            await self.service.post_jackpot_announce(interaction, result)
        except SlotsCooldownError as exc:
            await interaction.followup.send(f"⏳ Wait {exc.remaining_seconds}s", ephemeral=True)
        except SlotsError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        finally:
            button.disabled = False
            await interaction.edit_original_response(view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_state()
        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
