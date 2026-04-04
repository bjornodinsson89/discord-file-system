"""
Raffle system with sell-out trigger support and automatic payment verification.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from repositories.raffles import RafflesRepository
from repositories.torn_items import TornItemsRepository
from repositories.users import UsersRepository
from services.raffle_payment import RafflePaymentService
from services.payment_receipts import PaymentReceiptService
from services.logging_utils import log_event
from services.torn_identity import resolve_buyer_identity_for_paid_feature
from utils import GuildSettingsRepository, get_database, get_security_manager, get_torn_api, require_api_key
from utils.database import get_pool, is_initialized as db_is_initialized, wait_until_initialized
from utils.advisory_lock import run_with_advisory_lock
from utils.embeds import create_error_embed, clamp_percent, format_remaining_time, render_text_progress_bar
from utils.icon_strips import build_icon_strip_file
from utils.item_resolver import ItemResolver
from utils.payment_normalization import parse_payment_type
from utils.torn_api import TornAPIError
from utils.discord_safe_send import safe_send_channel
from utils.discord_perms import can_manage_paid_raffles
from utils.worker_throttle import db_heavy_worker_slot, sleep_startup_jitter
from utils.panel_edit_safety import PANEL_EDIT_SAFETY


def _format_admin_safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split()).strip()
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"


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
        return "Giveaway"
    if payment_type == "xanax":
        return f"💊 {amount} Xanax"
    if payment_type == "erotic_dvd":
        return f"📀 {amount} eDVD"
    return f"{amount} {payment_type}"


def _build_paid_raffle_panel_embed(raffle: dict) -> discord.Embed:
    raffle_id = int(raffle["raffle_id"])
    tickets_available = int(raffle.get("tickets_available") or 0)
    tickets_sold = max(0, int(raffle.get("tickets_sold") or 0))
    sold_for_percent = min(tickets_sold, tickets_available) if tickets_available > 0 else tickets_sold
    ticket_percent = clamp_percent((sold_for_percent / tickets_available) * 100) if tickets_available > 0 else 0
    now = datetime.now(timezone.utc)
    created_at = raffle.get("created_at")
    end_time = raffle.get("end_time")
    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if isinstance(end_time, datetime) and end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)
    has_real_end_time = isinstance(end_time, datetime) and isinstance(created_at, datetime)
    duration = int((end_time - created_at).total_seconds()) if has_real_end_time else 0
    elapsed = int((now - created_at).total_seconds()) if has_real_end_time else 0
    time_percent = clamp_percent((elapsed / duration) * 100) if duration > 0 else 0
    tickets_display = "Unlimited" if tickets_available <= 0 else str(tickets_available)
    draw_text = "30 seconds after sellout" if raffle.get("end_trigger") == "tickets_sold" else (f"<t:{int(end_time.timestamp())}:R>" if isinstance(end_time, datetime) else "Manual / no timer")
    price_text = _payment_text(str(raffle.get("ticket_payment_type") or ""), int(raffle.get("ticket_price") or 0))
    embed = discord.Embed(title=f"🎟️ Raffle #{raffle_id}: {raffle.get('prize')}", description="Use the buttons below to buy tickets or check your entry.", color=discord.Color.blurple())
    live_stats_lines = [
        f"Tickets: `{render_text_progress_bar(ticket_percent)}`",
        f"Sold / Available: **{tickets_sold}/{tickets_display}**",
    ]
    if has_real_end_time and duration > 0:
        live_stats_lines.insert(1, f"Time: `{render_text_progress_bar(time_percent)}`")
    embed.add_field(name="LIVE STATS", value="\n".join(live_stats_lines), inline=False)
    embed.add_field(name="PRIZE", value=f"🎁 {raffle.get('prize')}", inline=False)
    embed.add_field(name="RAFFLE INFO", value=f"Ticket price: **{price_text}**\nHost: <@{int(raffle.get('creator_discord_id') or 0)}>\nDraw: {draw_text}", inline=False)
    if raffle.get("admin_comments"):
        embed.add_field(name="📌 Notes", value=str(raffle["admin_comments"])[:1024], inline=False)
    embed.add_field(name="HOW TO PLAY", value="1) Click **🎟️ Buy Tickets**\n2) Send payment in Torn\n3) Verify payment", inline=False)
    image_url = str(raffle.get("prize_image_url") or "").strip()
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=(f"Last updated: {format_remaining_time(end_time, now)} remaining" if has_real_end_time else "Last updated just now"))
    return embed

def _compute_panel_tickets_value(raffle: dict) -> str:
    tickets_available = int(raffle.get("tickets_available") or 0)
    tickets_sold = int(raffle.get("tickets_sold") or 0)
    if tickets_available <= 0:
        return f"Tickets Sold: {tickets_sold}/Unlimited"
    if tickets_sold < 0:
        tickets_sold = 0
    if tickets_sold > tickets_available:
        tickets_sold = tickets_available
    return f"Tickets Sold: {tickets_sold}/{tickets_available}"


async def _update_raffle_purchase_panel_counts(
    bot: "discord.Client",
    repo: "RafflesRepository",
    raffle_id: int,
) -> None:
    try:
        raffle = await repo.get_raffle(int(raffle_id))
        if not raffle:
            return
        ref = await repo.get_purchase_panel_ref(int(raffle_id))
        if not ref:
            return
        channel_id = ref.get("purchase_panel_channel_id")
        message_id = ref.get("purchase_panel_message_id")
        if not channel_id or not message_id:
            return

        channel = bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await bot.fetch_channel(int(channel_id))
            except Exception:
                channel = None
        if channel is None or not hasattr(channel, "fetch_message"):
            return

        try:
            msg = await channel.fetch_message(int(message_id))
        except Exception:
            return

        embed = _build_paid_raffle_panel_embed(raffle)
        allow_token = bool(raffle.get("allow_prize_token_purchase"))
        view = RafflePurchasePanelView(raffle_id=int(raffle_id), allow_prize_token_purchase=allow_token)
        await PANEL_EDIT_SAFETY.request_edit(
            msg,
            embed=embed,
            view=view,
            min_interval_seconds=5,
            force=False,
        )
    except Exception:
        log.exception("Failed to update raffle purchase panel counts raffle_id=%s", raffle_id)



_FALLBACK_ITEM_NAMES = {206: "Xanax", 366: "Erotic DVD"}


_PRIZE_VERIFY_COOLDOWN_SECONDS = 60
_PRIZE_VERIFY_WINDOW_SECONDS = 7 * 24 * 60 * 60


def _format_discord_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:F>"


def _extract_prize_map(prize_items: list[dict]) -> dict[int, int]:
    expected: dict[int, int] = {}
    for item in prize_items:
        item_id = _safe_int(item.get("item_id"))
        qty = _safe_int(item.get("qty"))
        if not item_id or not qty:
            continue
        expected[item_id] = expected.get(item_id, 0) + qty
    return expected


def _extract_log_id(entry: dict) -> str:
    for key in ("id", "log_id", "log", "logid"):
        value = entry.get(key)
        if value not in (None, ""):
            return str(value)
    return "unknown"


def _disable_view(view: discord.ui.View | None) -> discord.ui.View | None:
    if view is None:
        return None
    for child in view.children:
        child.disabled = True
    return view


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _resolve_torn_identity(discord_id: int) -> tuple[str, int, bool]:
    safe_discord_id = _safe_int(discord_id) or 0
    row = None
    if safe_discord_id > 0:
        row = await UsersRepository(get_pool()).get_user_api_key(safe_discord_id)

    torn_id = int((row or {}).get("torn_user_id") or 0)
    torn_name = str((row or {}).get("torn_name") or "").strip()
    if not torn_name:
        torn_name = "User"
    return torn_name, torn_id, bool(torn_id)


async def _is_raffle_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild.id)
    return can_manage_paid_raffles(interaction.user, settings)


async def _can_manage_raffle_edit(interaction: discord.Interaction, raffle: dict | None) -> bool:
    if raffle and int(raffle.get("creator_discord_id") or 0) == int(interaction.user.id):
        return True
    return await _is_raffle_admin(interaction)


def _resolve_purchase_panel_channel_id(settings: dict, *, is_giveaway: bool) -> tuple[int | None, str | None, str | None]:
    if is_giveaway:
        giveaway_channel_id = _safe_int(settings.get("raffle_giveaway_purchase_channel_id"))
        if giveaway_channel_id:
            return giveaway_channel_id, "raffle_giveaway_purchase_channel_id", None
        paid_channel_id = _safe_int(settings.get("raffle_purchase_channel_id"))
        if paid_channel_id:
            return paid_channel_id, "raffle_purchase_channel_id", None
        return None, None, "❌ Set giveaway purchase panel channel in `/setup` (or set paid raffle purchase panel channel as fallback)."

    paid_channel_id = _safe_int(settings.get("raffle_purchase_channel_id"))
    if paid_channel_id:
        return paid_channel_id, "raffle_purchase_channel_id", None
    raffle_channel_id = _safe_int(settings.get("raffle_channel_id"))
    if raffle_channel_id:
        return raffle_channel_id, "raffle_channel_id", None
    return None, None, "❌ Set paid raffle purchase panel channel in `/setup` (or set raffle channel fallback)."


def _raffle_closed_status(raffle: dict | None) -> bool:
    if not raffle:
        return True
    return str(raffle.get("status") or "").lower() in {"closed", "cancelled", "expired", "completed"}


async def _resolve_item_name(item_repo: TornItemsRepository, item_id: int) -> str:
    meta = await item_repo.get_item_meta(int(item_id))
    if meta and meta.get("name"):
        return str(meta["name"])
    return _FALLBACK_ITEM_NAMES.get(int(item_id), f"Item {int(item_id)}")


async def _build_prize_instruction_text(repo: RafflesRepository, raffle: dict) -> tuple[str, list[dict], bool]:
    if raffle.get("prize_item_id") and raffle.get("prize_quantity"):
        items = [{"item_id": int(raffle["prize_item_id"]), "qty": int(raffle["prize_quantity"])}]
    else:
        items = await repo.get_prize_items_payload(raffle)

    if not items:
        return "Prize is not a Torn item; cannot auto-verify via logs.", [], False

    item_repo = TornItemsRepository(get_pool())
    if len(items) == 1:
        item = items[0]
        item_name = await _resolve_item_name(item_repo, int(item["item_id"]))
        return f"Send: {int(item['qty'])} × {item_name} (item {int(item['item_id'])})", items, True

    lines = ["Send:"]
    for item in items:
        item_id = int(item["item_id"])
        qty = int(item["qty"])
        item_name = await _resolve_item_name(item_repo, item_id)
        lines.append(f"- {qty} × {item_name} ({item_id})")
    return "\n".join(lines), items, True




class AdminPrizeVerificationView(discord.ui.View):
    def __init__(self, raffle_id: int, *, verified: bool = False):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label="Verified ✅" if verified else "Verify Prize",
            style=discord.ButtonStyle.primary,
            custom_id=f"raffle:verify_prize:{raffle_id}",
            disabled=verified,
        )
        button.callback = self.verify_prize
        self.add_item(button)
        self.raffle_id = raffle_id

    async def verify_prize(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = interaction.client.get_cog("RafflesCog")
        if cog is None:
            await interaction.followup.send("Raffle system unavailable.", ephemeral=True)
            return
        await cog.handle_admin_prize_verification(interaction, self.raffle_id, self)

class RafflePrizeConfirmDMView(discord.ui.View):
    def __init__(self, raffle_id: int, creator_discord_id: int, *, disabled: bool = False):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.creator_discord_id = creator_discord_id
        if disabled:
            _disable_view(self)

    @discord.ui.button(label="✅ Confirm Prize Sent", style=discord.ButtonStyle.success, custom_id="raffle:confirm_prize_sent")
    async def confirm_prize_sent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(self.raffle_id)
        if not raffle or _raffle_closed_status(raffle) and str(raffle.get("status")) != "completed":
            _disable_view(self)
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await interaction.followup.send("This is closed.", ephemeral=True)
            return

        guild = interaction.client.get_guild(int(raffle["guild_id"]))
        allowed = interaction.user.id == int(self.creator_discord_id)
        if not allowed and guild and isinstance(interaction.user, discord.Member):
            db = get_database()
            settings = await GuildSettingsRepository(db).get_or_create(guild.id)
            allowed = can_manage_paid_raffles(interaction.user, settings)

        if not allowed:
            await interaction.followup.send("Only the raffle creator, Admin role, or Paid Raffle Admin role can confirm this.", ephemeral=True)
            return

        if raffle.get("prize_sent_at"):
            _disable_view(self)
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await interaction.followup.send("Prize already confirmed.", ephemeral=True)
            return

        if str(raffle.get("status")) != "completed":
            await interaction.followup.send("This is closed.", ephemeral=True)
            return

        users_repo = UsersRepository(get_pool())
        security = get_security_manager()
        torn_api = get_torn_api()

        creator_key = await users_repo.get_user_api_key(int(raffle["creator_discord_id"]))
        encrypted_key = (creator_key or {}).get("encrypted_key") or (creator_key or {}).get("api_key_encrypted")
        creator_torn_id = _safe_int((creator_key or {}).get("torn_user_id")) or _safe_int(raffle.get("creator_torn_id"))
        if not encrypted_key or not creator_torn_id:
            await interaction.followup.send("Creator Torn API key is missing; cannot auto-verify.", ephemeral=True)
            return

        winner_torn_id = _safe_int(raffle.get("winner_torn_id"))
        if not winner_torn_id:
            winner_key = await users_repo.get_user_api_key(int(raffle.get("winner_discord_id") or 0))
            winner_torn_id = _safe_int((winner_key or {}).get("torn_user_id"))
        if not winner_torn_id:
            await interaction.followup.send("Winner has not linked Torn ID; cannot auto-verify.", ephemeral=True)
            return

        instructions, expected_items, verifiable = await _build_prize_instruction_text(repo, raffle)
        if not verifiable:
            await interaction.followup.send(instructions, ephemeral=True)
            return

        completion_ts = raffle.get("drawn_at") or raffle.get("tickets_fully_sold_at") or raffle.get("created_at")
        since_timestamp = int(completion_ts.timestamp()) if completion_ts else 0

        api_key = security.decrypt_api_key(encrypted_key)
        try:
            logs = await torn_api.get_item_send_receive_logs(
                api_key,
                limit=50,
                audit_discord_id=int(interaction.user.id),
                audit_torn_id=int(creator_torn_id),
                audit_context="payment_verify_logs",
                audit_query_meta={"cat": 85, "limit": 50},
            )
        except TornAPIError as exc:
            await interaction.followup.send(f"Verification failed: {exc}", ephemeral=True)
            return

        expected_map = {int(i["item_id"]): int(i["qty"]) for i in expected_items}
        matched = None
        for entry in logs:
            timestamp = _safe_int(entry.get("timestamp")) or 0
            if since_timestamp and timestamp < since_timestamp:
                continue
            data = entry.get("data") or {}
            if _safe_int(data.get("receiver")) != int(winner_torn_id):
                continue
            found_map: dict[int, int] = {}
            for item in data.get("items") or []:
                iid = _safe_int(item.get("id"))
                qty = _safe_int(item.get("qty")) or 0
                if iid:
                    found_map[iid] = found_map.get(iid, 0) + qty
            if found_map == expected_map:
                matched = entry
                break

        if not matched:
            await interaction.followup.send("No matching send log found yet. Send the prize, then try again.", ephemeral=True)
            return

        await repo.set_prize_sent(self.raffle_id)
        receipts = PaymentReceiptService(get_pool())
        await receipts.create_and_verify(
            featureType="raffle_prize",
            featureRefId=self.raffle_id,
            payer_discord_id=int(raffle["creator_discord_id"]),
            payer_torn_id=creator_torn_id,
            payee_discord_id=_safe_int(raffle.get("winner_discord_id")),
            payee_torn_id=winner_torn_id,
            amount=sum(expected_map.values()),
            currency_type="item_bundle" if len(expected_map) > 1 else "item",
            metadata=matched,
            verifier_discord_id=interaction.user.id,
            verifier_torn_id=creator_torn_id,
        )

        announce_channel_id = raffle.get("announcement_channel_id")
        if announce_channel_id:
            item_lines = []
            for item_id, qty in expected_map.items():
                item_name = await _resolve_item_name(TornItemsRepository(get_pool()), item_id)
                item_lines.append(f"- {qty} × {item_name} ({item_id})")
            log_id = matched.get("id") or matched.get("log") or "unknown"
            ts = _safe_int(matched.get("timestamp")) or int(datetime.now(timezone.utc).timestamp())
            proof = (
                "✅ **Prize Sent Confirmed**\n"
                f"Raffle: {raffle.get('prize')} (#{self.raffle_id})\n"
                f"Winner: <@{int(raffle['winner_discord_id'])}> (Torn {winner_torn_id})\n"
                f"Sender: <@{int(raffle['creator_discord_id'])}> (Torn {creator_torn_id})\n"
                f"Log: {log_id} at <t:{ts}:F>\n"
                f"Items:\n" + "\n".join(item_lines)
            )
            await safe_send_channel(interaction.client, int(announce_channel_id), content=proof)

        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0].copy()
        else:
            embed = discord.Embed(title="Prize Delivery", color=discord.Color.green())
        log_id = matched.get("id") or matched.get("log") or "unknown"
        embed.add_field(name="Status", value=f"Confirmed ✅\nMatched Log ID: {log_id}", inline=False)
        _disable_view(self)
        await interaction.message.edit(embed=embed, view=self)
        await interaction.followup.send("✅ Prize send verified and logged.", ephemeral=True)

def _raffle_remaining_tickets(raffle: dict, entries: list[dict]) -> int:
    if int(raffle.get("tickets_available") or 0) == 0:
        return 10**9
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
        label="Payment type (Giveaway / Xanax / eDVD)",
        placeholder="Giveaway / Xanax / Erotic DvD",
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
            raw_payment_type = str(self.payment_type.value)
            raw_price_input = str(self.ticket_price.value or "").strip()
            try:
                payment_type = parse_payment_type(raw_payment_type, allow_free=True)
            except ValueError as exc:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid payment type", str(exc)),
                    ephemeral=True,
                )
                return
            is_giveaway = not raw_price_input or raw_price_input.lower() == "free" or payment_type == "free"
            if is_giveaway:
                payment_type = "free"
                price = 0
            else:
                price = int(raw_price_input)
                if price <= 0:
                    await interaction.response.send_message(
                        embed=create_error_embed("Invalid ticket price", "Ticket Price must be greater than 0 for paid raffles."),
                        ephemeral=True,
                    )
                    return
            tickets_available_raw = str(self.tickets_available.value or "").strip().lower()
            if tickets_available_raw in {"unlimited", "inf", "infinite", "0"}:
                total = 0
            else:
                total = int(self.tickets_available.value)
            max_per = int(self.max_per_user.value or 0)
            if total < 0:
                await interaction.response.send_message(
                    embed=create_error_embed("Invalid total tickets", "Total Tickets must be 0/unlimited or 1 or greater."),
                    ephemeral=True,
                )
                return
            if total != 0 and total < 1:
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
            end_trigger = "time" if total == 0 else "tickets_sold"
            hours_after_sold_out = None
            users_repo = UsersRepository(get_pool())
            if not await require_api_key(interaction, get_database(), "create a raffle"):
                return
            creator_key = await users_repo.get_user_api_key(interaction.user.id)
            if not creator_key or not creator_key.get("torn_user_id"):
                await interaction.followup.send(
                    "❗ You need to register your Torn API key before you can create a raffle.\n"
                    "Run `/set api key` to register it.\n"
                    "This is required to verify payments and fetch your energy/cooldowns.",
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
                "admin_comments": None,
                "allow_prize_token_purchase": False,
                "prize_token_cost_per_ticket": None,
            }
            if draft["end_trigger"] == "time" and draft["end_time"] is None:
                raise ValueError("Time-based raffles require an end_time")
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
            raffle_cog.store_create_draft(interaction.user.id, draft)
            await interaction.response.send_message(
                "Draft saved. Add optional notes, then set Prize Token purchase option.",
                ephemeral=True,
                view=RaffleCommentsChoiceView(self.bot, interaction.user.id),
            )
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
        await raffle_cog.prompt_prize_token_step(interaction, draft, is_bundle=False, bundle_text=None, bundle_entries=[])


class RaffleCommentsChoiceView(discord.ui.View):
    def __init__(self, bot: commands.Bot, creator_discord_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.creator_discord_id = creator_discord_id

    @discord.ui.button(label="Add Comments", style=discord.ButtonStyle.primary)
    async def add_comments(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can continue this draft.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None or not raffle_cog.get_create_draft(interaction.user.id):
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        await interaction.response.send_modal(RaffleCommentsModal(self.bot, self.creator_discord_id))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary)
    async def skip_comments(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can continue this draft.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None:
            await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
            return
        draft = raffle_cog.get_create_draft(interaction.user.id)
        if not draft:
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        raffle_cog.pop_create_draft(interaction.user.id)
        draft["admin_comments"] = None
        await raffle_cog.prompt_prize_token_step(interaction, draft, is_bundle=False, bundle_text=None, bundle_entries=[])


class RaffleCommentsModal(discord.ui.Modal):
    admin_comments = discord.ui.TextInput(
        label="Comments / Notes (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, bot: commands.Bot, creator_discord_id: int):
        super().__init__(title="Add Raffle Notes")
        self.bot = bot
        self.creator_discord_id = creator_discord_id

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can continue this draft.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None:
            await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
            return
        draft = raffle_cog.get_create_draft(interaction.user.id)
        if not draft:
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        draft["admin_comments"] = str(self.admin_comments.value or "").strip() or None
        raffle_cog.pop_create_draft(interaction.user.id)
        await raffle_cog.prompt_prize_token_step(interaction, draft, is_bundle=False, bundle_text=None, bundle_entries=[])




class RafflePrizeTokenCostModal(discord.ui.Modal):
    token_cost_per_ticket = discord.ui.TextInput(
        label="Prize Token cost per ticket",
        placeholder="10",
        required=True,
        max_length=9,
    )

    def __init__(self, bot: commands.Bot, creator_discord_id: int, is_bundle: bool, bundle_text: str | None, bundle_entries: list[dict]):
        super().__init__(title="Prize Token Purchase")
        self.bot = bot
        self.creator_discord_id = creator_discord_id
        self.is_bundle = is_bundle
        self.bundle_text = bundle_text
        self.bundle_entries = bundle_entries

    async def on_submit(self, interaction: discord.Interaction):
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None:
            await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
            return
        draft = raffle_cog.get_create_draft(interaction.user.id)
        if not draft:
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        try:
            cost = int(str(self.token_cost_per_ticket.value).strip())
        except ValueError:
            await interaction.response.send_message("❌ Token cost must be a whole number greater than 0.", ephemeral=True)
            return
        if cost <= 0:
            await interaction.response.send_message("❌ Token cost must be greater than 0.", ephemeral=True)
            return
        draft["allow_prize_token_purchase"] = True
        draft["prize_token_cost_per_ticket"] = cost
        raffle_cog.pop_create_draft(interaction.user.id)
        await raffle_cog.create_raffle_from_draft(interaction, draft, is_bundle=self.is_bundle, bundle_text=self.bundle_text, bundle_entries=self.bundle_entries)


class RafflePrizeTokenChoiceView(discord.ui.View):
    def __init__(self, bot: commands.Bot, creator_discord_id: int, is_bundle: bool, bundle_text: str | None, bundle_entries: list[dict]):
        super().__init__(timeout=600)
        self.bot = bot
        self.creator_discord_id = creator_discord_id
        self.is_bundle = is_bundle
        self.bundle_text = bundle_text
        self.bundle_entries = bundle_entries

    @discord.ui.button(label="Yes, Allow Prize Tokens", style=discord.ButtonStyle.primary)
    async def enable(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can continue this draft.", ephemeral=True)
            return
        await interaction.response.send_modal(RafflePrizeTokenCostModal(self.bot, self.creator_discord_id, self.is_bundle, self.bundle_text, self.bundle_entries))

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def disable(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.creator_discord_id:
            await interaction.response.send_message("❌ Only the raffle creator can continue this draft.", ephemeral=True)
            return
        raffle_cog = self.bot.get_cog("RafflesCog")
        if raffle_cog is None:
            await interaction.response.send_message("❌ Raffle system unavailable.", ephemeral=True)
            return
        draft = raffle_cog.get_create_draft(interaction.user.id)
        if not draft:
            await interaction.response.send_message("❌ Draft expired. Please run /raffle_create again.", ephemeral=True)
            return
        draft["allow_prize_token_purchase"] = False
        draft["prize_token_cost_per_ticket"] = None
        raffle_cog.pop_create_draft(interaction.user.id)
        await raffle_cog.create_raffle_from_draft(interaction, draft, is_bundle=self.is_bundle, bundle_text=self.bundle_text, bundle_entries=self.bundle_entries)

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
    _ = use_followup  # Compatibility no-op; response mode is auto-detected.

    async def _send_response(**kwargs):
        if interaction.response.is_done():
            try:
                await interaction.followup.send(**kwargs)
                return
            except Exception:
                if not interaction.response.is_done():
                    await interaction.response.send_message(**kwargs)
                    return
                raise
        try:
            await interaction.response.send_message(**kwargs)
        except discord.errors.InteractionResponded:
            await interaction.followup.send(**kwargs)

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

    # Handle GIVEAWAY ENTRY
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
                           f"💰 **Price:** 🎫 Giveaway",
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
            log.error(f"Failed giveaway entry: {e}")
            await _send_error("❌ Failed to enter raffle")
            return
    # PAID ENTRY
    users_repo = UsersRepository(get_pool())
    creator_discord_id = int(raffle["creator_discord_id"])
    creator_key = await users_repo.get_user_api_key(creator_discord_id)
    creator_torn_id = int(raffle.get("creator_torn_id") or (creator_key or {}).get("torn_user_id") or 0)
    creator_name = str((creator_key or {}).get("torn_name") or "").strip() or "User"
    if not creator_torn_id or not creator_key or not creator_key.get("encrypted_key"):
        await _send_error("❌ Raffle creator Torn verification key is not configured. Please contact an admin.")
        return

    if interaction.guild is None:
        await _send_error("❌ Paid raffle entries are only available in servers.")
        return

    identity, resolve_error = await resolve_buyer_identity_for_paid_feature(
        guild=interaction.guild,
        buyer_discord_id=int(interaction.user.id),
        creator_discord_id=creator_discord_id,
        db=get_database(),
    )
    if not identity:
        await _send_error(f"❌ {resolve_error}")
        return

    reserved_until = datetime.utcnow() + timedelta(minutes=5)
    try:
        entry = await repo.reserve_entry(
            raffle_id=raffle_id,
            discord_id=interaction.user.id,
            torn_user_id=int(identity.torn_user_id),
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
            value=(
                f"⏱️ Auto-verification at 4:30, expires at 5:00\n"
                f"Send **{_payment_text(raffle['ticket_payment_type'], total_cost)}** to {creator_name} [{creator_torn_id}] in Torn!"
            ),
            inline=False
        )
        embed.add_field(
            name="💳 How to Pay",
            value="📨 Send items via Torn to the raffle creator, then click '✅ Verify Now' to check early.",
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
        await _reserve_raffle_tickets(interaction, self.repo, self.raffle_id, quantity)
class RafflePurchasePanelView(discord.ui.View):
    """Persistent purchase panel for raffle interactions."""
    def __init__(self, raffle_id: int, allow_prize_token_purchase: bool = False):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
        self.allow_prize_token_purchase = allow_prize_token_purchase

        buy_button = discord.ui.Button(
            label="🎟️ Buy Tickets",
            style=discord.ButtonStyle.success,
            custom_id=f"raffle:buy:{raffle_id}",
        )
        buy_button.callback = self.buy_tickets
        self.add_item(buy_button)

        if self.allow_prize_token_purchase:
            token_button = discord.ui.Button(
                label="🪙 Buy With Prize Tokens",
                style=discord.ButtonStyle.secondary,
                custom_id=f"raffle:buy_tokens:{raffle_id}",
            )
            token_button.callback = self.buy_with_prize_tokens
            self.add_item(token_button)

        my_tickets_button = discord.ui.Button(
            label="ℹ️ My Tickets",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raffle:my:{raffle_id}",
        )
        my_tickets_button.callback = self.my_tickets
        self.add_item(my_tickets_button)

        manage_button = discord.ui.Button(
            label="Manage",
            style=discord.ButtonStyle.primary,
            custom_id=f"raffle:manage:{raffle_id}",
        )
        manage_button.callback = self.manage
        self.add_item(manage_button)

    async def buy_with_prize_tokens(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "⚠️ Prize Token purchases are not enabled yet for this server.",
            ephemeral=True,
        )

    async def buy_tickets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            repo = RafflesRepository(get_pool())
            raffle = await repo.get_raffle(self.raffle_id)
            if not raffle:
                await interaction.followup.send("❌ Raffle not found", ephemeral=True)
                return
            if _raffle_closed_status(raffle):
                _disable_view(self)
                try:
                    await interaction.message.edit(view=self)
                except Exception:
                    pass
                await interaction.followup.send("This is closed.", ephemeral=True)
                return
            entries = await repo.get_raffle_entries(self.raffle_id)
            max_buy = _max_buy_now(raffle, entries, interaction.user.id)
            if max_buy <= 0:
                await interaction.followup.send("❌ No tickets available.", ephemeral=True)
                return
            if max_buy <= 1:
                await _reserve_raffle_tickets(interaction, repo, self.raffle_id, 1)
                return
            await interaction.followup.send(
                content=f"Choose quantity (1-{max_buy})",
                view=RaffleQuantityPickerView(self.raffle_id, repo, max_buy),
                ephemeral=True,
            )
        except RuntimeError as e:
            message = "❌ Something went wrong opening ticket purchase. Please try again."
            if "Database not initialized" in str(e):
                message = "⚠️ Bot is starting up, try again in a few seconds."
            log.exception("Runtime error handling buy_tickets for raffle %s: %s", self.raffle_id, e)
            await interaction.followup.send(message, ephemeral=True)
        except Exception as e:
            log.exception("Error handling buy_tickets for raffle %s: %s", self.raffle_id, e)
            message = "❌ Something went wrong opening ticket purchase. Please try again."
            await interaction.followup.send(message, ephemeral=True)
    async def my_tickets(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            repo = RafflesRepository(get_pool())
            raffle = await repo.get_raffle(self.raffle_id)
            if _raffle_closed_status(raffle):
                _disable_view(self)
                try:
                    await interaction.message.edit(view=self)
                except Exception:
                    pass
                await interaction.followup.send("This is closed.", ephemeral=True)
                return
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

    async def manage(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            if not await _is_raffle_admin(interaction):
                log.info("Paid raffle manage denied user_id=%s raffle_id=%s", interaction.user.id, self.raffle_id)
                await interaction.followup.send("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
                return
            manage_view = RaffleManageView(self.raffle_id)
            message = await interaction.followup.send(
                "Raffle management controls:",
                view=manage_view,
                ephemeral=True,
                wait=True,
            )
            manage_view.message = message
        except Exception:
            log.exception("Raffle manage panel failed raffle_id=%s", self.raffle_id)
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong opening raffle management.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong opening raffle management.", ephemeral=True)


class RaffleActionConfirmView(discord.ui.View):
    def __init__(self, manage_view: "RaffleManageView", *, action: str, owner_user_id: int):
        super().__init__(timeout=180)
        self.manage_view = manage_view
        self.action = action
        self.owner_user_id = int(owner_user_id)

        if action == "draw":
            confirm_label = "Confirm Draw"
            back_label = "Keep Open / Back"
        else:
            confirm_label = "Confirm Cancel"
            back_label = "Keep Active / Back"

        confirm_button = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
        confirm_button.callback = self._confirm
        self.add_item(confirm_button)

        back_button = discord.ui.Button(label=back_label, style=discord.ButtonStyle.secondary)
        back_button.callback = self._back
        self.add_item(back_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_user_id:
            await interaction.response.send_message("Only the admin who opened this confirmation can use it.", ephemeral=True)
            return False
        return True

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        if not await self.manage_view._ensure_admin(interaction):
            return
        if self.action == "draw":
            await self.manage_view._perform_draw(interaction)
        else:
            await self.manage_view._perform_cancel(interaction)

    async def _back(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("No changes made.", ephemeral=True)


class RaffleManageView(discord.ui.View):
    def __init__(self, raffle_id: int):
        super().__init__(timeout=300)
        self.raffle_id = int(raffle_id)
        self.message: discord.Message | None = None

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        if not await _is_raffle_admin(interaction):
            log.info("Paid raffle manage denied in control view user_id=%s raffle_id=%s", interaction.user.id, self.raffle_id)
            await interaction.followup.send("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
            return False
        return True

    async def _ensure_can_edit(self, interaction: discord.Interaction, raffle: dict | None = None) -> bool:
        if not await _can_manage_raffle_edit(interaction, raffle):
            log.info("Raffle edit denied user_id=%s raffle_id=%s", interaction.user.id, self.raffle_id)
            await interaction.followup.send("Only the raffle host or a raffle admin can edit this raffle.", ephemeral=True)
            return False
        return True

    async def _perform_draw(self, interaction: discord.Interaction) -> None:
        try:
            repo = RafflesRepository(get_pool())
            draw_result = await repo.force_draw_raffle_winner_atomic(self.raffle_id)
            state = str(draw_result.get("state") or "")
            if state == "not_found":
                await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
                return
            if state == "no_entries":
                await interaction.followup.send("❌ No verified entries found for this raffle.", ephemeral=True)
                return
            if state == "already_drawn":
                await interaction.followup.send("❌ This raffle has already been drawn or cancelled.", ephemeral=True)
                return
            if state == "not_drawable":
                await interaction.followup.send("❌ Only active raffles can be drawn.", ephemeral=True)
                return
            if state != "drawn":
                await interaction.followup.send("❌ Unable to draw raffle right now.", ephemeral=True)
                return

            result = draw_result.get("winner")
            if not result:
                await interaction.followup.send("❌ Unable to draw raffle right now.", ephemeral=True)
                return
            raffle = await repo.get_raffle(self.raffle_id)
            cog = interaction.client.get_cog("RafflesCog") if interaction.client else None
            if raffle and cog:
                await cog._handle_raffle_completed(raffle, result)
            await interaction.followup.send(f"✅ Raffle #{self.raffle_id} drawn.", ephemeral=True)
        except Exception:
            log.exception("Raffle draw manage callback failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to draw raffle right now.", ephemeral=True)

    async def _perform_cancel(self, interaction: discord.Interaction) -> None:
        try:
            repo = RafflesRepository(get_pool())
            raffle_before = await repo.get_raffle(self.raffle_id)
            cancelled = await repo.cancel_active_raffle(self.raffle_id)
            if not cancelled:
                await interaction.followup.send("❌ Raffle not found or already completed/cancelled", ephemeral=True)
                return
            raffle = await repo.get_raffle(self.raffle_id) or raffle_before
            cog = interaction.client.get_cog("RafflesCog") if interaction.client else None
            if raffle and cog:
                await cog._disable_raffle_panels(raffle, status_text="Raffle cancelled.")
                await cog._disable_prize_dm_confirm(raffle)
                await repo.mark_cleaned(self.raffle_id)
            await interaction.followup.send(f"✅ Raffle #{self.raffle_id} has been cancelled.", ephemeral=True)
        except Exception:
            log.exception("Raffle cancel manage callback failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to cancel raffle right now.", ephemeral=True)

    @discord.ui.button(label="Draw", style=discord.ButtonStyle.success)
    async def draw(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            if not await self._ensure_admin(interaction):
                return
            await interaction.followup.send(
                "Draw raffle now?",
                ephemeral=True,
                view=RaffleActionConfirmView(self, action="draw", owner_user_id=int(interaction.user.id)),
            )
        except Exception:
            log.exception("Raffle draw manage prompt failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to open draw confirmation right now.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)
            if not await self._ensure_admin(interaction):
                return
            await interaction.followup.send(
                "Cancel raffle?",
                ephemeral=True,
                view=RaffleActionConfirmView(self, action="cancel", owner_user_id=int(interaction.user.id)),
            )
        except Exception:
            log.exception("Raffle cancel manage prompt failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to open cancel confirmation right now.", ephemeral=True)

    @discord.ui.button(label="Verify Prize", style=discord.ButtonStyle.primary)
    async def verify_prize(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            if not await self._ensure_admin(interaction):
                return
            cog = interaction.client.get_cog("RafflesCog") if interaction.client else None
            if cog is None:
                await interaction.followup.send("Raffle system unavailable.", ephemeral=True)
                return
            await cog.handle_admin_prize_verification(interaction, self.raffle_id, AdminPrizeVerificationView(self.raffle_id))
        except Exception:
            log.exception("Raffle verify prize manage callback failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to verify prize right now.", ephemeral=True)

    @discord.ui.button(label="Recreate Canceled Raffle", style=discord.ButtonStyle.secondary)
    async def recreate_cancelled(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            if not await self._ensure_admin(interaction):
                return
            repo = RafflesRepository(get_pool())
            preview = await repo.get_recovery_preview(self.raffle_id)
            if not preview:
                await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
                return
            if preview.get("blocked_reason"):
                await interaction.followup.send(f"❌ {preview['blocked_reason']}", ephemeral=True)
                return
            raffle = preview["raffle"]
            channel_id, _key, _error = _resolve_purchase_panel_channel_id(
                await GuildSettingsRepository(get_database()).get_or_create(int(interaction.guild_id or 0)),
                is_giveaway=str(raffle.get("ticket_payment_type") or "").lower() == "free",
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Recreate canceled raffle?",
                    description=str(raffle.get("prize") or "Raffle"),
                    color=discord.Color.orange(),
                ).add_field(name="Restored entries", value=str(preview["restored_entry_count"]), inline=True)
                 .add_field(name="Restored tickets", value=str(preview["restored_ticket_count"]), inline=True)
                 .add_field(name="Repost channel", value=f"<#{channel_id}>" if channel_id else "Unresolved", inline=False),
                view=RaffleRecoveryConfirmView(self.raffle_id, channel_id),
                ephemeral=True,
            )
        except Exception:
            log.exception("Raffle recovery preview failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to open raffle recovery right now.", ephemeral=True)

    @discord.ui.button(label="Edit Raffle", style=discord.ButtonStyle.primary, row=1)
    async def edit_raffle(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True, thinking=False)
            repo = RafflesRepository(get_pool())
            raffle = await repo.get_raffle(self.raffle_id)
            if not raffle:
                await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
                return
            if not await self._ensure_can_edit(interaction, raffle):
                return
            await interaction.followup.send(
                "Edit raffle options:",
                view=RaffleEditView(self.raffle_id, int(interaction.user.id)),
                ephemeral=True,
            )
        except Exception:
            log.exception("Raffle edit view failed raffle_id=%s", self.raffle_id)
            await interaction.followup.send("Unable to open raffle editing right now.", ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


class RaffleAdminSelect(discord.ui.Select):
    def __init__(self, raffles: list[dict]):
        options = [
            discord.SelectOption(
                label=f"#{int(r['raffle_id'])} {str(r.get('prize') or 'Raffle')[:80]}",
                value=str(int(r['raffle_id'])),
                description=_compute_panel_tickets_value(r)[:100],
            )
            for r in raffles[:25]
        ]
        super().__init__(placeholder="Inspect raffle status", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, RaffleControlsView):
            await interaction.response.send_message("Raffle controls unavailable.", ephemeral=True)
            return
        await view.show_selected_raffle(interaction, int(self.values[0]))


class RaffleControlsView(discord.ui.View):
    def __init__(self, cog: "RafflesCog", user_id: int, raffles: list[dict]):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = int(user_id)
        if raffles:
            self.add_item(RaffleAdminSelect(raffles))

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the command user can use this panel.", ephemeral=True)
            return False
        if not await _is_raffle_admin(interaction):
            await interaction.response.send_message("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
            return False
        return True

    async def show_selected_raffle(self, interaction: discord.Interaction, raffle_id: int) -> None:
        if not await self._guard(interaction):
            return
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(int(raffle_id))
        if not raffle:
            await interaction.response.send_message("Raffle not found.", ephemeral=True)
            return
        embed = self.cog._build_raffle_controls_embed(raffle)
        await interaction.response.send_message(embed=embed, view=RaffleManageView(int(raffle_id)), ephemeral=True)


class RaffleEditCommentModal(discord.ui.Modal):
    comment = discord.ui.TextInput(
        label="Raffle Comment / Note",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, raffle_id: int, editor_user_id: int, current_comment: str | None):
        super().__init__(title="Edit Raffle Comment")
        self.raffle_id = int(raffle_id)
        self.editor_user_id = int(editor_user_id)
        self.comment.default = current_comment or ""

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            repo = RafflesRepository(get_pool())
            raffle = await repo.get_raffle(self.raffle_id)
            if not raffle:
                await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
                return
            if int(interaction.user.id) != self.editor_user_id or not await _can_manage_raffle_edit(interaction, raffle):
                await interaction.followup.send("Only the raffle host or a raffle admin can edit this raffle.", ephemeral=True)
                return
            comment = str(self.comment.value or "").strip() or None
            await repo.update_raffle_comment(self.raffle_id, comment)
            cog = interaction.client.get_cog("RafflesCog") if interaction.client else None
            if cog:
                await cog.refresh_raffle_public_panel(self.raffle_id)
            await interaction.followup.send("✅ Raffle comment updated.", ephemeral=True)
        except Exception:
            log.exception("Failed to update raffle comment raffle_id=%s", self.raffle_id)
            await interaction.followup.send("❌ Failed to update raffle comment. Please try again.", ephemeral=True)


class RaffleEditImageUrlModal(discord.ui.Modal):
    prize_image_url = discord.ui.TextInput(
        label="Prize Image URL",
        placeholder="https://i.imgur.com/example.png (Tip: use image2url.com to upload)",
        required=True,
        max_length=1000,
    )

    def __init__(self, raffle_id: int, editor_user_id: int, current_image_url: str | None):
        super().__init__(title="Add Photo")
        self.raffle_id = int(raffle_id)
        self.editor_user_id = int(editor_user_id)
        self.prize_image_url.default = current_image_url or ""

    @staticmethod
    def _is_valid_url(url: str) -> bool:
        normalized = url.strip().lower()
        if not (normalized.startswith("http://") or normalized.startswith("https://")):
            return False
        return normalized.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) or "imgur.com" in normalized

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            repo = RafflesRepository(get_pool())
            raffle = await repo.get_raffle(self.raffle_id)
            if not raffle:
                await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
                return
            if int(interaction.user.id) != self.editor_user_id or not await _can_manage_raffle_edit(interaction, raffle):
                await interaction.followup.send("Only the raffle host or a raffle admin can edit this raffle.", ephemeral=True)
                return
            if raffle.get("is_bundle"):
                await interaction.followup.send(
                    embed=create_error_embed("Not Allowed", "Bundle raffles do not support large prize images."),
                    ephemeral=True,
                )
                return
            image_url = str(self.prize_image_url.value or "").strip()
            if not self._is_valid_url(image_url):
                await interaction.followup.send(
                    "❌ Invalid image URL. Use http(s) and either an image extension or imgur.com URL.",
                    ephemeral=True,
                )
                return
            await repo.update_raffle_image(self.raffle_id, image_url)
            cog = interaction.client.get_cog("RafflesCog") if interaction.client else None
            if cog:
                await cog.refresh_raffle_public_panel(self.raffle_id)
            await interaction.followup.send("✅ Raffle photo updated.", ephemeral=True)
        except Exception:
            log.exception("Failed to update raffle image raffle_id=%s", self.raffle_id)
            await interaction.followup.send("❌ Failed to update raffle photo. Please try again.", ephemeral=True)


class RaffleEditPhotoPromptView(discord.ui.View):
    def __init__(self, raffle_id: int, editor_user_id: int, current_image_url: str | None):
        super().__init__(timeout=300)
        self.raffle_id = int(raffle_id)
        self.editor_user_id = int(editor_user_id)
        self.current_image_url = current_image_url or ""
        self.add_item(
            discord.ui.Button(
                label="Create img url",
                style=discord.ButtonStyle.link,
                url="https://www.image2url.com/",
                emoji="🔗",
            )
        )

    @discord.ui.button(label="Open image URL form", style=discord.ButtonStyle.primary)
    async def open_image_url_form(self, interaction: discord.Interaction, _: discord.ui.Button):
        if int(interaction.user.id) != self.editor_user_id:
            await interaction.response.send_message("Only the raffle host or a raffle admin can edit this raffle.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RaffleEditImageUrlModal(self.raffle_id, self.editor_user_id, self.current_image_url)
        )


class RaffleEditView(discord.ui.View):
    def __init__(self, raffle_id: int, owner_user_id: int):
        super().__init__(timeout=300)
        self.raffle_id = int(raffle_id)
        self.owner_user_id = int(owner_user_id)

    async def _load_raffle_for_editor(self, interaction: discord.Interaction) -> dict | None:
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(self.raffle_id)
        if not raffle:
            await interaction.followup.send("❌ Raffle not found.", ephemeral=True)
            return None
        if int(interaction.user.id) != self.owner_user_id or not await _can_manage_raffle_edit(interaction, raffle):
            await interaction.followup.send("Only the raffle host or a raffle admin can edit this raffle.", ephemeral=True)
            return None
        return raffle

    @discord.ui.button(label="Add Photo", style=discord.ButtonStyle.primary)
    async def add_photo(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=False)
        raffle = await self._load_raffle_for_editor(interaction)
        if not raffle:
            return
        await interaction.followup.send(
            "Use **Create img url** to upload and get a direct link, then click **Open image URL form**.",
            view=RaffleEditPhotoPromptView(
                self.raffle_id,
                self.owner_user_id,
                str(raffle.get("prize_image_url") or "").strip() or None,
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Edit Comment", style=discord.ButtonStyle.secondary)
    async def edit_comment(self, interaction: discord.Interaction, _: discord.ui.Button):
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(self.raffle_id)
        if not raffle:
            await interaction.response.send_message("❌ Raffle not found.", ephemeral=True)
            return
        if int(interaction.user.id) != self.owner_user_id or not await _can_manage_raffle_edit(interaction, raffle):
            await interaction.response.send_message("Only the raffle host or a raffle admin can edit this raffle.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RaffleEditCommentModal(
                raffle_id=self.raffle_id,
                editor_user_id=self.owner_user_id,
                current_comment=str(raffle.get("admin_comments") or "").strip() or None,
            )
        )

    @discord.ui.button(label="Refresh Active Raffles", style=discord.ButtonStyle.primary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        await self.cog._send_raffle_controls_panel(interaction, edit_existing=True)

    @discord.ui.button(label="View Active Raffles", style=discord.ButtonStyle.secondary, row=1)
    async def list_active(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        repo = RafflesRepository(get_pool())
        raffles = await repo.get_raffles_for_admin_controls(interaction.guild_id)
        embed = self.cog._build_raffle_controls_list_embed(raffles)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RaffleRecoveryConfirmView(discord.ui.View):
    def __init__(self, raffle_id: int, repost_channel_id: int | None):
        super().__init__(timeout=300)
        self.raffle_id = int(raffle_id)
        self.repost_channel_id = int(repost_channel_id) if repost_channel_id else None

    @discord.ui.button(label="Confirm Recreation", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await _is_raffle_admin(interaction):
            await interaction.followup.send("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
            return
        cog = interaction.client.get_cog("RafflesCog") if interaction.client else None
        if cog is None:
            await interaction.followup.send("Raffle system unavailable.", ephemeral=True)
            return
        await cog.recreate_cancelled_raffle(interaction, self.raffle_id, repost_channel_id=self.repost_channel_id)

    @discord.ui.button(label="Keep Cancelled", style=discord.ButtonStyle.secondary)
    async def keep_cancelled(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Cancelled raffle left unchanged.", ephemeral=True)

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
                creator_name = "User"
                creator_torn_id = 0
                raffle = await RafflesRepository(get_pool()).get_raffle(self.raffle_id)
                if raffle:
                    creator_discord_id = _safe_int(raffle.get("creator_discord_id")) or 0
                    identity_name, identity_torn_id, has_torn_id = await _resolve_torn_identity(creator_discord_id)
                    creator_torn_id = _safe_int(raffle.get("creator_torn_id")) or identity_torn_id
                    creator_name = identity_name or "User"
                    if not has_torn_id and not creator_torn_id:
                        creator_torn_id = 0
                fallback_error = (
                    f"Payment not found. Make sure you sent the items to {creator_name} [{creator_torn_id}] in Torn."
                    if creator_torn_id
                    else "Payment not found. Make sure you sent the items to User in Torn (Torn ID not configured)."
                )
                await interaction.followup.send(
                    f"❌ {error or fallback_error}",
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
            entry_details = await self.repo.get_entry_with_raffle(self.entry_id)
            if entry_details:
                interaction.client.dispatch(
                    "paid_raffle_purchase_verified",
                    {
                        "guild_id": int(entry_details.get("guild_id") or interaction.guild_id or 0),
                        "user_id": int(entry_details.get("discord_id") or interaction.user.id),
                        "raffle_id": int(entry_details.get("raffle_id") or self.raffle_id),
                        "entry_id": int(entry_details.get("entry_id") or self.entry_id),
                        "ticket_count": int(entry_details.get("num_tickets") or 0),
                        "payment_type": str(entry_details.get("ticket_payment_type") or ""),
                        "verified_at": datetime.now(timezone.utc),
                        "dedupe_key": f"raffle_purchase:{int(entry_details.get('entry_id') or self.entry_id)}",
                    },
                )
            await _update_raffle_purchase_panel_counts(interaction.client, self.repo, self.raffle_id)
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
        placeholder="https://i.imgur.com/example.png (Tip: use image2url.com to upload)",
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if interaction.user.id != self.prompt_view.creator_discord_id:
                await interaction.followup.send(
                    "❌ Only the raffle creator can set the prize image.",
                    ephemeral=True,
                )
                return
            raffle = await self.prompt_view.repo.get_raffle(self.prompt_view.raffle_id)
            if raffle and raffle.get("is_bundle"):
                await interaction.followup.send(
                    embed=create_error_embed("Not Allowed", "Bundle raffles do not support large prize images."),
                    ephemeral=True,
                )
                return
            image_url = str(self.prize_image_url.value).strip()
            if not self._is_valid_url(image_url):
                await interaction.followup.send(
                    "❌ Invalid image URL. Use http(s) and either an image extension or imgur.com URL.",
                    ephemeral=True,
                )
                return
            await self.prompt_view.repo.set_prize_image_url(self.prompt_view.raffle_id, image_url)
            await self.prompt_view.update_purchase_panel_image(image_url)
            await interaction.followup.send("✅ Prize image added", ephemeral=True)
        except Exception:
            log.exception("Failed to handle raffle prize image modal submission raffle_id=%s", self.prompt_view.raffle_id)
            await interaction.followup.send("❌ Failed to set prize image. Please try again.", ephemeral=True)


class RafflePrizeImageHelpView(discord.ui.View):
    """Helper view with upload link and modal opener for prize image setup."""

    def __init__(self, prompt_view: "RafflePrizeImagePromptView"):
        super().__init__(timeout=300)
        self.prompt_view = prompt_view
        self.add_item(
            discord.ui.Button(
                label="Create img url",
                style=discord.ButtonStyle.link,
                url="https://www.image2url.com/",
                emoji="🔗",
            )
        )

    @discord.ui.button(label="Open image URL form", style=discord.ButtonStyle.primary)
    async def open_image_url_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.prompt_view.creator_discord_id:
                await interaction.response.send_message(
                    "❌ Only the raffle creator can set the prize image.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(RafflePrizeImageUrlModal(self.prompt_view))
        except Exception:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Failed to open prize image form. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Failed to open prize image form. Please try again.",
                    ephemeral=True,
                )


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
            await PANEL_EDIT_SAFETY.request_edit(
                panel_message,
                embed=embed,
                view=RafflePurchasePanelView(raffle_id=self.raffle_id, allow_prize_token_purchase=False),
                min_interval_seconds=5,
                force=True,
            )
        except Exception:
            log.exception("Failed to update purchase panel image for raffle %s", self.raffle_id)
    @discord.ui.button(label="📷 Add Prize Image (optional)", style=discord.ButtonStyle.primary)
    async def add_prize_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.creator_discord_id:
                await interaction.response.send_message(
                    "❌ Only the raffle creator can set the prize image.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Use **Create img url** to upload and get a direct link, then click **Open image URL form**.",
                view=RafflePrizeImageHelpView(self),
                ephemeral=True,
            )
        except Exception:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Failed to open prize image form. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Failed to open prize image form. Please try again.",
                    ephemeral=True,
                )
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
    raffle = app_commands.Group(name="raffle", description="Raffle admin controls")

    """Raffle commands with sell-out trigger support."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pack_drafts: dict[int, tuple[dict, datetime]] = {}
        self._raffle_create_drafts: dict[int, tuple[dict, datetime]] = {}
        self._payment_meta_cache: dict[str, dict | None] = {}
        self._workers_started = False
        self._db_wait_logged = False
        self._post_ready_initialized = False

    def cog_unload(self):
        if self.check_raffles.is_running():
            self.check_raffles.cancel()
        if self.cleanup_expired.is_running():
            self.cleanup_expired.cancel()
        if self.auto_verify_payments.is_running():
            self.auto_verify_payments.cancel()
        if self.cleanup_closed_panels.is_running():
            self.cleanup_closed_panels.cancel()

    async def cog_load(self):
        return

    def _start_workers(self) -> None:
        if self._workers_started:
            return
        if not db_is_initialized():
            return
        if not self.check_raffles.is_running():
            self.check_raffles.start()
        if not self.cleanup_expired.is_running():
            self.cleanup_expired.start()
        if not self.auto_verify_payments.is_running():
            self.auto_verify_payments.start()
        if not self.cleanup_closed_panels.is_running():
            self.cleanup_closed_panels.start()
        self._workers_started = True

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await wait_until_initialized(timeout=30.0)
        if not self._post_ready_initialized:
            await self._post_ready_init()
            self._post_ready_initialized = True
        self._start_workers()

    async def _post_ready_init(self):
        """Register persistent raffle purchase views for existing panel messages."""
        try:
            db = get_database()
            if not db.pool:
                log.error("Failed registering persistent raffle views: database pool is not initialized")
                return
            try:
                repo = RafflesRepository(db.pool)
                panel_raffles = await repo.get_active_raffles_with_panels()
                for raffle in panel_raffles:
                    self.bot.add_view(
                        RafflePurchasePanelView(raffle_id=int(raffle["raffle_id"]), allow_prize_token_purchase=bool(raffle.get("allow_prize_token_purchase"))),
                        message_id=int(raffle["purchase_panel_message_id"]),
                    )
                active_raffles = await repo.get_all_active_raffle_ids()
                pending_dm = await repo.get_pending_prize_confirm_dm_rows()
                panel_raffle_ids = {int(r["raffle_id"]) for r in panel_raffles}
                for raffle_id in active_raffles:
                    if raffle_id in panel_raffle_ids:
                        continue
                    self.bot.add_view(RafflePurchasePanelView(raffle_id=raffle_id, allow_prize_token_purchase=False))
                for dm_row in pending_dm:
                    self.bot.add_view(
                        RafflePrizeConfirmDMView(
                            raffle_id=int(dm_row["raffle_id"]),
                            creator_discord_id=int(dm_row["creator_discord_id"]),
                        ),
                        message_id=int(dm_row["prize_confirm_dm_message_id"]),
                    )
                pending_verify = await repo.get_pending_prize_verification_rows()
                for verify_row in pending_verify:
                    self.bot.add_view(
                        AdminPrizeVerificationView(raffle_id=int(verify_row["raffle_id"])),
                        message_id=int(verify_row["announcement_message_id"]),
                    )
                if panel_raffles or active_raffles or pending_dm or pending_verify:
                    log.info(
                        "Registered %s message-bound and %s fallback raffle purchase views",
                        len(panel_raffles),
                        max(len(active_raffles) - len(panel_raffle_ids), 0),
                    )
            except Exception:
                log.exception("Failed registering persistent raffle views")
        except Exception:
            log.exception("Failed during post-ready raffle initialization")

    async def _ensure_db_ready(self, worker_name: str) -> bool:
        if db_is_initialized():
            self._db_wait_logged = False
            return True
        if not self._db_wait_logged:
            log.debug("%s waiting for database pool initialization", worker_name)
            self._db_wait_logged = True
        return False

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
    def store_create_draft(self, creator_discord_id: int, draft: dict) -> None:
        self._raffle_create_drafts[creator_discord_id] = (draft, datetime.utcnow() + timedelta(minutes=10))
    def get_create_draft(self, creator_discord_id: int) -> dict | None:
        entry = self._raffle_create_drafts.get(creator_discord_id)
        if not entry:
            return None
        draft, expires_at = entry
        if datetime.utcnow() > expires_at:
            self._raffle_create_drafts.pop(creator_discord_id, None)
            return None
        return draft
    def pop_create_draft(self, creator_discord_id: int) -> None:
        self._raffle_create_drafts.pop(creator_discord_id, None)
    def _is_admin_raffle(self, raffle: dict) -> bool:
        return str(raffle.get("ticket_payment_type") or "").lower() != "free" and not bool(raffle.get("is_free"))

    async def _handle_raffle_completed(self, raffle: dict, winner: dict) -> None:
        raffle_id = int(raffle.get("raffle_id") or 0)
        guild_id = int(raffle.get("guild_id") or 0)
        log.info("Raffle completion side effects running raffle_id=%s guild_id=%s", raffle_id, guild_id)

        await self._disable_raffle_panels(raffle, status_text="Raffle completed.")
        await self._send_prize_delivery_dm(raffle)

        admin_announcement_attempted = self._is_admin_raffle(raffle)
        log.info("Raffle completion admin announcement attempted=%s raffle_id=%s", admin_announcement_attempted, raffle_id)
        await self._send_admin_winner_announcement(raffle, winner)

        verification_cog = self.bot.get_cog("RaffleVerificationCog")
        if verification_cog:
            await verification_cog.send_winner_notification(winner)

        guild = self.bot.get_guild(guild_id)
        if not guild or not guild.system_channel:
            log.info("Raffle completion public fallback skipped raffle_id=%s reason=no_system_channel", raffle_id)
            return

        winner_discord_id = int(winner.get("discord_id") or raffle.get("winner_discord_id") or 0)
        prize_text = str(winner.get("prize") or raffle.get("prize") or "Unknown")
        total_entries = int(winner.get("total_entries") or 0)
        embed = discord.Embed(
            title="🎉 RAFFLE WINNER!",
            description=(
                f"🎁 **{prize_text}**\n\n"
                f"🏆 Winner: <@{winner_discord_id}>\n"
                f"🎟️ Total Entries: {total_entries}"
            ),
            color=discord.Color.gold(),
        )
        try:
            await guild.system_channel.send(embed=embed)
            log.info("Raffle completion public fallback posted raffle_id=%s channel_id=%s", raffle_id, guild.system_channel.id)
        except Exception:
            log.warning(
                "Raffle completion public fallback failed raffle_id=%s channel_id=%s",
                raffle_id,
                guild.system_channel.id,
                exc_info=True,
            )

    async def _send_admin_winner_announcement(self, raffle: dict, winner: dict) -> None:
        if not self._is_admin_raffle(raffle):
            return

        guild = self.bot.get_guild(int(raffle["guild_id"]))
        if guild is None:
            return

        db = get_database()
        settings = await GuildSettingsRepository(db).get_or_create(guild.id)
        announce_channel_id = _safe_int(settings.get("raffle_announcement_channel_id")) or _safe_int(raffle.get("announcement_channel_id"))
        if not announce_channel_id:
            return

        channel = guild.get_channel(int(announce_channel_id))
        if channel is None:
            try:
                channel = await guild.fetch_channel(int(announce_channel_id))
            except Exception:
                channel = None
        if not channel or not hasattr(channel, "send"):
            return

        repo = RafflesRepository(get_pool())
        updated_raffle = await repo.get_raffle(int(raffle["raffle_id"])) or raffle
        entries = await repo.get_raffle_entries(int(raffle["raffle_id"]))
        total_entries = sum(int(e.get("num_tickets") or 0) for e in entries if bool(e.get("payment_verified")))
        winner_discord_id = int(updated_raffle.get("winner_discord_id") or winner.get("discord_id"))
        winner_member = guild.get_member(winner_discord_id)
        winner_display = winner_member.display_name if winner_member else f"User {winner_discord_id}"
        prize_items = await repo.get_prize_items_payload(updated_raffle)
        if prize_items:
            item_repo = TornItemsRepository(get_pool())
            parts = []
            for item in prize_items:
                item_id = int(item["item_id"])
                item_name = await _resolve_item_name(item_repo, item_id)
                parts.append(f"{int(item['qty'])} × {item_name}")
            prize_text = "\n".join(parts)
        else:
            prize_text = str(updated_raffle.get("prize") or "Unknown")
        draw_time = updated_raffle.get("drawn_at")
        draw_ts = _format_discord_timestamp(draw_time)

        admin_roles = GuildSettingsRepository.resolve_admin_role_ids(settings)
        admin_mention = " ".join(f"<@&{rid}>" for rid in admin_roles) if admin_roles else "@admins"

        embed = discord.Embed(
            title="🏆 Raffle Winner Announcement",
            color=discord.Color.gold(),
            description=(
                f"**Raffle:** {updated_raffle.get('prize')} (#{int(updated_raffle['raffle_id'])})\n"
                f"**Prize:** {prize_text}\n"
                f"**Winner:** {winner_display} (`{winner_discord_id}`)\n"
                f"**Entries:** {total_entries}\n"
                f"**Drawn:** {draw_ts}"
            ),
        )

        content = f"<@{winner_discord_id}> {admin_mention}"
        verify_view = AdminPrizeVerificationView(int(updated_raffle["raffle_id"]), verified=str(updated_raffle.get("prize_verification_status") or "").upper() == "VERIFIED")

        announcement_message = None
        existing_channel_id = _safe_int(updated_raffle.get("announcement_channel_id"))
        existing_message_id = _safe_int(updated_raffle.get("announcement_message_id"))
        if existing_channel_id and existing_message_id:
            existing_channel = guild.get_channel(existing_channel_id)
            if existing_channel is None:
                try:
                    existing_channel = await guild.fetch_channel(existing_channel_id)
                except Exception:
                    existing_channel = None
            if existing_channel and hasattr(existing_channel, "fetch_message"):
                try:
                    message = await existing_channel.fetch_message(existing_message_id)
                    await message.edit(content=content, embed=embed, view=verify_view)
                    announcement_message = message
                except Exception:
                    announcement_message = None

        if announcement_message is None:
            announcement_message = await channel.send(content=content, embed=embed, view=verify_view)

        await repo.set_announcement_ref(int(updated_raffle["raffle_id"]), int(announcement_message.channel.id), int(announcement_message.id))

    async def handle_admin_prize_verification(self, interaction: discord.Interaction, raffle_id: int, view: AdminPrizeVerificationView) -> None:
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(int(raffle_id))
        if not raffle or not self._is_admin_raffle(raffle):
            await interaction.followup.send("This verification is only available for admin raffles.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Only admins can verify prizes.", ephemeral=True)
            return

        settings = await GuildSettingsRepository(get_database()).get_or_create(guild.id)
        if not can_manage_paid_raffles(interaction.user, settings):
            await interaction.followup.send("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
            return

        if str(raffle.get("prize_verification_status") or "").upper() == "VERIFIED":
            await interaction.followup.send("Already verified.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        checked_at = raffle.get("prize_verification_checked_at")
        if checked_at and checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        if checked_at and (now - checked_at).total_seconds() < _PRIZE_VERIFY_COOLDOWN_SECONDS:
            await interaction.followup.send("Verification was attempted recently. Try again in a minute.", ephemeral=True)
            return

        await repo.update_prize_verification(int(raffle_id), status="CHECKING", checked_at=now)

        users_repo = UsersRepository(get_pool())
        winner_discord_id = _safe_int(raffle.get("winner_discord_id")) or 0
        winner_api = await users_repo.get_user_api_key(winner_discord_id)
        encrypted_key = (winner_api or {}).get("encrypted_key") or (winner_api or {}).get("api_key_encrypted")
        if not encrypted_key:
            await repo.update_prize_verification(int(raffle_id), status="ERROR", checked_at=now)
            await interaction.followup.send("Winner has no linked Torn API. Cannot verify automatically.", ephemeral=True)
            await self._publish_verification_update(raffle, "ERROR", "Winner has no linked Torn API. Cannot verify automatically.", view=view)
            return

        security = get_security_manager()
        torn_api = get_torn_api()
        try:
            api_key = security.decrypt_api_key(encrypted_key)
            logs = await torn_api.get_item_send_receive_logs(
                api_key,
                limit=100,
                audit_discord_id=int(winner_discord_id),
                audit_torn_id=int((winner_api or {}).get("torn_user_id") or 0) or None,
                audit_context="payment_verify_logs",
                audit_query_meta={"cat": 85, "limit": 100},
            )
        except Exception as exc:
            summary = f"Torn log lookup failed ({type(exc).__name__})."
            await repo.update_prize_verification(int(raffle_id), status="ERROR", checked_at=now)
            await self._publish_verification_update(raffle, "ERROR", summary, view=view)
            await interaction.followup.send("Verification failed due to Torn API error.", ephemeral=True)
            return

        prize_items = await repo.get_prize_items_payload(raffle)
        expected_map = _extract_prize_map(prize_items)
        drawn_at = raffle.get("drawn_at") or raffle.get("tickets_fully_sold_at") or raffle.get("created_at")
        since_ts = int(drawn_at.replace(tzinfo=timezone.utc).timestamp()) if drawn_at and drawn_at.tzinfo is None else int(drawn_at.timestamp()) if drawn_at else 0
        max_ts = since_ts + _PRIZE_VERIFY_WINDOW_SECONDS if since_ts else 0

        candidates = []
        for entry in logs:
            data = entry.get("data") or {}
            ts = _safe_int(entry.get("timestamp")) or 0
            if since_ts and ts < since_ts:
                continue
            if max_ts and ts > max_ts:
                continue
            if expected_map:
                found: dict[int, int] = {}
                for item in data.get("items") or []:
                    iid = _safe_int(item.get("id"))
                    qty = _safe_int(item.get("qty")) or 0
                    if iid:
                        found[iid] = found.get(iid, 0) + qty
                if found != expected_map:
                    continue
            candidates.append(entry)

        matched = None
        if candidates:
            candidates.sort(key=lambda e: (_safe_int(e.get("timestamp")) or 0) - since_ts)
            matched = candidates[0]

        if not matched:
            checked_logs = len(logs)
            not_found = f"Checked last {checked_logs} logs, no matching item send found for {raffle.get('prize')} after {_format_discord_timestamp(drawn_at)}."
            await repo.update_prize_verification(int(raffle_id), status="NOT_FOUND", checked_at=now)
            await self._publish_verification_update(raffle, "NOT_FOUND", not_found, view=view)
            await interaction.followup.send("Verification complete: no matching log found.", ephemeral=True)
            return

        matched_ts = _safe_int(matched.get("timestamp")) or int(now.timestamp())
        matched_data = matched.get("data") or {}
        sender_id = _safe_int(matched_data.get("sender"))
        log_id = _extract_log_id(matched)
        safe_details = f"Verified ✅\nLog: `{log_id}` at <t:{matched_ts}:F>\nSender Torn ID: {sender_id or 'Unknown'}"
        await repo.update_prize_verification(
            int(raffle_id),
            status="VERIFIED",
            checked_at=now,
            verified_at=now,
            verified_by_discord_id=interaction.user.id,
            verification_log_id=log_id,
        )
        await self._publish_verification_update(raffle, "VERIFIED", safe_details, view=view, matched=matched)
        await interaction.followup.send("✅ Prize verified.", ephemeral=True)

    async def _publish_verification_update(self, raffle: dict, status: str, summary: str, *, view: AdminPrizeVerificationView, matched: dict | None = None) -> None:
        guild = self.bot.get_guild(int(raffle["guild_id"]))
        if guild is None:
            return
        repo = RafflesRepository(get_pool())
        refreshed = await repo.get_raffle(int(raffle["raffle_id"])) or raffle

        channel_id = _safe_int(refreshed.get("announcement_channel_id"))
        message_id = _safe_int(refreshed.get("announcement_message_id"))
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None and channel_id:
            try:
                channel = await guild.fetch_channel(channel_id)
            except Exception:
                channel = None

        message = None
        if channel and message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                message = None

        if message is None:
            fallback_channel_id = _safe_int(refreshed.get("announcement_channel_id"))
            if fallback_channel_id:
                fallback = guild.get_channel(fallback_channel_id)
                if fallback is None:
                    try:
                        fallback = await guild.fetch_channel(fallback_channel_id)
                    except Exception:
                        fallback = None
                if fallback and hasattr(fallback, "send"):
                    message = await fallback.send(f"Raffle #{int(refreshed['raffle_id'])} verification: {summary}")
                    await repo.set_announcement_ref(int(refreshed["raffle_id"]), int(message.channel.id), int(message.id))
                    return
            return

        embed = message.embeds[0].copy() if message.embeds else discord.Embed(title="Raffle Winner Announcement", color=discord.Color.gold())
        value = summary
        if matched:
            data = matched.get("data") or {}
            items = []
            for item in data.get("items") or []:
                iid = _safe_int(item.get("id"))
                qty = _safe_int(item.get("qty")) or 0
                if iid:
                    item_name = await _resolve_item_name(TornItemsRepository(get_pool()), iid)
                    items.append(f"- {qty} × {item_name} ({iid})")
            if items:
                value = f"{summary}\nItems:\n" + "\n".join(items)
        embed.add_field(name=f"Verification: {status}", value=value[:1024], inline=False)
        verified = status == "VERIFIED"
        await message.edit(embed=embed, view=AdminPrizeVerificationView(int(refreshed["raffle_id"]), verified=verified))

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
    async def prompt_prize_token_step(
        self,
        interaction: discord.Interaction,
        draft: dict,
        *,
        is_bundle: bool,
        bundle_text: str | None,
        bundle_entries: list[dict],
    ) -> None:
        if str(draft.get("ticket_payment_type") or "").lower() == "free":
            draft["allow_prize_token_purchase"] = False
            draft["prize_token_cost_per_ticket"] = None
            await self.create_raffle_from_draft(interaction, draft, is_bundle=is_bundle, bundle_text=bundle_text, bundle_entries=bundle_entries)
            return
        self.store_create_draft(int(interaction.user.id), draft)
        await interaction.response.send_message(
            "Allow Prize Token ticket purchases?",
            ephemeral=True,
            view=RafflePrizeTokenChoiceView(self.bot, int(interaction.user.id), is_bundle, bundle_text, bundle_entries),
        )

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
                admin_comments=draft.get("admin_comments"),
                is_bundle=is_bundle,
                bundle_text=bundle_text,
                allow_prize_token_purchase=bool(draft.get("allow_prize_token_purchase", False)),
                prize_token_cost_per_ticket=draft.get("prize_token_cost_per_ticket"),
            )
            db = get_database()
            settings_repo = GuildSettingsRepository(db)
            settings = await settings_repo.get_or_create(interaction.guild_id)
            is_giveaway = str(draft["ticket_payment_type"] or "").lower() == "free"
            purchase_channel_id, purchase_channel_key, purchase_channel_error = _resolve_purchase_panel_channel_id(
                settings,
                is_giveaway=is_giveaway,
            )
            if not purchase_channel_id:
                await interaction.response.send_message(purchase_channel_error or "❌ Unable to resolve raffle purchase panel channel.", ephemeral=True)
                return
            log.info(
                "Resolved raffle purchase panel channel raffle_id=%s is_giveaway=%s chosen_channel_id=%s settings_key=%s",
                raffle_id,
                is_giveaway,
                purchase_channel_id,
                purchase_channel_key,
            )
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
            created_raffle = await repo.get_raffle(raffle_id)
            if not created_raffle:
                raise RuntimeError("Failed to load created raffle")
            purchase_panel_embed = _build_paid_raffle_panel_embed(created_raffle)
            prize_thumbnail_url = await self._get_single_prize_thumbnail_url(draft["prize"], is_bundle)
            if prize_thumbnail_url:
                purchase_panel_embed.set_thumbnail(url=prize_thumbnail_url)
            single_item_meta = draft.get("single_item_meta")
            panel_message = await purchase_channel.send(
                embed=purchase_panel_embed,
                view=RafflePurchasePanelView(
                    raffle_id=raffle_id,
                    allow_prize_token_purchase=bool(created_raffle.get("allow_prize_token_purchase")),
                ),
            )
            await repo.set_purchase_panel_ref(raffle_id=raffle_id, channel_id=purchase_channel.id, message_id=panel_message.id)
            if is_bundle and bundle_entries:
                bundle_file_io = await self._build_bundle_file(bundle_entries)
                if bundle_file_io:
                    bundle_file = discord.File(fp=bundle_file_io, filename="bundle.png")
                    bundle_embed = discord.Embed(title="Pack Contents", description=bundle_text or "", color=discord.Color.blurple())
                    bundle_embed.set_image(url="attachment://bundle.png")
                    await purchase_channel.send(embed=bundle_embed, file=bundle_file)
            tickets_display = "Unlimited" if int(draft.get("tickets_available") or 0) == 0 else str(draft["tickets_available"])
            price_text = _payment_text(draft["ticket_payment_type"], draft["ticket_price"])
            announce_embed = discord.Embed(
                title="🎉 New Raffle Created!",
                description=f"🎁 **Prize:** {draft['prize']}\n🎟️ **Tickets:** {tickets_display} available\n💰 **Price:** {price_text} per ticket\n📋 **Max per user:** {'Unlimited' if draft['max_tickets_per_user'] == 0 else draft['max_tickets_per_user']}",
                color=discord.Color.green(),
            )
            if draft.get("admin_comments"):
                announce_embed.add_field(name="📌 Notes", value=str(draft["admin_comments"])[:1024], inline=False)
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
                        try:
                            announce_message = await announce_channel.send(embed=announce_embed)
                            await repo.set_announcement_ref(
                                raffle_id=raffle_id,
                                channel_id=announce_channel.id,
                                message_id=announce_message.id,
                            )
                        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                            log.warning("Failed to send raffle announcement raffle_id=%s channel_id=%s", raffle_id, announce_channel_id, exc_info=True)
            response_text = f"✅ Raffle created. {'Giveaway' if is_giveaway else 'Purchase'} panel posted in {purchase_channel.mention}.\n{panel_message.jump_url}"
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

    async def _post_raffle_purchase_panel(self, guild: discord.Guild | None, raffle: dict) -> tuple[discord.abc.Messageable | None, discord.Message | None]:
        settings = await GuildSettingsRepository(get_database()).get_or_create(int(raffle["guild_id"]))
        is_giveaway = str(raffle.get("ticket_payment_type") or "").lower() == "free"
        purchase_channel_id, _key, _error = _resolve_purchase_panel_channel_id(settings, is_giveaway=is_giveaway)
        if not purchase_channel_id or guild is None:
            return None, None
        purchase_channel = guild.get_channel(int(purchase_channel_id))
        if purchase_channel is None:
            try:
                fetched = await guild.fetch_channel(int(purchase_channel_id))
                if hasattr(fetched, "send"):
                    purchase_channel = fetched
            except Exception:
                purchase_channel = None
        if purchase_channel is None:
            return None, None
        embed = _build_paid_raffle_panel_embed(raffle)
        panel_message = await purchase_channel.send(
            embed=embed,
            view=RafflePurchasePanelView(
                raffle_id=int(raffle["raffle_id"]),
                allow_prize_token_purchase=bool(raffle.get("allow_prize_token_purchase")),
            ),
        )
        await RafflesRepository(get_pool()).set_purchase_panel_ref(
            raffle_id=int(raffle["raffle_id"]),
            channel_id=int(purchase_channel.id),
            message_id=int(panel_message.id),
        )
        return purchase_channel, panel_message

    async def recreate_cancelled_raffle(
        self,
        interaction: discord.Interaction,
        raffle_id: int,
        *,
        repost_channel_id: int | None = None,
    ) -> None:
        repo = RafflesRepository(get_pool())
        try:
            result = await repo.recreate_cancelled_raffle(raffle_id)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("Raffle recovery failed raffle_id=%s", raffle_id)
            await interaction.followup.send(
                f"❌ Failed to recreate raffle: {_format_admin_safe_error(exc)}",
                ephemeral=True,
            )
            return

        new_raffle = await repo.get_raffle(int(result["new_raffle_id"]))
        if not new_raffle:
            await interaction.followup.send("❌ Recreation completed but the new raffle could not be loaded.", ephemeral=True)
            return
        guild = interaction.guild
        channel = None
        panel_message = None
        if repost_channel_id and guild is not None:
            channel = guild.get_channel(int(repost_channel_id))
            if channel and hasattr(channel, "send"):
                embed = _build_paid_raffle_panel_embed(new_raffle)
                panel_message = await channel.send(
                    embed=embed,
                    view=RafflePurchasePanelView(
                        raffle_id=int(new_raffle["raffle_id"]),
                        allow_prize_token_purchase=bool(new_raffle.get("allow_prize_token_purchase")),
                    ),
                )
                await repo.set_purchase_panel_ref(int(new_raffle["raffle_id"]), int(channel.id), int(panel_message.id))
        if panel_message is None:
            channel, panel_message = await self._post_raffle_purchase_panel(guild, new_raffle)

        await interaction.followup.send(
            (
                "✅ Cancelled raffle recreated.\n"
                f"Old raffle id: **{result['old_raffle_id']}**\n"
                f"New raffle id: **{result['new_raffle_id']}**\n"
                f"Restored entry count: **{result['restored_entry_count']}**\n"
                f"Restored ticket count: **{result['restored_ticket_count']}**\n"
                f"Repost channel: {channel.mention if channel else ('<#{repost_channel_id}>' if repost_channel_id else 'Unavailable')}"
            ),
            ephemeral=True,
        )

    async def _disable_message_buttons(self, channel_id: int | None, message_id: int | None, view: discord.ui.View | None = None, status_text: str | None = None):
        if not channel_id or not message_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except Exception:
                return
        try:
            message = await channel.fetch_message(int(message_id))
        except Exception:
            return

        if view is None:
            try:
                view = discord.ui.View.from_message(message, timeout=None)
            except Exception:
                view = None
        new_view = _disable_view(view)

        embed = message.embeds[0].copy() if message.embeds else None
        if embed and status_text:
            embed.description = f"{embed.description or ''}\n\n⚠️ {status_text}".strip()
            title = embed.title or "Raffle"
            if "closed" not in title.lower() and "cancel" not in title.lower() and "complete" not in title.lower():
                embed.title = f"{title} (Closed)"
        try:
            await PANEL_EDIT_SAFETY.request_edit(
                message,
                embed=embed,
                view=new_view,
                min_interval_seconds=5,
                force=True,
            )
        except Exception:
            pass

    async def _disable_raffle_panels(self, raffle: dict, *, status_text: str):
        panel_channel_id = raffle.get("purchase_channel_id") or raffle.get("purchase_panel_channel_id")
        panel_message_id = raffle.get("purchase_message_id") or raffle.get("purchase_panel_message_id")
        await self._disable_message_buttons(
            panel_channel_id,
            panel_message_id,
            view=RafflePurchasePanelView(int(raffle["raffle_id"]), allow_prize_token_purchase=bool(raffle.get("allow_prize_token_purchase"))),
            status_text=status_text,
        )
        await self._disable_message_buttons(
            raffle.get("announcement_channel_id"),
            raffle.get("announcement_message_id"),
            status_text=status_text,
        )

    async def _disable_prize_dm_confirm(self, raffle: dict):
        dm_channel_id = raffle.get("prize_confirm_dm_channel_id")
        dm_message_id = raffle.get("prize_confirm_dm_message_id")
        if not dm_channel_id or not dm_message_id:
            return
        try:
            channel = await self.bot.fetch_channel(int(dm_channel_id))
            msg = await channel.fetch_message(int(dm_message_id))
            view = RafflePrizeConfirmDMView(int(raffle["raffle_id"]), int(raffle["creator_discord_id"]), disabled=True)
            _disable_view(view)
            await msg.edit(view=view)
        except Exception:
            pass

    async def _send_prize_delivery_dm(self, raffle: dict):
        repo = RafflesRepository(get_pool())
        instructions, _, verifiable = await _build_prize_instruction_text(repo, raffle)
        winner_discord_id = int(raffle.get("winner_discord_id") or 0)
        winner_torn_id = _safe_int(raffle.get("winner_torn_id"))
        details = [
            f"Raffle: {raffle.get('prize')} (ID #{int(raffle['raffle_id'])})",
            f"Winner: <@{winner_discord_id}>",
        ]
        if winner_torn_id:
            details.append(f"Winner Torn ID: {winner_torn_id}")
        details.append(f"What to send:\n{instructions}")
        details.append("After sending in Torn, press ✅ Confirm Prize Sent.")
        details.append("If you send a different item/qty, auto verification will fail.")
        embed = discord.Embed(
            title="Prize Delivery",
            description="\n\n".join(details),
            color=discord.Color.gold(),
        )

        creator_id = int(raffle["creator_discord_id"])
        view = None
        if verifiable:
            view = RafflePrizeConfirmDMView(int(raffle["raffle_id"]), creator_id)

        try:
            user = await self.bot.fetch_user(creator_id)
            dm = await user.send(embed=embed, view=view)
            await repo.set_prize_confirm_dm_ref(int(raffle["raffle_id"]), int(dm.channel.id), int(dm.id))
        except discord.Forbidden:
            channel_id = raffle.get("announcement_channel_id")
            if channel_id:
                channel = self.bot.get_channel(int(channel_id))
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(int(channel_id))
                    except Exception:
                        channel = None
                if channel and hasattr(channel, "send"):
                    await channel.send(f"<@{creator_id}> I couldn't DM you. Please enable DMs and rerun the confirm step.")

    def _build_raffle_controls_list_embed(self, raffles: list[dict]) -> discord.Embed:
        if not raffles:
            return discord.Embed(title="Raffle Controls", description="No active or cancelled raffles.", color=discord.Color.blue())
        embed = discord.Embed(title="Raffle Controls", description="Select a raffle below or use the management buttons after inspecting one.", color=discord.Color.blue())
        for raffle in raffles[:10]:
            tickets_total = "Unlimited" if int(raffle.get("tickets_available") or 0) == 0 else str(raffle["tickets_available"])
            embed.add_field(
                name=f"#{raffle['raffle_id']}: {str(raffle.get('prize') or 'Raffle')[:60]}",
                value=f"Status: **{raffle.get('status') or 'unknown'}**\n{_compute_panel_tickets_value(raffle)}\nPrice: {_payment_text(str(raffle.get('ticket_payment_type') or ''), int(raffle.get('ticket_price') or 0))}",
                inline=False,
            )
        return embed

    def _build_raffle_controls_embed(self, raffle: dict) -> discord.Embed:
        tickets_total = "Unlimited" if int(raffle.get("tickets_available") or 0) == 0 else str(raffle.get("tickets_available"))
        draw_text = "30 seconds after sellout" if raffle.get("end_trigger") == "tickets_sold" else (discord.utils.format_dt(raffle["end_time"], style="R") if raffle.get("end_time") else "Manual/end-time draw")
        embed = discord.Embed(title=f"Raffle #{int(raffle['raffle_id'])} Controls", description=str(raffle.get('prize') or 'Raffle'), color=discord.Color.blurple())
        embed.add_field(name="Status", value=str(raffle.get("status") or "unknown"), inline=True)
        embed.add_field(name="Tickets", value=f"{int(raffle.get('tickets_sold') or 0)}/{tickets_total}", inline=True)
        embed.add_field(name="Payment", value=_payment_text(str(raffle.get('ticket_payment_type') or ''), int(raffle.get('ticket_price') or 0)), inline=True)
        embed.add_field(name="Draw", value=draw_text, inline=False)
        return embed

    async def refresh_raffle_public_panel(self, raffle_id: int) -> None:
        repo = RafflesRepository(get_pool())
        raffle = await repo.get_raffle(int(raffle_id))
        if not raffle:
            return
        await _update_raffle_purchase_panel_counts(self.bot, repo, int(raffle_id))

    async def _send_raffle_controls_panel(self, interaction: discord.Interaction, *, edit_existing: bool = False) -> None:
        repo = RafflesRepository(get_pool())
        raffles = await repo.get_raffles_for_admin_controls(interaction.guild_id)
        embed = self._build_raffle_controls_list_embed(raffles)
        view = RaffleControlsView(self, interaction.user.id, raffles)
        if edit_existing:
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
            return
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # SINGLE ADMIN-ONLY CREATE COMMAND WITH EMOJIS IN CHOICES
    @app_commands.command(name="raffle_create", description="🎉 Create a new raffle (Admin only)")
    async def raffle_create(self, interaction: discord.Interaction):
        """🎉 Create a raffle - Admin only."""
        settings = await GuildSettingsRepository(get_database()).get_or_create(int(interaction.guild_id or 0))
        if not isinstance(interaction.user, discord.Member) or not can_manage_paid_raffles(interaction.user, settings):
            log.info("Paid raffle create denied user_id=%s guild_id=%s", interaction.user.id, interaction.guild_id)
            await interaction.response.send_message("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
            return
        await interaction.response.send_modal(RaffleCreateModal(self.bot))
    @raffle.command(name="controls", description="Open raffle admin controls")
    async def raffle_controls(self, interaction: discord.Interaction):
        settings = await GuildSettingsRepository(get_database()).get_or_create(int(interaction.guild_id or 0))
        if not isinstance(interaction.user, discord.Member) or not can_manage_paid_raffles(interaction.user, settings):
            log.info("Paid raffle controls denied user_id=%s guild_id=%s", interaction.user.id, interaction.guild_id)
            await interaction.response.send_message("You need the Admin role or the Paid Raffle Admin role to use this.", ephemeral=True)
            return
        await self._send_raffle_controls_panel(interaction)

    @tasks.loop(seconds=30)
    async def auto_verify_payments(self):
        if not await self._ensure_db_ready("raffles.auto_verify_payments"):
            return
        """Auto-poll Torn API for payment verification at 4:30 mark."""
        worker_slot = db_heavy_worker_slot("raffles.auto_verify_payments")
        await worker_slot.__aenter__()
        try:
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
                            await _update_raffle_purchase_panel_counts(self.bot, repo, int(entry["raffle_id"]))
                            details = await repo.get_entry_with_raffle(int(entry["entry_id"]))
                            if details:
                                self.bot.dispatch(
                                    "paid_raffle_purchase_verified",
                                    {
                                        "guild_id": int(details.get("guild_id") or 0),
                                        "user_id": int(details.get("discord_id") or 0),
                                        "raffle_id": int(details.get("raffle_id") or entry["raffle_id"]),
                                        "entry_id": int(details.get("entry_id") or entry["entry_id"]),
                                        "ticket_count": int(details.get("num_tickets") or 0),
                                        "payment_type": str(details.get("ticket_payment_type") or ""),
                                        "verified_at": datetime.now(timezone.utc),
                                        "dedupe_key": f"raffle_purchase:{int(details.get('entry_id') or entry['entry_id'])}",
                                    },
                                )
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
                            await _update_raffle_purchase_panel_counts(self.bot, repo, int(entry["raffle_id"]))
                    except Exception as e:
                        log.error(f"Auto-verify error for entry {entry['entry_id']}: {e}")
            except Exception as e:
                log.error(f"Error in auto_verify_payments task: {e}")
        finally:
            await worker_slot.__aexit__(None, None, None)

    @tasks.loop(minutes=1)
    async def check_raffles(self):
        if not await self._ensure_db_ready("raffles.check_raffles"):
            return
        """Check for raffles that need to be drawn."""
        worker_slot = db_heavy_worker_slot("raffles.check_raffles")
        await worker_slot.__aenter__()
        try:
            try:
                repo = RafflesRepository(get_pool())
                raffles = await repo.get_raffles_to_draw()
                for raffle in raffles:
                    try:
                        result = await repo.draw_raffle_winner(raffle["raffle_id"])
                        if result:
                            updated_raffle = await repo.get_raffle(int(raffle["raffle_id"]))
                            if updated_raffle:
                                await self._handle_raffle_completed(updated_raffle, result)
                    except Exception as e:
                        log.error(f"Error drawing raffle {raffle['raffle_id']}: {e}")
            except Exception as e:
                log.error(f"Error in check_raffles task: {e}")
        finally:
            await worker_slot.__aexit__(None, None, None)

    @tasks.loop(minutes=5)
    async def cleanup_expired(self):
        if not await self._ensure_db_ready("raffles.cleanup_expired"):
            return

        db = get_database()

        async def _run_once() -> int:
            repo = RafflesRepository(get_pool())
            return int(await repo.cleanup_expired_raffle_entries() or 0)

        worker_slot = db_heavy_worker_slot("raffles.cleanup_expired")
        await worker_slot.__aenter__()
        try:
            try:
                acquired, cleaned = await run_with_advisory_lock(db, "worker:raffles:cleanup_expired", _run_once)
                if not acquired:
                    return

                cleaned = int(cleaned or 0)
                if cleaned > 0:
                    log_event(
                        log,
                        logging.INFO,
                        "raffle.cleanup_expired",
                        action="cleanup_expired",
                        result="ok",
                        cleaned=cleaned,
                    )
                    log.info("Cleaned up %s expired raffle entries", cleaned)
                else:
                    log.debug("raffle.cleanup_expired tick cleaned=0")
            except Exception as e:
                log_event(
                    log,
                    logging.ERROR,
                    "raffle.cleanup_expired.failed",
                    action="cleanup_expired",
                    result="error",
                    error_type=type(e).__name__,
                    exc_info=True,
                )
        finally:
            await worker_slot.__aexit__(None, None, None)

    @tasks.loop(minutes=5)
    async def cleanup_closed_panels(self):
        if not await self._ensure_db_ready("raffles.cleanup_closed_panels"):
            return

        db = get_database()

        async def _run_once() -> int:
            repo = RafflesRepository(get_pool())
            stale = await repo.get_stale_raffles_for_cleanup()
            for raffle in stale:
                await self._disable_raffle_panels(raffle, status_text=f"Raffle {raffle.get('status')}.")
                await self._disable_prize_dm_confirm(raffle)
                await repo.mark_cleaned(int(raffle["raffle_id"]))
            return int(len(stale))

        worker_slot = db_heavy_worker_slot("raffles.cleanup_closed_panels")
        await worker_slot.__aenter__()
        try:
            try:
                acquired, cleaned = await run_with_advisory_lock(db, "worker:raffles:cleanup_panels", _run_once)
                if not acquired:
                    return

                cleaned = int(cleaned or 0)
                if cleaned > 0:
                    log_event(
                        log,
                        logging.INFO,
                        "raffle.cleanup_panels",
                        action="cleanup_panels",
                        result="ok",
                        cleaned=cleaned,
                    )
                else:
                    log.debug("raffle.cleanup_panels tick cleaned=0")
            except Exception as e:
                log_event(
                    log,
                    logging.ERROR,
                    "raffle.cleanup_panels.failed",
                    action="cleanup_panels",
                    result="error",
                    error_type=type(e).__name__,
                    exc_info=True,
                )
        finally:
            await worker_slot.__aexit__(None, None, None)


    async def _before_worker_loop(self, worker_name: str) -> None:
        await wait_until_initialized(timeout=30.0)
        await self.bot.wait_until_ready()
        await sleep_startup_jitter(worker_name)

    @auto_verify_payments.before_loop
    async def before_auto_verify_payments(self) -> None:
        await self._before_worker_loop("raffles.auto_verify_payments")

    @check_raffles.before_loop
    async def before_check_raffles(self) -> None:
        await self._before_worker_loop("raffles.check_raffles")

    @cleanup_expired.before_loop
    async def before_cleanup_expired(self) -> None:
        await self._before_worker_loop("raffles.cleanup_expired")

    @cleanup_closed_panels.before_loop
    async def before_cleanup_closed_panels(self) -> None:
        await self._before_worker_loop("raffles.cleanup_closed_panels")

async def setup(bot: commands.Bot):
    await bot.add_cog(RafflesCog(bot))
