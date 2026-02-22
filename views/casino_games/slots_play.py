from __future__ import annotations

import asyncio
import logging
import random

import discord

from services.casino_games.slots import CasinoSlotsService, SlotsCooldownError, SlotsError

log = logging.getLogger("happy_jumper.casino.slots")

SHORT_LABELS = {
    206: "XANAX",
    366: "eDVD",
    197: "E",
    865: "PM",
    394: "BRICK",
    707: "COAL",
    274: "PANDA",
    281: "LION",
}

BOX = (
    "```text\n"
    "┌──────────────────────────┐\n"
    "│        S L O T S         │\n"
    "└──────────────────────────┘\n"
    "```"
)


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

    def _emoji_map(self) -> dict:
        value = self.config.get("emoji_map")
        if isinstance(value, dict):
            normalized = {}
            for k, v in value.items():
                if k is None or not v:
                    continue
                try:
                    normalized[str(int(k))] = str(v)
                except (TypeError, ValueError):
                    continue
            return normalized
        return {}

    def _sym(self, item_id: int) -> str:
        em = self._emoji_map().get(str(int(item_id)))
        if em:
            return str(em)
        return SHORT_LABELS.get(int(item_id), str(item_id))

    def _reel_line(self, reels: list[int]) -> str:
        return f"{self._sym(reels[0])} │ {self._sym(reels[1])} │ {self._sym(reels[2])}"

    def _animation_int(self, key: str, default: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return value

    def cosmetic_pick(self) -> int:
        symbols = self.config.get("symbols") or []
        pairs = []
        for s in symbols:
            try:
                item_id = int(s.get("item_id"))
                weight = int(s.get("weight", 0))
                if weight > 0:
                    pairs.append((item_id, weight))
            except Exception:
                continue
        if not pairs:
            return random.choice([206, 366, 197, 865, 394, 707, 274, 281])
        item_ids = [p[0] for p in pairs]
        weights = [p[1] for p in pairs]
        return random.choices(item_ids, weights=weights, k=1)[0]

    def _frame_embed(
        self,
        reels: list[int],
        status_line: str,
        *,
        final: bool,
        result: dict | None = None,
    ) -> discord.Embed:
        description = f"{BOX}\n{self._reel_line(reels)}\n{status_line}"
        embed = discord.Embed(
            title="🎰 7️⃣7️⃣7️⃣  S L O T S  7️⃣7️⃣7️⃣ 🎰",
            description=description,
            color=discord.Color.purple(),
        )
        if final and result is not None:
            embed.add_field(name="Bet", value=f"**{int(result['bet'])}**", inline=True)
            embed.add_field(name="Payout", value=f"**{int(result['payout'])}**", inline=True)
            embed.add_field(name="Net", value=f"**{int(result['net']):+d}**", inline=True)
            embed.add_field(name="Balance", value=f"**{int(result['balance_after'])}**", inline=True)
            embed.add_field(
                name="Pool After",
                value=f"**{int(result['pool_after_tokens'])}.{int(result['pool_after_millis']):03d}**",
                inline=True,
            )
        return embed

    def build_embed(self) -> discord.Embed:
        reels = [self.cosmetic_pick(), self.cosmetic_pick(), self.cosmetic_pick()]
        embed = self._frame_embed(reels, "Ready to spin.", final=False)
        embed.add_field(name="Balance", value=f"**{self.balance}** tokens", inline=True)
        embed.add_field(name="Current Bet", value=f"**{self.current_bet}** tokens", inline=True)
        embed.add_field(name="Jackpot Pool", value=f"**{self._pool_label()}**", inline=True)
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

            final_reels = [int(v) for v in (result.get("reels") or [])][:3]
            while len(final_reels) < 3:
                final_reels.append(self.cosmetic_pick())

            total_frames = max(5, min(30, self._animation_int("animation_total_frames", 15)))
            delay_ms = self._animation_int("animation_delay_ms", 120)
            delay_ms = max(60, min(delay_ms, 250))

            lock_left = self._animation_int("animation_lock_left", 6)
            lock_mid = self._animation_int("animation_lock_mid", 10)
            lock_right = self._animation_int("animation_lock_right", 14)

            lock_left = max(1, min(lock_left, total_frames - 2))
            lock_mid = max(lock_left + 1, min(lock_mid, total_frames - 1))
            lock_right = max(lock_mid + 1, min(lock_right, total_frames))

            net = int(result["payout"]) - int(result["bet"])
            if int(result["payout"]) <= 0:
                final_status = "L O S E ☹️"
            elif net == 0:
                final_status = "P U S H 😐"
            else:
                final_status = "W I N ✅"

            for i in range(1, total_frames + 1):
                if i < lock_left:
                    locked = [False, False, False]
                elif i < lock_mid:
                    locked = [True, False, False]
                elif i < lock_right:
                    locked = [True, True, False]
                else:
                    locked = [True, True, True]

                reels_for_frame = [
                    final_reels[0] if locked[0] else self.cosmetic_pick(),
                    final_reels[1] if locked[1] else self.cosmetic_pick(),
                    final_reels[2] if locked[2] else self.cosmetic_pick(),
                ]

                is_final = i == total_frames
                if is_final:
                    reels_for_frame = final_reels

                embed = self._frame_embed(
                    reels_for_frame,
                    final_status if is_final else "S P I N N I N G…",
                    final=is_final,
                    result=result if is_final else None,
                )
                await interaction.edit_original_response(embed=embed, view=self)
                await asyncio.sleep(delay_ms / 1000)

            await self.service.post_jackpot_announce(interaction, result)
        except SlotsCooldownError as exc:
            await interaction.edit_original_response(
                embed=discord.Embed(description=f"⏳ Wait {exc.remaining_seconds}s", color=discord.Color.orange()),
                view=self,
            )
        except SlotsError as exc:
            await interaction.edit_original_response(
                embed=discord.Embed(description=f"❌ {exc}", color=discord.Color.red()),
                view=self,
            )
        except Exception:
            log.exception("slots.spin_failed")
            await interaction.edit_original_response(
                embed=discord.Embed(description="❌ Slots error. Check logs.", color=discord.Color.red()),
                view=self,
            )
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
