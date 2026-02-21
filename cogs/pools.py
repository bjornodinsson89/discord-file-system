from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

import config
import discord
from discord import app_commands
from discord.ext import commands

from repositories.pools_repository import PoolsRepository
from repositories.users import UsersRepository
from repositories.torn_items import TornItemsRepository
from services.raffle_payment import RafflePaymentService
from services.payment_receipts import PaymentReceiptService
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


async def _resolve_torn_identity(discord_id: int) -> tuple[str, int, bool]:
    try:
        safe_discord_id = int(discord_id or 0)
    except (TypeError, ValueError):
        safe_discord_id = 0

    row = None
    if safe_discord_id > 0:
        row = await UsersRepository(get_pool()).get_user_api_key(safe_discord_id)

    torn_id = int((row or {}).get("torn_user_id") or 0)
    torn_name = str((row or {}).get("torn_name") or "").strip()
    if not torn_name:
        torn_name = "User"
    return torn_name, torn_id, bool(torn_id)


async def _build_pool_panel_embed(pool: dict, sold: int) -> discord.Embed:
    ticket_price = int(pool["ticket_price_xanax"])
    tickets_total = int(pool["tickets_total"])
    current_pool_total = int(sold) * ticket_price
    max_pool_total = ticket_price * tickets_total
    embed = discord.Embed(
        title="Xanax Pool",
        description="Use the buttons below to buy tickets or check your total.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Price per ticket", value=f"💊 {ticket_price} Xanax", inline=True)
    embed.add_field(name="Tickets available", value=str(max(0, int(pool["tickets_total"]) - int(sold))), inline=True)
    embed.add_field(name="Max per user", value="Unlimited" if int(pool["max_per_user"]) == 0 else str(pool["max_per_user"]), inline=True)
    embed.add_field(name="Current Pool Total", value=f"💊 {current_pool_total} Xanax", inline=True)
    embed.add_field(name="Max Pool Total", value=f"💊 {max_pool_total} Xanax ({tickets_total} tickets)", inline=True)
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


def _pool_channel_missing_permissions(channel: discord.abc.GuildChannel, me: discord.Member) -> list[str]:
    perms = channel.permissions_for(me)
    missing: list[str] = []
    if not perms.view_channel:
        missing.append("View Channel")
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.embed_links:
        missing.append("Embed Links")
    return missing


async def _resolve_pool_purchase_channel(
    interaction: discord.Interaction,
    settings: dict,
) -> tuple[discord.abc.Messageable | None, str | None]:
    fallback_channel = interaction.channel
    guild = interaction.guild
    if not guild:
        return fallback_channel, None

    me = guild.me or guild.get_member(interaction.client.user.id)
    configured_id = settings.get("pool_channel_id")
    if not configured_id:
        return None, "❌ Configure **Pools PURCHASE channel** in `/setup` first."

    target = guild.get_channel(int(configured_id))
    if target is None:
        try:
            target = await guild.fetch_channel(int(configured_id))
        except Exception:
            target = None

    if target is None:
        return None, (
            "❌ Configured **Pools PURCHASE channel** is invalid or no longer exists. Update `/setup`."
        )

    if me and isinstance(target, discord.abc.GuildChannel):
        missing = _pool_channel_missing_permissions(target, me)
        if missing:
            return None, (
                f"❌ I cannot post purchase panel in {target.mention} (missing: {', '.join(missing)})."
            )

    return target, None


class PoolCustomQuantityModal(discord.ui.Modal):
    quantity = discord.ui.TextInput(label="Ticket quantity", placeholder="3", required=True, max_length=10)

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
        users_repo = UsersRepository(db.pool)
        buyer_key = await users_repo.get_user_api_key(interaction.user.id)
        if not buyer_key or not buyer_key.get("encrypted_key") or not buyer_key.get("torn_user_id"):
            await interaction.followup.send("❌ You must link your Torn API key first.", ephemeral=True)
            return

        creator_key = await users_repo.get_user_api_key(int(pool["created_by_discord_id"]))
        creator_torn_id = int((creator_key or {}).get("torn_user_id") or 0)
        creator_name = str((creator_key or {}).get("torn_name") or "").strip() or "User"
        if not creator_torn_id:
            await interaction.followup.send("❌ Pool creator Torn ID is not configured.", ephemeral=True)
            return

        buyer_torn_id = int(buyer_key.get("torn_user_id") or 0)
        if buyer_torn_id <= 0:
            await interaction.followup.send("❌ Your linked Torn ID is invalid.", ephemeral=True)
            return

        try:
            api_key = get_security_manager().decrypt(buyer_key["encrypted_key"])
            logs = await get_torn_api().get_item_send_receive_logs(
                api_key, limit=config.PAYMENT_VERIFICATION_LOG_LIMIT
            )
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
                f"❌ Payment not found. Send **💊 {total_cost} Xanax** to {creator_name} [{creator_torn_id}] in Torn, then press Verify again.",
                ephemeral=True,
            )
            return

        await repo.add_entry(self.pool_id, interaction.user.id, quantity)
        receipts = PaymentReceiptService(db.pool)
        receipt_id = await receipts.create_and_verify(
            featureType="pool",
            featureRefId=self.pool_id,
            payer_discord_id=interaction.user.id,
            payer_torn_id=buyer_torn_id,
            payee_discord_id=int(pool.get("created_by_discord_id") or 0) or None,
            payee_torn_id=creator_torn_id,
            amount=total_cost,
            currency_type="xanax",
            metadata=match,
            verifier_discord_id=interaction.user.id,
            verifier_torn_id=buyer_torn_id,
        )
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

    ticket_price = int(pool["ticket_price_xanax"])
    tickets_total = int(pool["tickets_total"])
    max_pool_total = ticket_price * tickets_total
    total_cost = quantity * ticket_price
    try:
        creator_discord_id = int(pool.get("created_by_discord_id") or 0)
    except (TypeError, ValueError):
        creator_discord_id = 0
    creator_name, creator_torn_id, has_torn_id = await _resolve_torn_identity(creator_discord_id)
    if not has_torn_id:
        payment_line = "Send 💊 {0} Xanax to User in Torn (Torn ID not configured), then click **Verify Payment**.".format(total_cost)
    else:
        payment_line = (
            f"Send 💊 {total_cost} Xanax to {creator_name} [{creator_torn_id}] in Torn, then click **Verify Payment**."
        )

    embed = discord.Embed(
        title="💊 Pool Tickets Reserved",
        description=(
            f"🎟️ **Tickets:** {quantity}\n"
            f"💰 **Total:** 💊 {total_cost} Xanax\n"
            f"🏦 **Max Pool Total:** 💊 {max_pool_total} Xanax ({tickets_total} tickets)\n\n"
            + payment_line
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
        return

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
        purchase_channel, purchase_channel_error = await _resolve_pool_purchase_channel(interaction, settings)
        if purchase_channel is None or not hasattr(purchase_channel, "send"):
            await interaction.response.send_message(purchase_channel_error or "❌ Unable to resolve Pools PURCHASE channel.", ephemeral=True)
            return

        announce_channel_id = settings.get("pools_post_channel_id") or settings.get("pool_channel_id")
        guild = interaction.guild
        announce_channel = guild.get_channel(int(announce_channel_id)) if (guild and announce_channel_id) else None
        if announce_channel is None and guild and announce_channel_id:
            try:
                announce_channel = await guild.fetch_channel(int(announce_channel_id))
            except Exception:
                announce_channel = None

        pool_id = await repo.create_pool(
            guild_id=interaction.guild_id,
            created_by_discord_id=interaction.user.id,
            ticket_price_xanax=ticket_price,
            tickets_total=tickets_total,
            max_per_user=max_per_user,
            announce_channel_id=int(announce_channel_id),
            panel_channel_id=int(purchase_channel.id),
        )
        pool = await repo.get_pool(pool_id)
        panel_embed = await _build_pool_panel_embed(pool, sold=0)
        panel_msg = await purchase_channel.send(embed=panel_embed, view=PoolPurchasePanelView(pool_id=pool_id))
        await repo.set_panel_ref(pool_id, purchase_channel.id, panel_msg.id)

        if bool(settings.get("raffle_announce_enabled", True)):
            max_pool_total = int(ticket_price) * int(tickets_total)
            announce_embed = discord.Embed(
                title="💊 Xanax Pool Started",
                description=(
                    f"Price per ticket: **💊 {ticket_price} Xanax**\n"
                    f"Tickets available: **{tickets_total}**\n"
                    f"Max per user: **{'Unlimited' if max_per_user == 0 else max_per_user}**\n"
                    f"Max Pool Total: **💊 {max_pool_total} Xanax ({tickets_total} tickets)**"
                ),
                color=discord.Color.green(),
            )
            announce_embed.add_field(name="", value=f"👉 Purchase tickets in <#{int(purchase_channel.id)}>", inline=False)
            if announce_channel is not None and hasattr(announce_channel, "send"):
                await announce_channel.send(embed=announce_embed)

        result_message = f"✅ Xanax Pool purchase panel created in {purchase_channel.mention}.\n{panel_msg.jump_url}"
        if purchase_channel_error:
            result_message = f"{result_message}\n\n{purchase_channel_error}"
        await interaction.response.send_message(result_message, ephemeral=True)


    @app_commands.command(name="pool_list", description="List active Xanax Pools")
    async def pool_list(self, interaction: discord.Interaction):
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        repo = PoolsRepository(get_pool())
        pools = await repo.list_active_pools(interaction.guild_id)

        if not pools:
            embed = discord.Embed(
                title="No active pools",
                description="There are currently no active Xanax pools.",
                color=discord.Color.blurple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(title="Active Xanax Pools", color=discord.Color.blurple())
        for pool in pools:
            pool_id = int(pool["id"])
            sold = await repo.get_total_tickets(pool_id)
            tickets_total = int(pool["tickets_total"])
            ticket_price_xanax = int(pool["ticket_price_xanax"])
            remaining = max(0, tickets_total - sold)
            total_xanax = sold * ticket_price_xanax
            max_per_user = int(pool.get("max_per_user") or 0)
            max_per_user_display = "Unlimited" if max_per_user == 0 else str(max_per_user)

            panel_channel_id = pool.get("panel_channel_id")
            panel_message_id = pool.get("panel_message_id")
            if panel_channel_id and panel_message_id:
                panel_url = (
                    f"https://discord.com/channels/{interaction.guild_id}/{int(panel_channel_id)}/{int(panel_message_id)}"
                )
                panel_display = f"<#{int(panel_channel_id)}> ({panel_url})"
            else:
                panel_display = "Not posted"

            embed.add_field(
                name=f"Pool #{pool_id}",
                value=(
                    f"Price: 💊 {ticket_price_xanax} Xanax\n"
                    f"Sold/Total: {sold}/{tickets_total} (Remaining: {remaining})\n"
                    f"Pool Total: {total_xanax} Xanax\n"
                    f"Max per user: {max_per_user_display}\n"
                    f"Panel: {panel_display}"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

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


async def register_persistent_pool_views(bot: commands.Bot) -> None:
    try:
        repo = PoolsRepository(get_pool())
        pools = await repo.get_active_pools_with_panels()
        for pool in pools:
            bot.add_view(PoolPurchasePanelView(pool_id=int(pool["id"])))
    except Exception:
        log.exception("Failed to register persistent pool views")


async def setup(bot: commands.Bot):
    await bot.add_cog(PoolsCog(bot))
