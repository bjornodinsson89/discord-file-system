from __future__ import annotations

import asyncio
import logging
import secrets
from io import BytesIO

import discord

import config
from repositories.slot_assets import get_slot_asset_url, normalize_combo
from services.casino_games.slots import CasinoSlotsService, SlotsCooldownError, SlotsError
from utils.jump_slots_gif import (
    REEL_CYCLE,
    SPIN_DURATION_MS,
    SPIN_FRAMES,
    render_idle_png,
    render_slots_gif,
)

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
        await interaction.response.defer()
        await self.view.refresh_state()
        self.view._update_spin_enabled()
        idle_file = self.view._idle_file()
        await interaction.edit_original_response(
            content="",
            embed=self.view._status_embed(self.view._pool_label(), "R E A D Y", None, "slots.png"),
            view=self.view,
            attachments=[idle_file],
        )
        if getattr(self.view, "message", None) is None:
            try:
                self.view.message = await interaction.original_response()
            except Exception:
                pass


class SlotsSeedModal(discord.ui.Modal, title="Set Client Seed"):
    seed = discord.ui.TextInput(label="Client Seed", required=True, min_length=6, max_length=64)

    def __init__(self, view: "SlotsPlayView"):
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self.view.service.set_client_seed(
                self.view.guild_id,
                self.view.discord_id,
                str(self.seed.value or "").strip(),
            )
        except SlotsError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        await interaction.response.defer()
        await self.view.refresh_state()
        idle_file = self.view._idle_file()
        self.view._update_spin_enabled()
        await interaction.edit_original_response(
            content="",
            embed=self.view._status_embed(self.view._pool_label(), "R E A D Y", None, "slots.png"),
            view=self.view,
            attachments=[idle_file],
        )


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
        house_discord_id: int = 0,
        house_torn_id: int = 0,
        payout_proof_channel_id: int = 0,
    ):
        super().__init__(timeout=3600)
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
        self.house_discord_id = int(house_discord_id or 0)
        self.house_torn_id = int(house_torn_id or 0)
        self.payout_proof_channel_id = int(payout_proof_channel_id or 0)
        self.message: discord.Message | None = None
        self._update_spin_enabled()

    async def _send_public_spin_result(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
    ):
        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await interaction.followup.send("I can’t post the slot result here.", ephemeral=True)
            return None
        try:
            return await channel.send(content=content, embed=embed, file=file, files=files)
        except discord.Forbidden:
            await interaction.followup.send(
                "I don’t have permission to post results in this channel.", ephemeral=True
            )
            return None
        except discord.HTTPException:
            await interaction.followup.send("Failed to post the slot result. Try again.", ephemeral=True)
            return None


    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != int(self.discord_id):
            if interaction.response.is_done():
                await interaction.followup.send("This isn’t your session.", ephemeral=True)
            else:
                await interaction.response.send_message("This isn’t your session.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        try:
            if getattr(self, "message", None) is not None:
                await self.message.edit(view=self)
        except Exception:
            pass

    def _update_spin_enabled(self) -> None:
        can_spin = int(self.balance) >= int(self.current_bet)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label and child.label.startswith("Spin"):
                child.disabled = not can_spin

    def _pool_label(self) -> str:
        if self.pool_millis > 0:
            return f"{self.pool_tokens}.{self.pool_millis:03d}"
        return str(self.pool_tokens)

    def _roll_preview_reels(self) -> list[int]:
        symbols = list((self.config or {}).get("symbols") or [])
        ids = [int(s.get("item_id")) for s in symbols if s.get("item_id") is not None]
        weights = [
            max(0, int(s.get("weight") or 0)) for s in symbols if s.get("item_id") is not None
        ]
        if not ids or sum(weights) <= 0:
            ids = list(REEL_CYCLE)
            weights = [1 for _ in ids]
        weighted = []
        for item_id, weight in zip(ids, weights):
            weighted.extend([item_id] * max(1, int(weight)))
        if not weighted:
            weighted = list(ids)
        return [secrets.choice(weighted) for _ in range(3)]

    def _idle_file(self) -> discord.File:
        idle_png = render_idle_png(
            self._roll_preview_reels(),
            balance=self.balance,
            bet=self.current_bet,
            jackpot_pool=self.pool_tokens,
        )
        return discord.File(BytesIO(idle_png), filename="slots.png")

    def _status_embed(
        self, jackpot_str: str, status: str, payout: int | None, image_name: str | None
    ) -> discord.Embed:
        em = discord.Embed(title="🎰 7️⃣7️⃣7️⃣  S L O T S  7️⃣7️⃣7️⃣ 🎰")
        if self.house_discord_id and self.house_torn_id:
            house_line = (
                f"**House:** <@{self.house_discord_id}>\n"
                f"**Send payments to:** Torn ID `{self.house_torn_id}`"
            )
        elif self.house_discord_id:
            house_line = (
                f"**House:** <@{self.house_discord_id}>\n"
                "⚠️ **Admins:** House Torn ID is missing. Configure it in /back_of_house"
            )
        else:
            house_line = "**House:** Not set (admins: configure House in /back_of_house)"
        desc = f"{house_line}\n"
        if self.payout_proof_channel_id:
            desc += f"**Proof Channel:** <#{self.payout_proof_channel_id}>\n"
        desc += f"**Jackpot:** `{jackpot_str}`\n\n**{status}**"
        if payout is not None:
            desc += f"\n**Payout:** `{payout}`"
        em.description = desc
        if image_name:
            em.set_image(url=f"attachment://{image_name}")
        return em

    def build_embed(self) -> discord.Embed:
        self._update_spin_enabled()
        return self._status_embed(self._pool_label(), "R E A D Y", None, "slots.png")

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
        self.house_discord_id = int(snapshot.get("house_discord_id") or 0)
        self.house_torn_id = int(snapshot.get("house_torn_id") or 0)
        self.payout_proof_channel_id = int(snapshot.get("payout_proof_channel_id") or 0)
        self._update_spin_enabled()

    @discord.ui.button(label="Set Bet", style=discord.ButtonStyle.secondary)
    async def set_bet(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SlotsBetModal(self))

    @discord.ui.button(label="Spin 🎰", style=discord.ButtonStyle.success)
    async def spin(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        await interaction.response.defer(ephemeral=True)

        try:
            if int(self.balance) < int(self.current_bet):
                raise SlotsError(
                    f"Not enough tokens. Balance is {int(self.balance)}, bet is {int(self.current_bet)}."
                )

            result = await self.service.spin(self.guild_id, self.discord_id, self.current_bet)
            self.balance = int(result["balance_after"])
            self.pool_tokens = int(result["pool_after_tokens"])
            self.pool_millis = int(result.get("pool_after_millis") or 0)
            self.config = dict(result.get("config") or self.config)
            self.min_bet = int(self.config.get("min_bet") or self.min_bet)
            self.max_bet = int(self.config.get("max_bet") or self.max_bet)
            self.current_bet = max(self.min_bet, min(self.current_bet, self.max_bet))
            self.house_discord_id = int(result.get("house_discord_id") or self.house_discord_id)
            self.house_torn_id = int(result.get("house_torn_id") or self.house_torn_id)
            self.payout_proof_channel_id = int(
                result.get("payout_proof_channel_id") or self.payout_proof_channel_id
            )
            self._update_spin_enabled()

            final_reels = [int(x) for x in (result.get("reels") or [])][:3]
            while len(final_reels) < 3:
                final_reels.append(REEL_CYCLE[len(final_reels) % len(REEL_CYCLE)])

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

            combo = normalize_combo(final_reels)
            slot_url: str | None = None
            if config.slot_assets_ready():
                slot_url = await get_slot_asset_url(combo)

            result_embed = discord.Embed(
                title="🎰 Slots Result",
                description=(
                    f"Player: {interaction.user.mention}\n"
                    f"Result: **{final_status}**\n"
                    f"Bet: `{bet}` • Payout: `{payout}`\n\n"
                    f"Server Hash: `{result.get('server_seed_hash', '')}`\n"
                    f"Client Seed: `{result.get('client_seed', '')}`\n"
                    f"Nonce: `{result.get('nonce', 0)}`"
                ),
                color=discord.Color.gold(),
            )

            if slot_url is not None:
                try:
                    result_embed.set_image(url=slot_url)
                    posted_message = await self._send_public_spin_result(interaction, embed=result_embed)
                    if posted_message is not None:
                        await interaction.followup.send("✅ Result posted.", ephemeral=True)

                    idle_file = self._idle_file()
                    self._update_spin_enabled()
                    await interaction.edit_original_response(
                        content="",
                        embed=self._status_embed(self._pool_label(), "R E A D Y", None, "slots.png"),
                        view=self,
                        attachments=[idle_file],
                    )
                    await self.service.post_big_win_announce(interaction, result)
                    return
                except discord.HTTPException:
                    log.warning("slot_assets_url_failed combo=%s", combo, exc_info=True)

            if config.slot_assets_ready() and slot_url is None:
                log.warning("slot_assets_cache_miss combo=%s", combo)

            gif_bytes = await asyncio.to_thread(
                lambda: render_slots_gif(
                    final_reels,
                    frames=SPIN_FRAMES,
                    duration_ms=SPIN_DURATION_MS,
                    balance=self.balance,
                    bet=bet,
                    jackpot_pool=self.pool_tokens,
                )
            )
            gif_file = discord.File(BytesIO(gif_bytes), filename="slots.gif")
            result_embed.set_image(url="attachment://slots.gif")
            posted_message = await self._send_public_spin_result(
                interaction, embed=result_embed, file=gif_file
            )
            if posted_message is not None:
                await interaction.followup.send("✅ Result posted.", ephemeral=True)

            idle_file = self._idle_file()
            self._update_spin_enabled()
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(self._pool_label(), "R E A D Y", None, "slots.png"),
                view=self,
                attachments=[idle_file],
            )
            if getattr(self, "message", None) is None:
                try:
                    self.message = await interaction.original_response()
                except Exception:
                    pass
            await self.service.post_big_win_announce(interaction, result)
        except SlotsCooldownError as exc:
            idle_file = self._idle_file()
            self._update_spin_enabled()
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(
                    self._pool_label(), f"⏳ Wait {exc.remaining_seconds}s", None, "slots.png"
                ),
                view=self,
                attachments=[idle_file],
            )
            if getattr(self, "message", None) is None:
                try:
                    self.message = await interaction.original_response()
                except Exception:
                    pass
        except SlotsError as exc:
            idle_file = self._idle_file()
            self._update_spin_enabled()
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(self._pool_label(), f"❌ {exc}", None, "slots.png"),
                view=self,
                attachments=[idle_file],
            )
            if getattr(self, "message", None) is None:
                try:
                    self.message = await interaction.original_response()
                except Exception:
                    pass
        except Exception:
            log.exception("slots.spin_failed")
            idle_file = self._idle_file()
            self._update_spin_enabled()
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(
                    self._pool_label(), "❌ Slots error. Check logs.", None, "slots.png"
                ),
                view=self,
                attachments=[idle_file],
            )
            if getattr(self, "message", None) is None:
                try:
                    self.message = await interaction.original_response()
                except Exception:
                    pass
        finally:
            self._update_spin_enabled()
            await interaction.edit_original_response(view=self)
            if getattr(self, "message", None) is None:
                try:
                    self.message = await interaction.original_response()
                except Exception:
                    pass


    @discord.ui.button(label="Fairness", style=discord.ButtonStyle.secondary)
    async def fairness(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = await self.service.get_fairness_state(self.guild_id, self.discord_id)
        lines = [
            f"Server Seed Hash: `{state.get('server_seed_hash')}`",
            f"Client Seed: `{state.get('client_seed')}`",
            f"Next Nonce: `{int(state.get('nonce') or 0)}`",
        ]
        if state.get("previous_server_seed"):
            lines.append(f"Previous Server Seed: `{state.get('previous_server_seed')}`")
            lines.append(f"Previous Server Seed Hash: `{state.get('previous_server_seed_hash')}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="Set Seed", style=discord.ButtonStyle.secondary)
    async def set_seed(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SlotsSeedModal(self))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_state()
        self._update_spin_enabled()
        embed = self._status_embed(self._pool_label(), "R E A D Y", None, "slots.png")
        await interaction.edit_original_response(
            content="", embed=embed, view=self, attachments=[self._idle_file()]
        )
        if getattr(self, "message", None) is None:
            try:
                self.message = await interaction.original_response()
            except Exception:
                pass

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        if getattr(self, "message", None) is None:
            try:
                self.message = await interaction.original_response()
            except Exception:
                pass
