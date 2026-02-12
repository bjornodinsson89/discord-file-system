from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from repositories.pools_repository import PoolsRepository
from repositories.torn_items import TornItemsRepository
from services.raffle_payment import RafflePaymentService
from utils import GuildSettingsRepository, get_security_manager, get_torn_api
from utils.database import get_database, get_pool
from utils.embeds import create_error_embed
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError

log = logging.getLogger("happy_jumper.pools")
XANAX_FALLBACK_ICON_URL = "https://www.torn.com/images/items/206/large.png"


def _pool_remaining_tickets(pool: dict, sold: int) -> int:
    return max(0, int(pool["tickets_total"]) - int(sold))


def _pool_max_buy_now(pool: dict, sold: int, user_tickets: int) -> int:
    remaining = _pool_remaining_tickets(pool, sold)
    if remaining <= 0:
        return 0
    max_per_user = int(pool.get("max_per_user") or 0)
    if max_per_user <= 0:
        return remaining
    user_remaining = max(0, max_per_user - int(user_tickets))
    return min(remaining, user_remaining)


async def _xanax_thumbnail_url() -> str:
    repo = TornItemsRepository(get_pool())
    meta = await repo.get_item_meta_by_name("xanax")
    image_url = (meta or {}).get("image_url", "")
    return image_url.strip() or XANAX_FALLBACK_ICON_URL


async def _build_pool_panel_embed(pool: dict, sold: int) -> discord.Embed:
    ticket_price = int(pool["ticket_price_xanax"])
    xanax_total = int(sold) * ticket_price
    embed = discord.Embed(
        title="Xanax Pool",
        description="Use the buttons below to buy tickets or check your total.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Price per ticket", value=f"💊 {ticket_price} Xanax", inline=True)
    embed.add_field(name="Tickets available", value=str(max(0, int(pool["tickets_total"]) - int(sold))), inline=True)
    embed.add_field(name="Max per user", value="Unlimited" if int(pool["max_per_user"]) == 0 else str(pool["max_per_user"]), inline=True)
    embed.add_field(name="Pool Total", value=f"{xanax_total} Xanax in the pool", inline=False)
    embed.set_thumbnail(url=await _xanax_thumbnail_url())
    return embed


async def _refresh_pool_panel_message(bot: commands.Bot, pool_id: int) -> None:
    repo = PoolsRepository(get_pool())
    pool = await repo.get_pool(pool_id)
    if not pool:
        return
    channel_id = pool.get("panel_channel_id")
    message_id = pool.get("panel_message_id")
    if not channel_id or not message_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception:
            return
    try:
        message = await channel.fetch_message(int(message_id))
    except Exception:
        return

    sold = await repo.get_total_tickets(pool_id)
    embed = await _build_pool_panel_embed(pool, sold)
    await message.edit(embed=embed, view=PoolPurchasePanelView(pool_id=pool_id, disabled=(pool.get("status") != "active")))


class PoolCustomQuantityModal(discord.ui.Modal):
    quantity = discord.ui.TextInput(label="Custom Ticket Quantity", placeholder="Enter an integer", required=True, max_length=10)

    def __init__(self, pool_id: int, max_buy: int):
        super().__init__(title="💊 Buy Pool Tickets")
        self.pool_id = pool_id
        self.max_buy = max_buy

    async def on_submit(self, interaction: discord.Interaction):
        try:
            quantity = int(self.quantity.value)
        except (TypeError, ValueError):
            await interaction.response.send_message("❌ Invalid quantity", ephemeral=True)
            return
        if quantity < 1:
            await interaction.response.send_message("❌ Invalid quantity", ephemeral=True)
            return
        if quantity > self.max_buy:
            quantity = self.max_buy
        await _start_pool_purchase(interaction, self.pool_id, quantity)


class PoolQuantitySelect(discord.ui.Select):
    def __init__(self, pool_id: int, max_buy: int):
        options: list[discord.SelectOption]
        if max_buy <= 25:
            options = [discord.SelectOption(label=str(i), value=str(i)) for i in range(1, max_buy + 1)]
        else:
            base = [1, 2, 3, 5, 10, 15, 20, 25]
            options = [discord.SelectOption(label=str(i), value=str(i)) for i in base if i <= max_buy]
            options.append(discord.SelectOption(label="Custom", value="custom"))
        super().__init__(placeholder="Choose ticket quantity", options=options, min_values=1, max_values=1)
        self.pool_id = pool_id
        self.max_buy = max_buy

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "custom":
            await interaction.response.send_modal(PoolCustomQuantityModal(self.pool_id, self.max_buy))
            return
        try:
            quantity = int(selected)
        except (TypeError, ValueError):
            await interaction.response.send_message("❌ Invalid quantity", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _start_pool_purchase(interaction, self.pool_id, quantity, use_followup=True)


class PoolQuantityPickerView(discord.ui.View):
    def __init__(self, pool_id: int, max_buy: int):
        super().__init__(timeout=120)
        self.add_item(PoolQuantitySelect(pool_id, max_buy))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(content="Purchase cancelled.", view=None)


class PoolVerifyPaymentView(discord.ui.View):
    def __init__(self, bot: commands.Bot, pool_id: int, quantity: int, owner_discord_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.pool_id = pool_id
        self.quantity = quantity
        self.owner_discord_id = owner_discord_id

    @discord.ui.button(label="✅ Verify Payment", style=discord.ButtonStyle.green)
    async def verify_payment(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.owner_discord_id:
            await interaction.response.send_message("❌ This verification prompt belongs to another user.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        repo = PoolsRepository(get_pool())
        pool = await repo.get_pool(self.pool_id)
        if not pool or pool.get("status") != "active":
            await interaction.followup.send("❌ This pool is no longer active.", ephemeral=True)
            return

        sold = await repo.get_total_tickets(self.pool_id)
        user_tickets = await repo.get_user_tickets(self.pool_id, interaction.user.id)
        max_buy = _pool_max_buy_now(pool, sold, user_tickets)
        if max_buy <= 0:
            await interaction.followup.send("❌ No tickets available.", ephemeral=True)
            return

        quantity = min(int(self.quantity), max_buy)
        total_cost = quantity * int(pool["ticket_price_xanax"])

        db = get_database()
        buyer_key = await db.get_user_api_key(interaction.user.id)
        if not buyer_key or not buyer_key.get("encrypted_key") or not buyer_key.get("torn_user_id"):
            await interaction.followup.send("❌ You must link your Torn API key first.", ephemeral=True)
            return

        creator_key = await db.get_user_api_key(int(pool["created_by_discord_id"]))
        creator_torn_id = int((creator_key or {}).get("torn_user_id") or 0)
        if not creator_torn_id:
            await interaction.followup.send("❌ Pool creator Torn ID is not configured.", ephemeral=True)
            return

        buyer_torn_id = int(buyer_key.get("torn_user_id") or 0)
        if buyer_torn_id <= 0:
            await interaction.followup.send("❌ Your linked Torn ID is invalid.", ephemeral=True)
            return

        try:
            api_key = get_security_manager().decrypt(buyer_key["encrypted_key"])
            logs = await get_torn_api().get_user_logs(api_key, limit=10)
        except TornAPIRateLimitError:
            await interaction.followup.send("❌ Torn API is rate-limited. Try again in a moment.", ephemeral=True)
            return
        except TornAPIPermissionError:
            await interaction.followup.send("❌ Your Torn key is missing item-log permissions (cat=85).", ephemeral=True)
            return
        except TornAPIError:
            await interaction.followup.send("❌ Torn verification is unavailable right now.", ephemeral=True)
            return
        except Exception:
            log.exception("Unexpected pool verification error pool_id=%s user_id=%s", self.pool_id, interaction.user.id)
            await interaction.followup.send("❌ Verification failed unexpectedly.", ephemeral=True)
            return

        verifier = RafflePaymentService(db)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        since_ts = int((datetime.now(timezone.utc) - timedelta(minutes=15)).timestamp())
        match = verifier._find_matching_payment(
            logs=logs,
            sender_torn_id=buyer_torn_id,
            creator_torn_id=creator_torn_id,
            required_item_id=206,
            required_qty=total_cost,
            since_ts=since_ts,
            until_ts=now_ts,
        )
        if not match:
            await interaction.followup.send(
                f"❌ Payment not found. Send **💊 {total_cost} Xanax** to the pool creator, then press Verify again.",
                ephemeral=True,
            )
            return

        await repo.add_entry(self.pool_id, interaction.user.id, quantity)
        self.stop()
        await _refresh_pool_panel_message(self.bot, self.pool_id)
        await interaction.followup.send(f"✅ Purchase verified. Added **{quantity}** ticket(s).", ephemeral=True)


async def _start_pool_purchase(
    interaction: discord.Interaction,
    pool_id: int,
    quantity: int,
    use_followup: bool = False,
) -> None:
    async def _send(**kwargs):
        if use_followup:
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    repo = PoolsRepository(get_pool())
    pool = await repo.get_pool(pool_id)
    if not pool or pool.get("status") != "active":
        await _send(content="❌ This pool is no longer active.", ephemeral=True)
        return

    sold = await repo.get_total_tickets(pool_id)
    user_tickets = await repo.get_user_tickets(pool_id, interaction.user.id)
    max_buy = _pool_max_buy_now(pool, sold, user_tickets)
    if max_buy <= 0:
        await _send(content="❌ No tickets available.", ephemeral=True)
        return
    if quantity > max_buy:
        quantity = max_buy

    total_cost = quantity * int(pool["ticket_price_xanax"])
    embed = discord.Embed(
        title="💊 Pool Tickets Reserved",
        description=(
            f"🎟️ **Tickets:** {quantity}\n"
            f"💰 **Total:** 💊 {total_cost} Xanax\n\n"
            "Send the Xanax to the pool creator in Torn, then click **Verify Payment**."
        ),
        color=discord.Color.blue(),
    )
    view = PoolVerifyPaymentView(interaction.client, pool_id, quantity, interaction.user.id)
    await _send(embed=embed, view=view, ephemeral=True)


class PoolPurchasePanelView(discord.ui.View):
    def __init__(self, pool_id: int, disabled: bool = False):
        super().__init__(timeout=None)
        self.pool_id = pool_id

        buy_btn = discord.ui.Button(
            label="🎟️ Buy Tickets",
            style=discord.ButtonStyle.success,
            custom_id=f"pool:buy:{pool_id}",
            disabled=disabled,
        )
        my_btn = discord.ui.Button(
            label="ℹ️ My Tickets",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pool:my:{pool_id}",
            disabled=disabled,
        )
        buy_btn.callback = self.buy_tickets
        my_btn.callback = self.my_tickets
        self.add_item(buy_btn)
        self.add_item(my_btn)

    async def buy_tickets(self, interaction: discord.Interaction):
        repo = PoolsRepository(get_pool())
        pool = await repo.get_pool(self.pool_id)
        if not pool:
            await interaction.response.send_message("❌ Pool not found", ephemeral=True)
            return
        if pool.get("status") != "active":
            await interaction.response.send_message("❌ This pool is no longer active.", ephemeral=True)
            return

        sold = await repo.get_total_tickets(self.pool_id)
        user_tickets = await repo.get_user_tickets(self.pool_id, interaction.user.id)
        max_buy = _pool_max_buy_now(pool, sold, user_tickets)
        if max_buy <= 0:
            await interaction.response.send_message("❌ No tickets available.", ephemeral=True)
            return

        if max_buy == 1:
            await _start_pool_purchase(interaction, self.pool_id, 1)
            return

        await interaction.response.send_message(
            content=f"Choose quantity (1-{max_buy})",
            ephemeral=True,
            view=PoolQuantityPickerView(self.pool_id, max_buy),
        )

    async def my_tickets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        repo = PoolsRepository(get_pool())
        mine = await repo.get_user_tickets(self.pool_id, interaction.user.id)
        if mine <= 0:
            await interaction.followup.send("ℹ️ You have no tickets in this pool yet.", ephemeral=True)
            return
        await interaction.followup.send(f"🎟️ You currently have **{mine}** ticket(s) in this pool.", ephemeral=True)


class PoolsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        repo = PoolsRepository(get_pool())
        try:
            pools = await repo.get_active_pools_with_panels()
        except Exception:
            return
        for pool in pools:
            self.bot.add_view(PoolPurchasePanelView(pool_id=int(pool["id"])))

    @app_commands.command(name="pool", description="Start a Xanax Pool (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def pool(self, interaction: discord.Interaction, ticket_price: int, tickets_total: int, max_per_user: int):
        if ticket_price < 1 or tickets_total < 1 or max_per_user < 0:
            await interaction.response.send_message("❌ Invalid values. ticket_price/tickets_total must be >=1, max_per_user >=0.", ephemeral=True)
            return

        repo = PoolsRepository(get_pool())
        active = await repo.get_active_pool(interaction.guild_id)
        if active:
            await interaction.response.send_message("❌ There is already an active pool in this server.", ephemeral=True)
            return

        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
        panel_channel_id = settings.get("raffle_purchase_channel_id") or settings.get("raffle_channel_id")
        announce_channel_id = settings.get("raffle_announcement_channel_id")
        if not panel_channel_id or not announce_channel_id:
            await interaction.response.send_message(
                "❌ Configure **raffle purchase panel channel** and **raffle announcement channel** in `/setup` first.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        panel_channel = guild.get_channel(int(panel_channel_id)) if guild else None
        announce_channel = guild.get_channel(int(announce_channel_id)) if guild else None
        if panel_channel is None or announce_channel is None:
            await interaction.response.send_message("❌ One or more configured channels are invalid or inaccessible.", ephemeral=True)
            return

        pool_id = await repo.create_pool(
            guild_id=interaction.guild_id,
            created_by_discord_id=interaction.user.id,
            ticket_price_xanax=ticket_price,
            tickets_total=tickets_total,
            max_per_user=max_per_user,
            announce_channel_id=int(announce_channel_id),
            panel_channel_id=int(panel_channel_id),
        )
        pool = await repo.get_pool(pool_id)
        panel_embed = await _build_pool_panel_embed(pool, sold=0)
        panel_msg = await panel_channel.send(embed=panel_embed, view=PoolPurchasePanelView(pool_id=pool_id))
        await repo.set_panel_ref(pool_id, panel_channel.id, panel_msg.id)

        if bool(settings.get("raffle_announce_enabled", True)):
            announce_embed = discord.Embed(
                title="💊 Xanax Pool Started",
                description=(
                    f"Price per ticket: **💊 {ticket_price} Xanax**\n"
                    f"Tickets available: **{tickets_total}**\n"
                    f"Max per user: **{'Unlimited' if max_per_user == 0 else max_per_user}**"
                ),
                color=discord.Color.green(),
            )
            announce_embed.add_field(name="", value=f"👉 Buy in {panel_channel.mention}", inline=False)
            await announce_channel.send(embed=announce_embed)

        await interaction.response.send_message(
            f"✅ Xanax Pool created in {panel_channel.mention}.\n{panel_msg.jump_url}",
            ephemeral=True,
        )

    @app_commands.command(name="end_pool", description="End active Xanax Pool and draw winner (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def end_pool(self, interaction: discord.Interaction):
        repo = PoolsRepository(get_pool())
        pool = await repo.get_active_pool(interaction.guild_id)
        if not pool:
            await interaction.response.send_message("❌ No active pool found.", ephemeral=True)
            return

        await repo.end_pool(int(pool["id"]))

        channel_id = pool.get("panel_channel_id")
        message_id = pool.get("panel_message_id")
        if channel_id and message_id:
            channel = interaction.guild.get_channel(int(channel_id)) if interaction.guild else None
            if channel is None and interaction.guild:
                try:
                    channel = await interaction.guild.fetch_channel(int(channel_id))
                except Exception:
                    channel = None
            if channel is not None:
                try:
                    message = await channel.fetch_message(int(message_id))
                    sold = await repo.get_total_tickets(int(pool["id"]))
                    ended_pool = await repo.get_pool(int(pool["id"]))
                    embed = await _build_pool_panel_embed(ended_pool, sold)
                    await message.edit(embed=embed, view=PoolPurchasePanelView(pool_id=int(pool["id"]), disabled=True))
                except Exception:
                    log.warning("Failed disabling pool panel guild=%s pool_id=%s", interaction.guild_id, pool["id"])

        entries = await repo.list_entries(int(pool["id"]))
        winner_id = None
        total_tickets = 0
        if entries:
            total_tickets = sum(int(e.get("tickets", 0)) for e in entries)
            if total_tickets > 0:
                pick = random.randint(1, total_tickets)
                cursor = 0
                for entry in entries:
                    cursor += int(entry.get("tickets", 0))
                    if pick <= cursor:
                        winner_id = int(entry["user_discord_id"])
                        break

        announce_channel = None
        announce_channel_id = pool.get("announce_channel_id")
        if announce_channel_id and interaction.guild:
            announce_channel = interaction.guild.get_channel(int(announce_channel_id))
            if announce_channel is None:
                try:
                    announce_channel = await interaction.guild.fetch_channel(int(announce_channel_id))
                except Exception:
                    announce_channel = None

        if announce_channel is not None:
            if winner_id:
                embed = discord.Embed(
                    title="💊 Xanax Pool Ended",
                    description=f"🏆 Winner: <@{winner_id}>\n🎟️ Total tickets: **{total_tickets}**",
                    color=discord.Color.gold(),
                )
            else:
                embed = create_error_embed("Xanax Pool Ended", "No valid entries were found, so no winner was drawn.")
            await announce_channel.send(embed=embed)

        await interaction.response.send_message("✅ Active pool ended.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PoolsCog(bot))
