from __future__ import annotations

import asyncio
import logging
import secrets
from io import BytesIO

import discord

import config
from repositories.slot_assets import get_slot_asset_url, normalize_combo, upsert_slot_asset
from services.casino_games.slots import CasinoSlotsService, SlotsCooldownError, SlotsError
from utils.jump_slots_gif import (
    REEL_CYCLE,
    SPIN_DURATION_MS,
    SPIN_FRAMES,
    render_idle_png,
    render_slots_gif,
)

log = logging.getLogger("happy_jumper.casino.slots")


async def _cache_slot_asset_if_enabled(
    interaction: discord.Interaction,
    combo: str,
    gif_bytes: bytes,
) -> str | None:
    if not config.slot_assets_ready():
        return None

    bot = interaction.client
    if bot is None:
        return None

    guild = bot.get_guild(int(config.SLOT_ASSETS_GUILD_ID))
    if guild is None:
        try:
            guild = await bot.fetch_guild(int(config.SLOT_ASSETS_GUILD_ID))
        except Exception:
            log.warning("slot_assets_fetch_guild_failed combo=%s", combo, exc_info=True)
            return None

    channel = guild.get_channel(int(config.SLOT_ASSETS_CHANNEL_ID)) if guild else None
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(config.SLOT_ASSETS_CHANNEL_ID))
        except Exception:
            log.warning("slot_assets_fetch_channel_failed combo=%s", combo, exc_info=True)
            return None

    if not isinstance(channel, discord.TextChannel):
        return None

    perms = channel.permissions_for(channel.guild.me) if channel.guild and channel.guild.me else None
    if perms and not (perms.send_messages and perms.attach_files):
        return None

    try:
        msg = await channel.send(
            content=f"slot:{combo}",
            file=discord.File(BytesIO(gif_bytes), filename="slots.gif"),
        )
    except Exception:
        log.warning("slot_assets_upload_failed combo=%s", combo, exc_info=True)
        return None

    url = msg.attachments[0].url if msg.attachments else None
    if not url:
        return None

    try:
        await upsert_slot_asset(
            combo,
            url,
            msg.id,
            frames=SPIN_FRAMES,
            duration_ms=SPIN_DURATION_MS,
            max_w=0,
            palette_colors=0,
        )
    except Exception:
        log.warning("slot_assets_upsert_failed combo=%s", combo, exc_info=True)

    return url


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
            embed=self.view._status_embed(self.view._jackpot_label(), "R E A D Y", None, "slots.png"),
            view=self.view,
            attachments=[idle_file],
        )
        if getattr(self.view, "message", None) is None:
            try:
                self.view.message = await interaction.original_response()
            except Exception:
                pass


class SlotsPublicBetModal(discord.ui.Modal, title="Change Slots Bet"):
    bet = discord.ui.TextInput(label="Bet (tokens)", required=True, max_length=12)

    def __init__(self, view: "SlotsPublicResultView"):
        super().__init__()
        self.view = view
        self.bet.default = str(view.bet)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_bet = int(str(self.bet.value).strip())
        except Exception:
            await interaction.response.send_message("❌ Bet must be an integer.", ephemeral=True)
            return

        snapshot = await self.view.service.get_balance_and_pool(self.view.guild_id, self.view.player_id)
        config_snapshot = dict(snapshot.get("config") or self.view.config or {})
        min_bet = int(config_snapshot.get("min_bet") or 1)
        max_bet = int(config_snapshot.get("max_bet") or min_bet)

        if new_bet < min_bet or new_bet > max_bet:
            await interaction.response.send_message(
                f"❌ Bet must be between {min_bet} and {max_bet} tokens.",
                ephemeral=True,
            )
            return

        self.view.bet = new_bet
        self.view.config = config_snapshot
        await interaction.response.send_message(
            f"✅ Bet updated to {new_bet}. Use Spin to play.",
            ephemeral=True,
        )
        if self.view.message is not None:
            try:
                await self.view.message.edit(view=self.view)
            except Exception:
                pass


class SlotsPublicResultView(discord.ui.View):
    def __init__(
        self,
        *,
        guild_id: int,
        player_id: int,
        service: CasinoSlotsService,
        config: dict,
        balance_after: int,
        pool_tokens: int,
        bet: int,
        payout: int,
        win_type: str,
        status_label: str,
        round_id: int | None,
        server_seed_hash: str,
        client_seed: str,
        nonce: int,
    ):
        super().__init__(timeout=3600)
        self.guild_id = int(guild_id)
        self.player_id = int(player_id)
        self.service = service
        self.config = dict(config or {})
        self.balance_after = int(balance_after)
        self.pool_tokens = int(pool_tokens)
        self.bet = int(bet)
        self.payout = int(payout)
        self.win_type = str(win_type or "")
        self.status_label = str(status_label or "")
        self.round_id = int(round_id) if round_id is not None else None
        self.server_seed_hash = str(server_seed_hash or "")
        self.client_seed = str(client_seed or "")
        self.nonce = int(nonce)
        self.message: discord.Message | None = None
        self._spinning = False

    def _update_spin_enabled(self) -> None:
        can_spin = int(self.balance_after) >= int(self.bet)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label and child.label.startswith("Spin"):
                child.disabled = not can_spin

    def _result_status_label(
        self,
        *,
        win_type: str,
        payout: int,
        bet: int,
    ) -> str:
        if win_type == "jackpot":
            return "J A C K P O T 💰"
        if win_type == "push":
            return "P U S H 😐"
        if payout <= 0:
            return "L O S E ☹️"
        if win_type == "small":
            return "S M A L L  W I N ✅"
        return "W I N ✅"

    async def _safe_message_edit(self, interaction: discord.Interaction, message: discord.Message, **kwargs):
        try:
            return await message.edit(**kwargs)
        except discord.NotFound:
            try:
                return await interaction.edit_original_response(**kwargs)
            except Exception:
                if interaction.channel is None:
                    raise
                new_msg = await interaction.channel.send(**kwargs)
                self.message = new_msg
                return new_msg

    async def on_timeout(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Spin", style=discord.ButtonStyle.success)
    async def spin(self, interaction: discord.Interaction, _: discord.ui.Button):
        if int(interaction.user.id) != self.player_id:
            await interaction.response.send_message("This isn’t your slot panel.", ephemeral=True)
            return
        if self._spinning:
            return

        self._spinning = True
        await interaction.response.defer()

        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

        source_message = interaction.message or self.message
        if source_message is None:
            self._spinning = False
            return

        try:
            await self._safe_message_edit(interaction, source_message, view=self)

            snapshot = await self.service.get_balance_and_pool(self.guild_id, self.player_id)
            self.balance_after = int(snapshot["balance"])
            self.pool_tokens = int(snapshot.get("pool_tokens") or self.pool_tokens)
            self.config = dict(snapshot.get("config") or self.config)

            if int(self.balance_after) < int(self.bet):
                self.status_label = "❌ Not enough tokens"
                self._update_spin_enabled()
                await self._safe_message_edit(interaction, source_message, view=self)
                return

            result = await self.service.spin(self.guild_id, self.player_id, self.bet)

            self.balance_after = int(result["balance_after"])
            self.pool_tokens = int(result.get("pool_tokens") or self.pool_tokens)
            self.config = dict(result.get("config") or self.config)
            self.payout = int(result["payout"])
            self.bet = int(result["bet"])
            self.win_type = str(result.get("win_type") or "")
            self.round_id = int(result["round_id"]) if result.get("round_id") is not None else None
            self.server_seed_hash = str(result.get("server_seed_hash") or "")
            self.client_seed = str(result.get("client_seed") or "")
            self.nonce = int(result.get("nonce") or 0)

            final_reels = [int(x) for x in (result.get("reels") or [])][:3]
            while len(final_reels) < 3:
                final_reels.append(REEL_CYCLE[len(final_reels) % len(REEL_CYCLE)])

            self.status_label = self._result_status_label(
                win_type=self.win_type,
                payout=self.payout,
                bet=self.bet,
            )

            combo = normalize_combo(final_reels)
            slot_url: str | None = None
            if config.slot_assets_ready():
                slot_url = await get_slot_asset_url(combo)

            result_embed = discord.Embed(
                title="🎰 Slots Result",
                description=(
                    f"Player: <@{self.player_id}>\n"
                    f"Result: **{self.status_label}**\n"
                    f"Bet: `{self.bet}` • Payout: `{self.payout}`\nJackpot (Max Bet): `{int(result.get('jackpot_pool_display_tokens') or result.get('pool_tokens') or self.pool_tokens)}`"
                ),
                color=discord.Color.gold(),
            )

            if slot_url is not None:
                result_embed.set_image(url=slot_url)
                self._update_spin_enabled()
                await self._safe_message_edit(interaction, source_message, embed=result_embed, attachments=[], view=self)
            else:
                if config.slot_assets_ready():
                    log.warning("slot_assets_cache_miss combo=%s", combo)
                gif_bytes = await asyncio.to_thread(
                    lambda: render_slots_gif(
                        final_reels,
                        frames=SPIN_FRAMES,
                        duration_ms=SPIN_DURATION_MS,
                        balance=self.balance_after,
                        bet=self.bet,
                        jackpot_pool=int(self.pool_tokens),
                    )
                )
                cached_url = await _cache_slot_asset_if_enabled(interaction, combo, gif_bytes)
                if cached_url:
                    result_embed.set_image(url=cached_url)
                    self._update_spin_enabled()
                    await self._safe_message_edit(interaction, source_message, embed=result_embed, attachments=[], view=self)
                else:
                    gif_file = discord.File(BytesIO(gif_bytes), filename="slots.gif")
                    result_embed.set_image(url="attachment://slots.gif")
                    self._update_spin_enabled()
                    await self._safe_message_edit(interaction, source_message, embed=result_embed, attachments=[gif_file], view=self)

            self.message = source_message
            await self.service.post_big_win_announce(interaction, result)
        except SlotsCooldownError as exc:
            self.status_label = f"⏳ Wait {exc.remaining_seconds}s"
            self._update_spin_enabled()
            await self._safe_message_edit(interaction, source_message, view=self)
        except SlotsError as exc:
            self.status_label = f"❌ {exc}"
            self._update_spin_enabled()
            await self._safe_message_edit(interaction, source_message, view=self)
        except Exception:
            log.exception("slots.public_result_spin_failed")
            self.status_label = "❌ Slots error. Check logs."
            self._update_spin_enabled()
            await self._safe_message_edit(interaction, source_message, view=self)
        finally:
            self._spinning = False

    @discord.ui.button(label="Change Bet", style=discord.ButtonStyle.secondary)
    async def change_bet(self, interaction: discord.Interaction, _: discord.ui.Button):
        if int(interaction.user.id) != self.player_id:
            await interaction.response.send_message("This isn’t your slot panel.", ephemeral=True)
            return
        await interaction.response.send_modal(SlotsPublicBetModal(self))

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.secondary)
    async def verify(self, interaction: discord.Interaction, _: discord.ui.Button):
        round_id_display = str(self.round_id) if self.round_id is not None else "N/A"
        details = [
            f"Server Seed Hash: `{self.server_seed_hash}`",
            f"Client Seed: `{self.client_seed}`",
            f"Nonce: `{self.nonce}`",
            f"Round ID: `{round_id_display}`",
            "After the seed rotates, admins can reveal the previous server seed in Back of House. "
            "Use previous server seed + client seed + nonce to reproduce the SHA256 sequence.",
        ]
        await interaction.response.send_message("\n".join(details), ephemeral=True)


class SlotsPlayView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        discord_id: int,
        service: CasinoSlotsService,
        config: dict,
        balance: int,
        pool_tokens: int,
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
        self.house_discord_id = int(house_discord_id or 0)
        self.house_torn_id = int(house_torn_id or 0)
        self.payout_proof_channel_id = int(payout_proof_channel_id or 0)
        self.message: discord.Message | None = None
        self._spinning = False
        self._update_spin_enabled()

    async def _safe_message_edit(self, interaction: discord.Interaction, message: discord.Message, **kwargs):
        try:
            return await message.edit(**kwargs)
        except discord.NotFound:
            try:
                return await interaction.edit_original_response(**kwargs)
            except Exception:
                if interaction.channel is None:
                    raise
                new_msg = await interaction.channel.send(**kwargs)
                self.message = new_msg
                return new_msg

    async def _send_public_spin_result(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
        view: discord.ui.View | None = None,
    ):
        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await interaction.followup.send("I can’t post the slot result here.", ephemeral=True)
            return None
        try:
            return await channel.send(content=content, embed=embed, file=file, files=files, view=view)
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

    def _jackpot_label(self) -> str:
        return f"{int(self.pool_tokens)}"

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
            jackpot_pool=int(self.pool_tokens),
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
        desc += f"**Jackpot (Max Bet):** `{jackpot_str}`\n\n**{status}**"
        if payout is not None:
            desc += f"\n**Payout:** `{payout}`"
        em.description = desc
        if image_name:
            em.set_image(url=f"attachment://{image_name}")
        return em

    def _result_status_label(self, *, win_type: str, payout: int, bet: int) -> str:
        if win_type == "jackpot":
            return "J A C K P O T 💰"
        if win_type == "push":
            return "P U S H 😐"
        if payout <= 0:
            return "L O S E ☹️"
        if win_type == "small":
            return "S M A L L  W I N ✅"
        return "W I N ✅"

    def build_embed(self) -> discord.Embed:
        self._update_spin_enabled()
        return self._status_embed(self._jackpot_label(), "R E A D Y", None, "slots.png")

    def build_content(self) -> str:
        return ""

    async def refresh_state(self) -> None:
        snapshot = await self.service.get_balance_and_pool(self.guild_id, self.discord_id)
        self.balance = int(snapshot["balance"])
        self.pool_tokens = int(snapshot.get("pool_tokens") or self.pool_tokens)
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
        if self._spinning:
            return

        self._spinning = True
        await interaction.response.defer()

        source_message = interaction.message or self.message
        if source_message is None:
            self._spinning = False
            return

        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True

        try:
            await self._safe_message_edit(interaction, source_message, view=self)

            if int(self.balance) < int(self.current_bet):
                raise SlotsError(
                    f"Not enough tokens. Balance is {int(self.balance)}, bet is {int(self.current_bet)}."
                )

            result = await self.service.spin(self.guild_id, self.discord_id, self.current_bet)
            self.balance = int(result["balance_after"])
            self.pool_tokens = int(result.get("pool_tokens") or self.pool_tokens)
            self.config = dict(result.get("config") or self.config)
            self.min_bet = int(self.config.get("min_bet") or self.min_bet)
            self.max_bet = int(self.config.get("max_bet") or self.max_bet)
            self.current_bet = max(self.min_bet, min(self.current_bet, self.max_bet))
            self.house_discord_id = int(result.get("house_discord_id") or self.house_discord_id)
            self.house_torn_id = int(result.get("house_torn_id") or self.house_torn_id)
            self.payout_proof_channel_id = int(
                result.get("payout_proof_channel_id") or self.payout_proof_channel_id
            )

            final_reels = [int(x) for x in (result.get("reels") or [])][:3]
            while len(final_reels) < 3:
                final_reels.append(REEL_CYCLE[len(final_reels) % len(REEL_CYCLE)])

            payout = int(result["payout"])
            bet = int(result["bet"])
            win_type = str(result.get("win_type") or "")
            final_status = self._result_status_label(
                win_type=win_type,
                payout=payout,
                bet=bet,
            )

            result_view = SlotsPublicResultView(
                guild_id=self.guild_id,
                player_id=interaction.user.id,
                service=self.service,
                config=dict(result.get("config") or self.config),
                balance_after=self.balance,
                pool_tokens=int(result.get("pool_tokens") or self.pool_tokens),
                bet=bet,
                payout=payout,
                win_type=win_type,
                status_label=final_status,
                round_id=result.get("round_id"),
                server_seed_hash=str(result.get("server_seed_hash") or ""),
                client_seed=str(result.get("client_seed") or ""),
                nonce=int(result.get("nonce") or 0),
            )

            combo = normalize_combo(final_reels)
            slot_url: str | None = None
            if config.slot_assets_ready():
                slot_url = await get_slot_asset_url(combo)

            result_embed = discord.Embed(
                title="🎰 Slots Result",
                description=(
                    f"Player: {interaction.user.mention}\n"
                    f"Result: **{final_status}**\n"
                    f"Bet: `{bet}` • Payout: `{payout}`\nJackpot (Max Bet): `{int(result.get('jackpot_pool_display_tokens') or result.get('pool_tokens') or self.pool_tokens)}`"
                ),
                color=discord.Color.gold(),
            )

            if slot_url is not None:
                result_embed.set_image(url=slot_url)
                result_view._update_spin_enabled()
                await self._safe_message_edit(interaction, source_message, embed=result_embed, attachments=[], view=result_view)
            else:
                if config.slot_assets_ready():
                    log.warning("slot_assets_cache_miss combo=%s", combo)
                gif_bytes = await asyncio.to_thread(
                    lambda: render_slots_gif(
                        final_reels,
                        frames=SPIN_FRAMES,
                        duration_ms=SPIN_DURATION_MS,
                        balance=self.balance,
                        bet=bet,
                        jackpot_pool=int(self.pool_tokens),
                    )
                )
                cached_url = await _cache_slot_asset_if_enabled(interaction, combo, gif_bytes)
                if cached_url:
                    result_embed.set_image(url=cached_url)
                    result_view._update_spin_enabled()
                    await self._safe_message_edit(interaction, source_message, embed=result_embed, attachments=[], view=result_view)
                else:
                    gif_file = discord.File(BytesIO(gif_bytes), filename="slots.gif")
                    result_embed.set_image(url="attachment://slots.gif")
                    result_view._update_spin_enabled()
                    await self._safe_message_edit(interaction, source_message, embed=result_embed, attachments=[gif_file], view=result_view)

            result_view.message = source_message
            await self.service.post_big_win_announce(interaction, result)
        except SlotsCooldownError as exc:
            idle_file = self._idle_file()
            self._update_spin_enabled()
            await interaction.edit_original_response(
                content="",
                embed=self._status_embed(
                    self._jackpot_label(), f"⏳ Wait {exc.remaining_seconds}s", None, "slots.png"
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
                embed=self._status_embed(self._jackpot_label(), f"❌ {exc}", None, "slots.png"),
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
                    self._jackpot_label(), "❌ Slots error. Check logs.", None, "slots.png"
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
            self._spinning = False

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

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.refresh_state()
        self._update_spin_enabled()
        embed = self._status_embed(self._jackpot_label(), "R E A D Y", None, "slots.png")
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
