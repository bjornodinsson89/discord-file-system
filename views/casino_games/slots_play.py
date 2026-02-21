from __future__ import annotations

import asyncio
from io import BytesIO
import random

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

    def _animation_int(self, key: str, default: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return value

    def _pick_weighted_symbol(self) -> int:
        symbols = list(self.config.get("symbols") or [])
        ids = [int(s.get("item_id") or 0) for s in symbols if int(s.get("item_id") or 0) > 0]
        weights = [max(0, int(s.get("weight") or 0)) for s in symbols if int(s.get("item_id") or 0) > 0]
        if ids and sum(weights) > 0:
            return random.choices(ids, weights=weights, k=1)[0]
        return random.choice([394, 707, 274, 281, 197, 366, 865, 206])

    def _build_frame_embed(
        self,
        reels: list[int],
        spin_mask: list[bool],
        result: dict,
        is_final: bool,
        status_label: str,
    ) -> discord.Embed:
        display = ["???" if spin_mask[idx] else self._symbol_name(reels[idx]) for idx in range(3)]
        body = (
            "```text\n"
            "┌──────────────────────────┐\n"
            f"│  {display[0]:<6}│  {display[1]:<6}│  {display[2]:<6}│\n"
            "└──────────────────────────┘\n"
            "```"
        )

        if not is_final:
            body += "\nS P I N N I N G…"
        else:
            body += f"\n{status_label}"
            body += (
                f"\nBet **{int(result['bet'])}** → Payout **{int(result['payout'])}** "
                f"(Net {int(result['net']):+d})"
                f"\nBalance: **{int(result['balance_after'])}** tokens"
                f"\nJackpot Pool: **{int(result['pool_after_tokens'])}.{int(result['pool_after_millis']):03d}**"
            )

        embed = discord.Embed(
            title="🎰 7️⃣7️⃣7️⃣  S L O T S  7️⃣7️⃣7️⃣ 🎰",
            description=body,
            color=discord.Color.purple(),
        )
        return embed

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
            pool_before_tokens = self.pool_tokens
            pool_before_millis = self.pool_millis
            result = await self.service.spin(self.guild_id, self.discord_id, self.current_bet)
            self.balance = int(result["balance_after"])
            self.pool_tokens = int(result["pool_after_tokens"])
            self.pool_millis = int(result["pool_after_millis"])
            self.config = dict(result.get("config") or self.config)

            final_reels = [int(v) for v in (result.get("reels") or [])][:3]
            while len(final_reels) < 3:
                final_reels.append(self._pick_weighted_symbol())

            frames_total = max(1, self._animation_int("animation_total_frames", 15))
            delay_ms = max(1, self._animation_int("animation_delay_ms", 335))

            lock_left = min(frames_total, max(1, self._animation_int("animation_lock_left", 6)))
            lock_mid = min(frames_total, max(1, self._animation_int("animation_lock_mid", 10)))
            lock_right = min(frames_total, max(1, self._animation_int("animation_lock_right", 14)))

            if lock_mid <= lock_left:
                lock_mid = min(frames_total, lock_left + 1)
            if lock_right <= lock_mid:
                lock_right = min(frames_total, lock_mid + 1)
            if lock_mid <= lock_left:
                lock_left = max(1, lock_mid - 1)
            if lock_right <= lock_mid:
                lock_mid = max(1, lock_right - 1)

            net = int(result["payout"]) - int(result["bet"])
            if int(result["payout"]) <= 0:
                status_label = "L O S E ☹️"
            elif net == 0:
                status_label = "P U S H 😐"
            else:
                status_label = "W I N ✅"

            for frame in range(1, frames_total + 1):
                if frame < lock_left:
                    locked = [False, False, False]
                elif frame < lock_mid:
                    locked = [True, False, False]
                elif frame < lock_right:
                    locked = [True, True, False]
                else:
                    locked = [True, True, True]

                reels_for_frame = [
                    final_reels[idx] if locked[idx] else self._pick_weighted_symbol()
                    for idx in range(3)
                ]
                spin_mask = [not locked[0], not locked[1], not locked[2]]

                is_final_frame = frame == frames_total
                if is_final_frame:
                    reels_for_frame = final_reels
                    spin_mask = [False, False, False]

                embed = self._build_frame_embed(reels_for_frame, spin_mask, result, is_final_frame, status_label)

                try:
                    png = await render_slots_png(
                        reels=reels_for_frame,
                        bet=int(result["bet"]),
                        payout=int(result["payout"]) if is_final_frame else 0,
                        balance=int(result["balance_after"]) if is_final_frame else None,
                        pool_tokens=int(result["pool_after_tokens"]) if is_final_frame else int(pool_before_tokens),
                        pool_millis=int(result["pool_after_millis"]) if is_final_frame else int(pool_before_millis),
                        status_text=status_label if is_final_frame else "SPINNING",
                        spin_mask=spin_mask,
                    )
                    embed.set_image(url="attachment://slots.png")
                    await interaction.edit_original_response(
                        embed=embed,
                        view=self,
                        files=[discord.File(BytesIO(png), filename="slots.png")],
                    )
                except Exception:
                    await interaction.edit_original_response(embed=embed, view=self)

                if frame < frames_total:
                    await asyncio.sleep(delay_ms / 1000)

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
