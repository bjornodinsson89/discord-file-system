"""
Raffle system with sell-out trigger support and automatic payment verification.
"""
import logging
import re
from datetime import datetime, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks
from repositories.raffles import RafflesRepository
from repositories.torn_items import TornItemsRepository
from repositories.users import UsersRepository
from services.raffle_payment import RafflePaymentService
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool
from utils.embeds import create_error_embed
from utils.icon_strips import build_icon_strip_file
from utils.item_resolver import ItemResolver
log = logging.getLogger("happy_jumper.raffles")
_PACK_WORD_RE = re.compile(r"\bpack\b", re.IGNORECASE)
_CURLY_QUOTES_RE = re.compile(r"[’‘]")
_NON_ALNUM_WS_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")
_MULTI_ITEM_X_PATTERNS = [
    re.compile(r"\b\d+\s*[x×]\s*[a-z]", re.IGNORECASE),
    re.compile(r"\b[a-z][a-z0-9'\- ]*\s*[x×]\s*\d+\b", re.IGNORECASE),
    re.compile(r"\b[x×]\s*\d+\s+[a-z]", re.IGNORECASE),
]
_QTY_PATTERNS = [
    re.compile(r"^\s*(\d+)\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(.+?)\s+[x×](\d+)\s*$", re.IGNORECASE),
    re.compile(r"^\s*[x×](\d+)\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(\d+)[x×]\s+(.+?)\s*$", re.IGNORECASE),
]
def _payment_text(payment_type: str, amount: int) -> str:
    if payment_type == "free":
        return "FREE"
    if payment_type == "xanax":
        return f"💊 {amount} Xanax"
    if payment_type == "erotic_dvd":
        return f"📀 {amount} eDVD"
    return f"{amount} {payment_type}"


def _raffle_remaining_tickets(raffle: dict, entries: list[dict]) -> int:
    reserved_or_paid = sum(int(e.get("num_tickets", 0)) for e in entries)
    return max(0, int(raffle["tickets_available"]) - reserved_or_paid)


def _max_buy_now(raffle: dict, entries: list[dict], user_id: int) -> int:
    raffle_remaining = _raffle_remaining_tickets(raffle, entries)
    if raffle_remaining <= 0:
        return 0
    max_per_user = int(raffle.get("max_tickets_per_user") or 0)
    if max_per_user <= 0:
        return raffle_remaining
    user_current = sum(int(e.get("num_tickets", 0)) for e in entries if int(e.get("discord_id", 0)) == user_id)
    user_remaining = max(0, max_per_user - user_current)
    return min(user_remaining, raffle_remaining)
def _is_supported_icon_payment(payment_type: str) -> bool:
    return payment_type in {"xanax", "erotic_dvd"}


def _normalize_item_name(raw_name: str) -> str:
    value = (raw_name or "").strip().lower()
    value = _CURLY_QUOTES_RE.sub("'", value)
    value = _NON_ALNUM_WS_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).strip()
    return value


def _is_single_item_prize(prize_text: str, is_bundle: bool) -> bool:
    if is_bundle:
        return False
    value = (prize_text or "").strip().lower()
    if not value:
        return False
    if "," in value:
        return False
    return not any(pattern.search(value) for pattern in _MULTI_ITEM_X_PATTERNS)
def _parse_bundle_entry(raw_entry: str) -> tuple[int, str] | None:
    text = (raw_entry or "").strip()
    if not text:
        return None
    for pattern in _QTY_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        left, right = match.group(1), match.group(2)
        if pattern is _QTY_PATTERNS[1]:
            name_raw, qty_raw = left, right
        else:
            qty_raw, name_raw = left, right
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            return None
        if qty < 1:
            return None
        return qty, name_raw.strip()
    return None
class RaffleCreateModal(discord.ui.Modal):
    """Modal for creating a new raffle."""

    prize = discord.ui.TextInput(
        label="🎁 Prize",
        placeholder="What are you giving away?",
        required=True,
        max_length=200,
    )
    payment_type = discord.ui.TextInput(
        label="Payment type",
        placeholder="free",
        required=True,
        max_length=20,
    )
    ticket_price = discord.ui.TextInput(
        label="Ticket price",
        placeholder="1",
        required=True,
        max_length=6,
    )
    tickets_available = discord.ui.TextInput(
        label="Tickets available",
        placeholder="100",
        required=True,
        max_length=10,
    )
    max_per_user = discord.ui.TextInput(
        label="Max per user",
        placeholder="5",
        required=True,
        max_length=3,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__(title="🎉 Create Raffle")
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        try:
            payment_type = str(self.payment_type.value).strip().lower()
            if payment_type not in {"free", "xanax", "erotic_dvd"}:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid payment type", "Payment type must be one of: free, xanax, erotic_dvd."),
                    ephemeral=True,
                )
                return
            price = int(self.ticket_price.value)
            total = int(self.tickets_available.value)
            max_per = int(self.max_per_user.value or 0)
            if payment_type != "free" and price <= 0:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid ticket price", "Ticket Price must be greater than 0 for paid raffles."),
                    ephemeral=True,
                )
                return
            if total < 1:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid total tickets", "Total Tickets must be 1 or greater."),
                    ephemeral=True,
                )
                return
            if max_per < 0:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid max per user", "Max Per User must be 0 or greater."),
                    ephemeral=True,
                )
                return
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid numeric input", "Ticket Price, Total Tickets, and Max Per User must be valid integers."),
                ephemeral=True,
            )
            return
        try:
            end_time = datetime.utcnow() + timedelta(days=30)
            end_trigger = "tickets_sold"
            hours_after_sold_out = None
            users_repo = UsersRepository(get_pool())
            creator_key = await users_repo.get_user_api_key(interaction.user.id)
            if not creator_key or not creator_key.get("torn_user_id"):
                await interaction.response.send_message(
                    embed=create_error_embed("Missing API key", "You must link your Torn API key first to create raffles."),
                    ephemeral=True,
                )
                return
            item_repo = TornItemsRepository(get_pool())
            single_item_meta = await item_repo.get_item_meta_by_name(str(self.prize.value))
            draft = {
                "guild_id": interaction.guild_id,
                "creator_discord_id": interaction.user.id,
                "prize": str(self.prize.value).strip(),
                "ticket_payment_type": payment_type,
                "ticket_price": price,
                "tickets_available": total,
                "max_tickets_per_user": max_per,
                "end_time": end_time,
                "end_trigger": end_trigger,
                "hours_after_sold_out": hours_after_sold_out,
                "single_item_meta": single_item_meta,
            }
            if _PACK_WORD_RE.search(draft["prize"]) and not single_item_meta:
                raffle_cog = self.bot.get_cog("RafflesCog")
                if raffle_cog is None:
                    await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
                    return
                raffle_cog.store_pack_draft(interaction.user.id, draft)
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title="Pack Detected",
                        description="This prize looks like a pack. Define pack contents now or skip and create text-only.",
                        color=discord.Color.blurple(),
                    ),
                    view=RafflePackChoiceView(self.bot, interaction.user.id),
                    ephemeral=True,
                )
                return
            raffle_cog = self.bot.get_cog("RafflesCog")
            if raffle_cog is None:
                await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
                return
            await raffle_cog.create_raffle_from_draft(interaction, draft, is_bundle=False, bundle_text=None, bundle_entries=[])
        except Exception as exc:
            log.exception("raffle create modal submit failed: %s", exc)
            err_embed = create_error_embed("Raffle creation failed", f"{type(exc).__name__}: {exc}")
            if interaction.response.is_done():
                await interaction.followup.send(embed=err_embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=err_embed, ephemeral=True)

class RafflePackChoiceView(discord.ui.View):
    def __init__(self, bot: commands.Bot, creator_discord_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.creator_discord_id = creator_discord_id
    @discord.ui.button(label="Define Pack Contents", style=discord.ButtonStyle.primary)
    async def define_pack_contents(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can define this pack.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None or not raffle_cog.get_pack_draft(interaction.user.id):
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        await interaction.response.send_modal(RafflePackContentsModal(self.bot, self.creator_discord_id))
    @discord.ui.button(label="Skip (text-only)", style=discord.ButtonStyle.secondary)
    async def skip_text_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can continue this draft.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None:
            await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
            return
        draft = raffle_cog.get_pack_draft(interaction.user.id)
        if not draft:
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        raffle_cog.pop_pack_draft(interaction.user.id)
        await raffle_cog.create_raffle_from_draft(interaction, draft, is_bundle=False, bundle_text=None, bundle_entries=[])
class RafflePackContentsModal(discord.ui.Modal):
    contents = discord.ui.TextInput(
        label="Pack Contents",
        placeholder="60 xanax, 5 edvd, 1 ecstasy",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )
    def __init__(self, bot: commands.Bot, creator_discord_id: int):
        super().__init__(title="Define Pack Contents")
        self.bot = bot
        self.creator_discord_id = creator_discord_id
    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can submit this pack.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None:
            await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
            return
        draft = raffle_cog.get_pack_draft(interaction.user.id)
        if not draft:
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        raw_contents = str(self.contents.value)
        parts = [p.strip() for p in re.split(r"[\n,]", raw_contents) if p.strip()]
        parsed: list[tuple[int, str]] = []
        for part in parts:
            parsed_entry = _parse_bundle_entry(part)
            if not parsed_entry:
                await interaction.response.send_message(
                    f"❌ Could not parse entry: `{part}`. Use formats like `60 xanax`, `xanax x60`, `x60 xanax`, `60x xanax`.",
                    ephemeral=True,
                )
                return
            parsed.append(parsed_entry)
        if not parsed:
            await interaction.response.send_message("❌ No valid bundle entries provided.", ephemeral=True)
            return
        repo = TornItemsRepository(get_pool())
        bundle_entries: list[dict] = []
        for qty, raw_name in parsed:
            item_id = await repo.resolve_item_id(raw_name)
            if not item_id:
                await interaction.response.send_message(
                    f"❌ Could not resolve item name `{raw_name}` in Torn item index.",
                    ephemeral=True,
                )
                return
            meta = await repo.get_item_meta(item_id)
            if not meta:
                await interaction.response.send_message(
                    f"❌ Could not load metadata for `{raw_name}`.",
                    ephemeral=True,
                )
                return
            bundle_entries.append({"item_id": item_id, "name": meta["name"], "image_url": meta["image_url"], "quantity": qty})
        raffle_cog.pop_pack_draft(interaction.user.id)
        await raffle_cog.create_raffle_from_draft(
            interaction,
            draft,
            is_bundle=True,
            bundle_text=raw_contents,
            bundle_entries=bundle_entries,
        )
async def _reserve_raffle_tickets(
    interaction: discord.Interaction,
    repo: RafflesRepository,
    raffle_id: int,
    quantity: int,
    *,
    use_followup: bool = False,
):
    async def _send_response(**kwargs):
        if use_followup:
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def _send_error(message: str):
        await _send_response(content=message, ephemeral=True)

    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        await _send_error("❌ Invalid quantity")
        return
    if quantity < 1:
        await _send_error("❌ Invalid quantity")
        return

    # Check raffle
    raffle = await repo.get_raffle(raffle_id)
    if not raffle:
        await _send_error("❌ Raffle not found")
        return
    if raffle["status"] != "active":
        await _send_error("❌ This raffle is no longer active")
        return

    entries = await repo.get_raffle_entries(raffle_id)
    max_buy_now = _max_buy_now(raffle, entries, interaction.user.id)
    if max_buy_now <= 0:
        await _send_error("❌ No tickets available.")
        return
    if quantity > max_buy_now:
        quantity = max_buy_now

    # Handle FREE ENTRY
    if raffle["ticket_payment_type"] == "free":
        # Check raffle
        try:
            entry = await repo.reserve_free_entry(
                    raffle_id=raffle_id,
                    discord_id=interaction.user.id,
                    torn_user_id=0,
                    num_tickets=quantity
                )
            if not entry:
                await _send_error("❌ Failed to enter raffle")
                return
            embed = discord.Embed(
                title="✅ Entry Confirmed!",
                description=f"🎁 **Raffle:** {raffle['prize']}\n"
                           f"🎟️ **Tickets:** {quantity}\n"
                           f"💰 **Price:** 🎫 FREE",
                color=discord.Color.green()
            )
            # Check if sold out
            updated_raffle = await repo.get_raffle(raffle_id)
            if updated_raffle["tickets_fully_sold_at"]:
                embed.add_field(
                    name="🎉 SOLD OUT!",
                    value="This raffle is now full! Drawing soon.",
                    inline=False
                )
            await _send_response(embed=embed, ephemeral=True)
            return
        except Exception as e:
            log.error(f"Failed free entry: {e}")
            await _send_error("❌ Failed to enter raffle")
            return
    # PAID ENTRY
    users_repo = UsersRepository(get_pool())
    buyer_key = await users_repo.get_user_api_key(interaction.user.id)
    if not buyer_key or not buyer_key.get("torn_user_id"):
        await _send_error(
            "❌ You must link your Torn API key first to buy paid raffle tickets."
        )
        return
    creator_torn_id = raffle.get("creator_torn_id")
    if not creator_torn_id:
        creator_key = await users_repo.get_user_api_key(int(raffle["creator_discord_id"]))
        creator_torn_id = creator_key.get("torn_user_id") if creator_key else None
    if not creator_torn_id:
        await _send_error("❌ Raffle creator Torn ID is not configured. Please contact an admin.")
        return
    reserved_until = datetime.utcnow() + timedelta(minutes=5)
    try:
        entry = await repo.reserve_entry(
            raffle_id=raffle_id,
            discord_id=interaction.user.id,
            torn_user_id=int(buyer_key["torn_user_id"]),
            num_tickets=quantity,
            reserved_until=reserved_until
        )
        if not entry:
            await _send_error("❌ Failed to reserve tickets")
            return
        total_cost = quantity * raffle["ticket_price"]
        embed = discord.Embed(
            title="🎫 Tickets Reserved!",
            description=f"🎁 **Raffle:** {raffle['prize']}\n"
                       f"🎟️ **Tickets:** {quantity}\n"
                       f"💰 **Total:** {_payment_text(raffle['ticket_payment_type'], total_cost)}",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="⏰ Payment Deadline",
            value=f"⏱️ Auto-verification at 4:30, expires at 5:00\nSend **{_payment_text(raffle['ticket_payment_type'], total_cost)}** to raffle creator in-game!",
            inline=False
        )
        embed.add_field(
            name="💳 How to Pay",
            value="📨 Send items via Torn, bot will auto-detect. Click '✅ Verify Now' to check early.",
            inline=False
        )
        view = PaymentVerificationView(raffle_id, entry["entry_id"], repo, manual=True)
        await _send_response(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        log.error(f"Failed to reserve entry: {e}")
        await _send_error("❌ Failed to reserve tickets")


class RaffleCustomQuantityModal(discord.ui.Modal):
    quantity = discord.ui.TextInput(label="Ticket quantity", placeholder="3", required=True, max_length=10)

    def __init__(self, raffle_id: int, repo: RafflesRepository, max_buy: int):
        super().__init__(title="🎫 Buy Tickets")
        self.raffle_id = raffle_id
        self.repo = repo
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
        await _reserve_raffle_tickets(interaction, self.repo, self.raffle_id, quantity)


class RaffleQuantityPickerView(discord.ui.View):
    def __init__(self, raffle_id: int, repo: RafflesRepository, max_buy: int):
        super().__init__(timeout=120)
        self.raffle_id = raffle_id
        self.repo = repo
        self.max_buy = max_buy
        self.add_item(RaffleQuantitySelect(raffle_id, repo, max_buy))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Purchase cancelled.", view=None)


class RaffleQuantitySelect(discord.ui.Select):
    def __init__(self, raffle_id: int, repo: RafflesRepository, max_buy: int):
        options: list[discord.SelectOption] = []
        if max_buy <= 25:
            options = [discord.SelectOption(label=str(i), value=str(i)) for i in range(1, max_buy + 1)]
        else:
            base = [1, 2, 3, 5, 10, 15, 20, 25]
            options = [discord.SelectOption(label=str(i), value=str(i)) for i in base if i <= max_buy]
            options.append(discord.SelectOption(label="Custom", value="custom"))
        super().__init__(placeholder="Choose ticket quantity", options=options, min_values=1, max_values=1)
        self.raffle_id = raffle_id
        self.repo = repo
        self.max_buy = max_buy

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        if selected == "custom":
            await interaction.response.send_modal(RaffleCustomQuantityModal(self.raffle_id, self.repo, self.max_buy))
            return
        try:
            quantity = int(selected)
        except (TypeError, ValueError):
            await interaction.response.send_message("❌ Invalid quantity", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        await _reserve_raffle_tickets(interaction, self.repo, self.raffle_id, quantity, use_followup=True)
class RafflePurchasePanelView(discord.ui.View):
    """Persistent purchase panel for raffle interactions."""
    def __init__(self, raffle_id: int):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.add_item(discord.ui.Button(
            label="🎟️ Buy Tickets",
            style=discord.ButtonStyle.success,
            custom_id=f"raffle:buy:{raffle_id}",
        ))
        self.add_item(discord.ui.Button(
            label="ℹ️ My Tickets",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raffle:my:{raffle_id}",
        ))
        self.children[0].callback = self.buy_tickets
        self.children[1].callback = self.my_tickets
    async def buy_tickets(self, interaction: discord.Interaction):
        try:
            repo = RafflesRepository(get_pool())
            raffle = await repo.get_raffle(self.raffle_id)
            if not raffle:
                await interaction.response.send_message("❌ Raffle not found", ephemeral=True)
                return
            entries = await repo.get_raffle_entries(self.raffle_id)
            max_buy = _max_buy_now(raffle, entries, interaction.user.id)
            if max_buy <= 0:
                await interaction.response.send_message("❌ No tickets available.", ephemeral=True)
                return
            if max_buy <= 1:
                await _reserve_raffle_tickets(interaction, repo, self.raffle_id, 1)
                return
            await interaction.response.send_message(
                content=f"Choose quantity (1-{max_buy})",
                view=RaffleQuantityPickerView(self.raffle_id, repo, max_buy),
                ephemeral=True,
            )
        except RuntimeError as e:
            message = "❌ Something went wrong opening ticket purchase. Please try again."
            if "Database not initialized" in str(e):
                message = "⚠️ Bot is starting up, try again in a few seconds."
            log.exception("Runtime error handling buy_tickets for raffle %s: %s", self.raffle_id, e)
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception as e:
            log.exception("Error handling buy_tickets for raffle %s: %s", self.raffle_id, e)
            message = "❌ Something went wrong opening ticket purchase. Please try again."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
    async def my_tickets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            repo = RafflesRepository(get_pool())
            entries = await repo.get_raffle_entries(self.raffle_id)
            mine = [e for e in entries if e.get("discord_id") == interaction.user.id]
            if not mine:
                await interaction.followup.send("ℹ️ You have no tickets in this raffle yet.", ephemeral=True)
                return
            paid = sum(int(e.get("num_tickets", 0)) for e in mine if e.get("payment_verified"))
            reserved = sum(int(e.get("num_tickets", 0)) for e in mine if not e.get("payment_verified"))
            total = paid + reserved
            info = f"🎟️ **Total tickets:** {total}\n✅ **Confirmed:** {paid}"
            if reserved:
                info += f"\n⏳ **Reserved (unverified):** {reserved}"
            await interaction.followup.send(info, ephemeral=True)
        except RuntimeError as e:
            message = "❌ Something went wrong fetching your tickets. Please try again."
            if "Database not initialized" in str(e):
                message = "⚠️ Bot is starting up, try again in a few seconds."
            log.exception("Runtime error handling my_tickets for raffle %s: %s", self.raffle_id, e)
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            log.exception("Error handling my_tickets for raffle %s: %s", self.raffle_id, e)
            await interaction.followup.send(
                "❌ Something went wrong fetching your tickets. Please try again.",
                ephemeral=True,
            )
class PaymentVerificationView(discord.ui.View):
    """View for manually verifying raffle payment."""
    def __init__(self, raffle_id: int, entry_id: int, repo: RafflesRepository, manual: bool = True):
        super().__init__(timeout=300)
        self.raffle_id = raffle_id
        self.entry_id = entry_id
        self.repo = repo
        self.manual = manual
    @discord.ui.button(label="✅ Verify Payment", style=discord.ButtonStyle.green)
    async def verify_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            service = RafflePaymentService(get_database())
            success, sold_out_raffle_id, error = await service.verify_entry_payment(
                self.entry_id, manual=True
            )
            if not success:
                await interaction.followup.send(
                    f"❌ {error or 'Payment not found. Make sure you sent the items to the creator.'}",
                    ephemeral=True
                )
                return
            if sold_out_raffle_id:
                raffle = await self.repo.get_raffle(sold_out_raffle_id)
                embed = discord.Embed(
                    title="🎉 RAFFLE SOLD OUT!",
                    description=f"🎁 **{raffle['prize']}**\n\n"
                               "All tickets sold! Drawing in **30 seconds**.",
                    color=discord.Color.gold()
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(
                    "✅ Payment verified! Your tickets are confirmed.", ephemeral=True
                )
            self.stop()
        except Exception as e:
            log.error(f"Payment verification error: {e}")
            await interaction.followup.send(
                "❌ Verification failed. Try again.", ephemeral=True
            )
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Reservation cancelled", ephemeral=True
        )
        self.stop()
class RafflePrizeImageUrlModal(discord.ui.Modal):
    """Modal for setting or replacing raffle prize image URL."""
    prize_image_url = discord.ui.TextInput(
        label="Prize Image URL",
        placeholder="https://i.imgur.com/example.png",
        required=True,
        max_length=1000,
    )
    def __init__(self, prompt_view: "RafflePrizeImagePromptView"):
        super().__init__(title="📷 Add Prize Image (optional)")
        self.prompt_view = prompt_view
    @staticmethod
    def _is_valid_url(url: str) -> bool:
        normalized = url.strip().lower()
        if not (normalized.startswith("http://") or normalized.startswith("https://")):
            return False
        return (
            normalized.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            or "imgur.com" in normalized
        )
    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.prompt_view.creator_discord_id:
            await interaction.response.send_message(
                "❌ Only the raffle creator can set the prize image.",
                ephemeral=True,
            )
            return
        raffle = await self.prompt_view.repo.get_raffle(self.prompt_view.raffle_id)
        if raffle and raffle.get("is_bundle"):
            await interaction.response.send_message(
                embed=create_error_embed("Not Allowed", "Bundle raffles do not support large prize images."),
                ephemeral=True,
            )
            return
        image_url = str(self.prize_image_url.value).strip()
        if not self._is_valid_url(image_url):
            await interaction.response.send_message(
                "❌ Invalid image URL. Use http(s) and either an image extension or imgur.com URL.",
                ephemeral=True,
            )
            return
        await self.prompt_view.repo.set_prize_image_url(self.prompt_view.raffle_id, image_url)
        await self.prompt_view.update_purchase_panel_image(image_url)
        await interaction.response.send_message("✅ Prize image added", ephemeral=True)
class RafflePrizeImagePromptView(discord.ui.View):
    """Prompt the raffle creator to optionally add a prize image URL."""
    def __init__(
        self,
        bot: commands.Bot,
        raffle_id: int,
        creator_discord_id: int,
        purchase_panel_channel_id: int,
        purchase_panel_message_id: int,
    ):
        super().__init__(timeout=3600)
        self.bot = bot
        self.raffle_id = raffle_id
        self.creator_discord_id = creator_discord_id
        self.purchase_panel_channel_id = purchase_panel_channel_id
        self.purchase_panel_message_id = purchase_panel_message_id
        self.repo = RafflesRepository(get_pool())
    async def update_purchase_panel_image(self, image_url: str) -> None:
        panel_channel = self.bot.get_channel(int(self.purchase_panel_channel_id))
        if panel_channel is None:
            try:
                panel_channel = await self.bot.fetch_channel(int(self.purchase_panel_channel_id))
            except Exception:
                panel_channel = None
        if panel_channel is None:
            log.warning(
                "Prize image set but purchase panel channel unavailable raffle_id=%s channel_id=%s",
                self.raffle_id,
                self.purchase_panel_channel_id,
            )
            return
        try:
            panel_message = await panel_channel.fetch_message(int(self.purchase_panel_message_id))
            if panel_message.embeds:
                embed = panel_message.embeds[0].copy()
            else:
                embed = discord.Embed(title=f"🎟️ Raffle #{self.raffle_id}")
            embed.set_image(url=image_url)
            await panel_message.edit(embed=embed, view=RafflePurchasePanelView(raffle_id=self.raffle_id))
        except Exception as e:
            log.error(f"Failed to update purchase panel image for raffle {self.raffle_id}: {e}")
    @discord.ui.button(label="📷 Add Prize Image (optional)", style=discord.ButtonStyle.primary)
    async def add_prize_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message(
                "❌ Only the raffle creator can set the prize image.",
                ephemeral=True,
            )
            return
        raffle = await self.repo.get_raffle(self.raffle_id)
        if raffle and raffle.get("is_bundle"):
            await interaction.response.send_message(
                embed=create_error_embed("Not Allowed", "Bundle raffles do not support large prize images."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RafflePrizeImageUrlModal(self))
    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip_upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message(
                "❌ Only the raffle creator can skip for this raffle.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("👌 Skipped prize image.", ephemeral=True)
class RafflesCog(commands.Cog):
    """Raffle commands with sell-out trigger support."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pack_drafts: dict[int, tuple[dict, datetime]] = {}
        self._payment_meta_cache: dict[str, dict | None] = {}
        self.check_raffles.start()
        self.cleanup_expired.start()
        self.auto_verify_payments.start()
    def cog_unload(self):
        self.check_raffles.cancel()
        self.cleanup_expired.cancel()
        self.auto_verify_payments.cancel()
    async def cog_load(self):
        """Register persistent raffle purchase views for existing panel messages."""
        try:
            repo = RafflesRepository(get_pool())
            panel_raffles = await repo.get_active_raffles_with_panels()
            for raffle in panel_raffles:
                self.bot.add_view(
                    RafflePurchasePanelView(raffle_id=int(raffle["raffle_id"])),
                    message_id=int(raffle["purchase_panel_message_id"]),
                )
            active_raffles = await repo.get_all_active_raffle_ids()
            panel_raffle_ids = {int(r["raffle_id"]) for r in panel_raffles}
            for raffle_id in active_raffles:
                if raffle_id in panel_raffle_ids:
                    continue
                self.bot.add_view(RafflePurchasePanelView(raffle_id=raffle_id))
            if panel_raffles or active_raffles:
                log.info(
                    "Registered %s message-bound and %s fallback raffle purchase views",
                    len(panel_raffles),
                    max(len(active_raffles) - len(panel_raffle_ids), 0),
                )
        except Exception as e:
            log.error("Failed registering persistent raffle views: %s", e)
    def store_pack_draft(self, creator_discord_id: int, draft: dict) -> None:
        self._pack_drafts[creator_discord_id] = (draft, datetime.utcnow() + timedelta(minutes=10))
    def get_pack_draft(self, creator_discord_id: int) -> dict | None:
        entry = self._pack_drafts.get(creator_discord_id)
        if not entry:
            return None
        draft, expires_at = entry
        if datetime.utcnow() > expires_at:
            self._pack_drafts.pop(creator_discord_id, None)
            return None
        return draft
    def pop_pack_draft(self, creator_discord_id: int) -> None:
        self._pack_drafts.pop(creator_discord_id, None)
    async def _get_payment_meta(self, payment_type: str) -> dict | None:
        name = "xanax" if payment_type == "xanax" else "erotic dvd"
        if name in self._payment_meta_cache:
            return self._payment_meta_cache[name]
        repo = TornItemsRepository(get_pool())
        meta = await repo.get_item_meta_by_name(name)
        self._payment_meta_cache[name] = meta
        return meta
    async def _get_single_prize_thumbnail_url(self, prize_text: str, is_bundle: bool) -> str | None:
        if not _is_single_item_prize(prize_text, is_bundle):
            return None
        normalized_name = _normalize_item_name(prize_text)
        if not normalized_name:
            return None
        resolver = ItemResolver(get_pool())
        item = await resolver.resolve_item(normalized_name)
        cleaned = (item or {}).get("image_url", "").strip()
        return cleaned or None
    async def build_payment_file(self, payment_type: str, amount: int):
        if not _is_supported_icon_payment(payment_type) or amount <= 0:
            return None
        meta = await self._get_payment_meta(payment_type)
        if not meta:
            return None
        return await build_icon_strip_file([
            {"image_url": meta.get("image_url"), "quantity": amount}
        ], filename="payments.png", icon_size=32, max_width=420)
    async def _build_bundle_file(self, bundle_entries: list[dict]):
        return await build_icon_strip_file(bundle_entries, filename="bundle.png", icon_size=36, max_width=700)
    async def create_raffle_from_draft(
        self,
        interaction: discord.Interaction,
        draft: dict,
        is_bundle: bool,
        bundle_text: str | None,
        bundle_entries: list[dict],
    ) -> None:
        repo = RafflesRepository(get_pool())
        try:
            raffle_id = await repo.create_raffle(
                guild_id=draft["guild_id"],
                creator_discord_id=draft["creator_discord_id"],
                prize=draft["prize"],
                ticket_payment_type=draft["ticket_payment_type"],
                ticket_price=draft["ticket_price"],
                tickets_available=draft["tickets_available"],
                max_tickets_per_user=draft["max_tickets_per_user"],
                end_time=draft["end_time"],
                end_trigger=draft["end_trigger"],
                hours_after_sold_out=draft["hours_after_sold_out"],
                is_bundle=is_bundle,
                bundle_text=bundle_text,
            )
            db = get_database()
            settings_repo = GuildSettingsRepository(db)
            settings = await settings_repo.get_or_create(interaction.guild_id)
            purchase_channel_id = settings.get("raffle_purchase_channel_id") or settings.get("raffle_channel_id")
            if not purchase_channel_id:
                await interaction.response.send_message(
                    "❌ Configure **raffle purchase panel channel** in `/setup` before creating raffles.",
                    ephemeral=True,
                )
                return
            guild = interaction.guild
            purchase_channel = guild.get_channel(int(purchase_channel_id)) if guild else None
            if purchase_channel is None and guild:
                try:
                    fetched = await guild.fetch_channel(int(purchase_channel_id))
                    if hasattr(fetched, "send"):
                        purchase_channel = fetched
                except Exception:
                    purchase_channel = None
            if purchase_channel is None:
                await interaction.response.send_message(
                    "❌ Raffle purchase panel channel is invalid or inaccessible. Update it in `/setup`.",
                    ephemeral=True,
                )
                return
            ticket_payment_type = draft["ticket_payment_type"]
            price_text = _payment_text(ticket_payment_type, draft["ticket_price"])
            purchase_panel_embed = discord.Embed(
                title=f"🎟️ Raffle #{raffle_id}: {draft['prize']}",
                description="Use the buttons below to buy tickets or check your entry.\n⏰ **Draw occurs 30 seconds after sellout.**",
                color=discord.Color.blurple(),
            )
            purchase_panel_embed.add_field(name="Price", value=price_text, inline=True)
            purchase_panel_embed.add_field(name="Tickets", value=f"{draft['tickets_available']}", inline=True)
            purchase_panel_embed.add_field(name="Max per user", value="Unlimited" if draft['max_tickets_per_user'] == 0 else str(draft['max_tickets_per_user']), inline=True)
            prize_thumbnail_url = await self._get_single_prize_thumbnail_url(draft["prize"], is_bundle)
            if prize_thumbnail_url:
                purchase_panel_embed.set_thumbnail(url=prize_thumbnail_url)
            single_item_meta = draft.get("single_item_meta")
            panel_message = await purchase_channel.send(embed=purchase_panel_embed, view=RafflePurchasePanelView(raffle_id=raffle_id))
            await repo.set_purchase_panel_ref(raffle_id=raffle_id, channel_id=purchase_channel.id, message_id=panel_message.id)
            if is_bundle and bundle_entries:
                bundle_file_io = await self._build_bundle_file(bundle_entries)
                if bundle_file_io:
                    bundle_file = discord.File(fp=bundle_file_io, filename="bundle.png")
                    bundle_embed = discord.Embed(title="Pack Contents", description=bundle_text or "", color=discord.Color.blurple())
                    bundle_embed.set_image(url="attachment://bundle.png")
                    await purchase_channel.send(embed=bundle_embed, file=bundle_file)
            announce_embed = discord.Embed(
                title="🎉 New Raffle Created!",
                description=f"🎁 **Prize:** {draft['prize']}\n🎟️ **Tickets:** {draft['tickets_available']} available\n💰 **Price:** {price_text} per ticket\n📋 **Max per user:** {'Unlimited' if draft['max_tickets_per_user'] == 0 else draft['max_tickets_per_user']}",
                color=discord.Color.green(),
            )
            if single_item_meta and not is_bundle:
                announce_embed.set_thumbnail(url=single_item_meta.get("image_url"))
            announce_embed.add_field(name="", value=f"👉 Head to {purchase_channel.mention} to purchase your ticket.", inline=False)
            if bool(settings.get("raffle_announce_enabled", True)):
                announce_channel_id = settings.get("raffle_announcement_channel_id")
                if announce_channel_id:
                    announce_channel = guild.get_channel(int(announce_channel_id)) if guild else None
                    if announce_channel is None and guild:
                        try:
                            fetched = await guild.fetch_channel(int(announce_channel_id))
                            if hasattr(fetched, "send"):
                                announce_channel = fetched
                        except Exception:
                            announce_channel = None
                    if announce_channel is not None:
                        await announce_channel.send(embed=announce_embed)
            response_text = f"✅ Raffle created. Purchase panel posted in {purchase_channel.mention}.\n{panel_message.jump_url}"
            if is_bundle:
                await interaction.response.send_message(response_text, ephemeral=True)
            else:
                await interaction.response.send_message(response_text, ephemeral=True)
                await interaction.followup.send(
                    "Optional: add a prize image URL (single-item raffles only).",
                    ephemeral=True,
                    view=RafflePrizeImagePromptView(
                        bot=self.bot,
                        raffle_id=raffle_id,
                        creator_discord_id=draft["creator_discord_id"],
                        purchase_panel_channel_id=purchase_channel.id,
                        purchase_panel_message_id=panel_message.id,
                    ),
                )
        except Exception as e:
            log.error("Failed to create raffle: %s", e)
            if interaction.response.is_done():
                await interaction.followup.send("❌ Failed to create raffle", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Failed to create raffle", ephemeral=True)
    # SINGLE ADMIN-ONLY CREATE COMMAND WITH EMOJIS IN CHOICES
    @app_commands.command(name="raffle_create", description="🎉 Create a new raffle (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def raffle_create(self, interaction: discord.Interaction):
        """🎉 Create a raffle - Admin only."""
        await interaction.response.send_modal(RaffleCreateModal(self.bot))
    @app_commands.command(name="raffle_draw", description="🎲 Draw a raffle winner (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(raffle_id="🎟️ ID of the raffle to draw")
    async def raffle_draw(self, interaction: discord.Interaction, raffle_id: int):
        """🎲 Manually trigger a raffle draw - Admin only."""
        await interaction.response.defer()
        repo = RafflesRepository(get_pool())
        result = await repo.draw_raffle_winner(raffle_id)
        if not result:
            await interaction.followup.send("❌ No entries or raffle not found")
            return
        # Send winner notification
        verification_cog = self.bot.get_cog("RaffleVerificationCog")
        if verification_cog:
            await verification_cog.send_winner_notification(result)
        embed = discord.Embed(
            title="🎉 RAFFLE WINNER!",
            description=f"🏆 **Winner:** <@{result['discord_id']}>\n"
                       f"🎟️ **Total Entries:** {result['total_entries']}",
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed)
    @app_commands.command(name="raffle_cancel", description="❌ Cancel a raffle (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(raffle_id="🎟️ ID of the raffle to cancel")
    async def raffle_cancel(self, interaction: discord.Interaction, raffle_id: int):
        """❌ Cancel an active raffle - Admin only."""
        repo = RafflesRepository(get_pool())
        cancelled = await repo.cancel_active_raffle(raffle_id)
        if not cancelled:
                await interaction.response.send_message(
                    "❌ Raffle not found or already completed/cancelled", ephemeral=True
                )
                return
        await interaction.response.send_message(
            f"✅ Raffle #{raffle_id} has been cancelled.", ephemeral=True
        )
    @app_commands.command(name="raffle_list", description="📋 List raffles (Admin only)")
    @app_commands.checks.has_permissions(administrator=True)
    async def raffle_list(self, interaction: discord.Interaction):
        """📋 List all active raffles - Admin only."""
        repo = RafflesRepository(get_pool())
        raffles = await repo.get_active_raffles(interaction.guild_id)
        if not raffles:
            await interaction.response.send_message(
                "📭 No active raffles", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="🎉 Active Raffles",
            color=discord.Color.blue()
        )
        for raffle in raffles:
            value = f"🎟️ Tickets: {raffle['tickets_sold']}/{raffle['tickets_available']}\n"
            if raffle.get("is_free") or raffle["ticket_payment_type"] == "free":
                value += "💰 Price: FREE"
            else:
                value += f"💰 Price: {_payment_text(raffle['ticket_payment_type'], raffle['ticket_price'])}"
            value += "\n⏰ Draw occurs 30 seconds after sellout."
            embed.add_field(
                name=f"#{raffle['raffle_id']}: {raffle['prize'][:50]}",
                value=value,
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @tasks.loop(seconds=30)
    async def auto_verify_payments(self):
        """Auto-poll Torn API for payment verification at 4:30 mark."""
        await self.bot.wait_until_ready()
        try:
            repo = RafflesRepository(get_pool())
            pending = await repo.get_pending_verifications()
            for entry in pending:
                try:
                    service = RafflePaymentService(get_database())
                    success, sold_out_id, error = await service.verify_entry_payment(
                        entry["entry_id"], manual=False
                    )
                    if success:
                        log.info(f"Auto-verified payment for entry {entry['entry_id']}")
                        try:
                            user = await self.bot.fetch_user(entry["discord_id"])
                            await user.send(
                                f"✅ Your raffle tickets for entry #{entry['raffle_id']} have been auto-verified!"
                            )
                        except:
                            pass
                        if sold_out_id:
                            raffle = await repo.get_raffle(sold_out_id)
                            guild = self.bot.get_guild(raffle["guild_id"])
                            if guild and guild.system_channel:
                                embed = discord.Embed(
                                    title="🎉 RAFFLE SOLD OUT!",
                                    description=f"🎁 **{raffle['prize']}** is now sold out! "
                                               "Drawing in **30 seconds**.",
                                    color=discord.Color.gold()
                                )
                                await guild.system_channel.send(embed=embed)
                    elif error and "expired" in error.lower():
                        await repo.cancel_expired_reservation(entry["entry_id"])
                        log.info(f"Cancelled expired reservation {entry['entry_id']}")
                except Exception as e:
                    log.error(f"Auto-verify error for entry {entry['entry_id']}: {e}")
        except Exception as e:
            log.error(f"Error in auto_verify_payments task: {e}")
    @tasks.loop(minutes=1)
    async def check_raffles(self):
        """Check for raffles that need to be drawn."""
        await self.bot.wait_until_ready()
        try:
            repo = RafflesRepository(get_pool())
            raffles = await repo.get_raffles_to_draw()
            for raffle in raffles:
                try:
                    result = await repo.draw_raffle_winner(raffle["raffle_id"])
                    if result:
                        verification_cog = self.bot.get_cog("RaffleVerificationCog")
                        if verification_cog:
                            await verification_cog.send_winner_notification(result)
                        guild = self.bot.get_guild(raffle["guild_id"])
                        if guild and guild.system_channel:
                            embed = discord.Embed(
                                title="🎉 RAFFLE WINNER!",
                                description=f"🎁 **{raffle['prize']}**\n\n"
                                           f"🏆 Winner: <@{result['discord_id']}>\n"
                                           f"🎟️ Total Entries: {result['total_entries']}",
                                color=discord.Color.gold()
                            )
                            await guild.system_channel.send(embed=embed)
                except Exception as e:
                    log.error(f"Error drawing raffle {raffle['raffle_id']}: {e}")
        except Exception as e:
            log.error(f"Error in check_raffles task: {e}")
    @tasks.loop(minutes=5)
    async def cleanup_expired(self):
        """Clean up expired unpaid reservations."""
        try:
            repo = RafflesRepository(get_pool())
            count = await repo.cleanup_expired_raffle_entries()
            if count > 0:
                log.info(f"Cleaned up {count} expired raffle entries")
        except Exception as e:
            log.error(f"Error cleaning up expired entries: {e}")
async def setup(bot: commands.Bot):
    await bot.add_cog(RafflesCog(bot))
