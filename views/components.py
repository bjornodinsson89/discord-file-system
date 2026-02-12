"""Discord views for all Happy Jumper features."""

import asyncio
import discord
from discord import ui
import logging
import json
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
from utils import get_database, get_security_manager, get_torn_api
from utils.discord_channels import resolve_guild_channel
from utils.torn_api import TornAPIError, TornAPIPermissionError
from utils.item_resolver import ItemResolver
from utils.embeds import *
from utils.payouts import parse_payout_string, payout_items_to_human, payout_items_to_string, PayoutParseError
from services import JumpService, RaffleService, DomainError, AlreadyExists, NotFound, InvalidInput, BusinessRuleViolation
from services.jump_monitor import get_jump_monitor
from repositories.jumps import JumpsRepository
import config

log = logging.getLogger("happy_jumper.views")

from .application_review_view import ApplicationReviewView


# ============================================================================
# API KEY VIEWS
# ============================================================================

class ApiKeyIntroView(ui.View):
    def __init__(self):
        super().__init__(timeout=300)

        # Link-style buttons cannot be declared via @ui.button (no callback).
        # They must be added as plain Button items.
        self.add_item(
            ui.Button(
                label="Create API Key",
                style=discord.ButtonStyle.link,
                url=config.TORN_API_KEY_LINK,
            )
        )

    @ui.button(label="Enter API Key", style=discord.ButtonStyle.primary, emoji=config.EMOJI_LOCK)
    async def enter_key(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ApiKeyModal())

    @ui.button(label="View Guide", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_INFO)
    async def guide(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(embed=create_api_key_guide_embed(), ephemeral=True)



class ApiKeyModal(ui.Modal, title="Register Torn API Key"):
    api_key = ui.TextInput(label="Torn API Key", placeholder="Enter your 16-character API key", min_length=16, max_length=16)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            api_key = self.api_key.value.strip()
            torn_api = get_torn_api()
            discord_id_api, torn_id, _perms = await torn_api.validate_api_key(api_key)
            
            if discord_id_api != interaction.user.id:
                await interaction.followup.send(embed=create_error_embed(
                    "Discord ID Mismatch", f"Key belongs to Discord user {discord_id_api}"), ephemeral=True)
                return
            
            security = get_security_manager()
            encrypted = security.encrypt(api_key)
            
            db = get_database()
            await db.set_user_api_key(interaction.user.id, torn_id, encrypted, guild_id=interaction.guild_id)
            try:
                await db.log_audit(interaction.user.id, "api_key_registered", "user", interaction.user.id, {"torn_id": torn_id})
            except Exception:
                log.exception("Failed to write api_key_registered audit log")
            
            await interaction.followup.send(embed=create_success_embed(
                "API Key Registered", f"Torn ID: `{torn_id}`"), ephemeral=True)
        except TornAPIPermissionError as e:
            await interaction.followup.send(embed=create_error_embed("Insufficient Permissions", str(e)), ephemeral=True)
        except TornAPIError as e:
            await interaction.followup.send(embed=create_error_embed("Validation Failed", str(e)), ephemeral=True)
        except Exception as e:
            log.exception(f"API key error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", "An unexpected error occurred"), ephemeral=True)


class ConfirmRemoveKeyView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)
    
    @ui.button(label="Yes, Remove", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        await db.delete_user_api_key(interaction.user.id)
        await db.log_audit(interaction.user.id, "api_key_removed", "user", interaction.user.id)
        await interaction.followup.send(embed=create_success_embed("API Key Removed"), ephemeral=True)
        self.stop()
    
    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        self.stop()


# ============================================================================
# JUMP VIEWS
# ============================================================================

class JumpSessionView(ui.View):
    _status_panel_tasks: dict[tuple[int, int], asyncio.Task] = {}

    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        self.session_id = session_id
    
    @ui.button(label="Join Jump", style=discord.ButtonStyle.success, emoji=config.EMOJI_JUMP, custom_id="jump_join")
    async def join(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            service = JumpService(db, get_torn_api(), get_security_manager())
            result = await service.join_session(
                session_id=self.session_id,
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
            )
            if result["result"] == "waitlist":
                await interaction.followup.send(
                    embed=create_info_embed("Session Full", f"Added to waitlist at position #{result['position']}"),
                    ephemeral=True,
                )
                return

            embed_update_status = await update_jump_embed(self.session_id, interaction.message)
            if embed_update_status == "missing_access":
                await interaction.followup.send(
                    "I reserved your spot, but I can't update the session message due to missing channel permissions. "
                    "An admin should grant me View Channel, Send Messages, Read Message History, and Embed Links in "
                    "the 99k-jumps channel (and thread if applicable).",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=create_info_embed(
                        "Spot Reserved",
                        f"Please send payment and click **Mark as Paid** before <t:{int(result['reserved_until'].timestamp())}:R>",
                    ),
                    view=PaymentView(self.session_id),
                    ephemeral=True,
                )
        except AlreadyExists as exc:
            msg = str(exc)
            title = "Already Signed Up" if msg.startswith("Status") else "Already Signed Up"
            await interaction.followup.send(embed=create_warning_embed(title, msg), ephemeral=True)
        except NotFound:
            await interaction.followup.send(embed=create_error_embed("Session Unavailable"), ephemeral=True)
        except InvalidInput:
            await interaction.followup.send(embed=create_error_embed("API Key Required", "Use `/set_api_key`"), ephemeral=True)
        except BusinessRuleViolation as exc:
            text = str(exc)
            if text.endswith("remaining"):
                await interaction.followup.send(embed=create_warning_embed("Drug Cooldown", text), ephemeral=True)
            else:
                await interaction.followup.send(embed=create_error_embed("Blacklisted", text), ephemeral=True)
        except Exception as e:
            log.exception("Join error: %s", e)
            await interaction.followup.send(embed=create_error_embed("Unable to join session"), ephemeral=True)

    @ui.button(label="Leave Jump", style=discord.ButtonStyle.danger, emoji=config.EMOJI_CROSS, custom_id="jump_leave")
    async def leave(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            signup = await db.get_signup(self.session_id, interaction.user.id)
            if not signup:
                await interaction.followup.send(embed=create_error_embed("Not Signed Up"), ephemeral=True)
                return
            if signup['status'] == 'paid':
                await interaction.followup.send(embed=create_warning_embed("Payment Verified", "Contact host to cancel"), ephemeral=True)
                return
            
            await db.delete_signup(self.session_id, interaction.user.id)
            await db.log_audit(interaction.user.id, "jump_leave", "session", self.session_id)
            
            # Promote from waitlist
            next_user = await db.promote_from_waitlist(self.session_id)
            if next_user:
                settings = await db.get_guild_settings(interaction.guild.id)
                timeout = settings.get('reservation_timeout_minutes', config.DEFAULT_RESERVATION_TIMEOUT)
                reserved_until = datetime.utcnow() + timedelta(minutes=timeout)
                await db.create_signup(self.session_id, next_user['discord_id'], next_user['torn_user_id'], reserved_until)
            
            await update_jump_embed(self.session_id, interaction.message)
            get_jump_monitor().mark_needs_refresh(self.session_id)
            await interaction.followup.send(embed=create_success_embed("Left Session"), ephemeral=True)
        except Exception as e:
            log.exception(f"Leave error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)
    
    @ui.button(label="Status Panel", style=discord.ButtonStyle.secondary, custom_id="jump_status_panel")
    async def status_panel(self, interaction: discord.Interaction, button: ui.Button):
        monitor = get_jump_monitor()
        await monitor.start(self.session_id)

        embed = await build_jump_status_panel_embed(self.session_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        key = (interaction.user.id, self.session_id)
        existing_task = self._status_panel_tasks.get(key)
        if existing_task and not existing_task.done():
            existing_task.cancel()

        self._status_panel_tasks[key] = asyncio.create_task(
            self._run_status_panel_refresh(interaction)
        )

    @ui.button(label="Start Jump", style=discord.ButtonStyle.primary, emoji="🚀", custom_id="jump_start")
    async def start_jump(self, interaction: discord.Interaction, button: ui.Button):
        db = get_database()
        session = await db.get_jump_session(self.session_id)
        if not session:
            await interaction.response.send_message(embed=create_error_embed("Session Not Found"), ephemeral=True)
            return

        if session["host_discord_id"] != interaction.user.id:
            await interaction.response.send_message(embed=create_error_embed("Not Authorized", "Only the jump host can start this jump."), ephemeral=True)
            return

        await interaction.response.send_message(
            embed=create_info_embed("Start Jump", "Choose a delay before DM notifications are sent to participants."),
            view=StartJumpDelayView(self.session_id),
            ephemeral=True,
        )

    async def _run_status_panel_refresh(self, interaction: discord.Interaction) -> None:
        started = datetime.utcnow()
        while True:
            try:
                if (datetime.utcnow() - started).total_seconds() > 3600:
                    return

                db = get_database()
                session = await db.get_jump_session(self.session_id)
                if not session or session.get("status") not in {"open", "locked"}:
                    return

                await asyncio.sleep(10)
                embed = await build_jump_status_panel_embed(self.session_id)
                await interaction.edit_original_response(embed=embed)
            except asyncio.CancelledError:
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
            except Exception:
                log.exception("Status panel refresh failed session_id=%s", self.session_id)
                return

    @ui.button(label="Session Info", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_INFO, custom_id="jump_info")
    async def info(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        session = await db.get_jump_session(self.session_id)
        if not session:
            await interaction.followup.send(embed=create_error_embed("Session Not Found"), ephemeral=True)
            return
        
        signups = await db.get_session_signups(self.session_id)
        user_signup = next((s for s in signups if s['discord_id'] == interaction.user.id), None)
        waitlist = await db.get_session_waitlist(self.session_id)
        user_wait = next((w for w in waitlist if w['discord_id'] == interaction.user.id), None)
        
        info = f"**Session #{session['id']}** ({session['status']})\n"
        info += f"Host: <@{session['host_discord_id']}>\n"
        info += f"Spots: {len(signups)}/{session['max_spots']}\n"
        info += f"Waitlist: {len(waitlist)}\n"
        
        if user_signup:
            info += f"\n**Your Status:** {user_signup['status']}"
            if user_signup['status'] == 'reserved' and user_signup['reserved_until']:
                info += f"\nExpires: <t:{int(user_signup['reserved_until'].timestamp())}:R>"
        elif user_wait:
            info += f"\n**Waitlist Position:** #{user_wait['position']}"
        
        await interaction.followup.send(embed=create_info_embed("Session Info", info), ephemeral=True)
    
    @ui.button(label="Join Waitlist", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_LIST, custom_id="jump_waitlist")
    async def waitlist(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        try:
            position = await JumpService(db, get_torn_api(), get_security_manager()).join_waitlist(
                session_id=self.session_id,
                user_id=interaction.user.id,
            )
            get_jump_monitor().mark_needs_refresh(self.session_id)
            await interaction.followup.send(embed=create_success_embed("Added to Waitlist", f"Position: #{position}"), ephemeral=True)
        except InvalidInput:
            await interaction.followup.send(embed=create_error_embed("API Key Required"), ephemeral=True)
        except AlreadyExists as exc:
            await interaction.followup.send(embed=create_warning_embed("Already on Waitlist", str(exc)), ephemeral=True)
        except Exception as e:
            log.exception("Waitlist join error: %s", e)
            await interaction.followup.send(embed=create_error_embed("Error", "Unable to join waitlist"), ephemeral=True)



class PaymentView(ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=300)
        self.session_id = session_id
    
    @ui.button(label="Mark as Paid", style=discord.ButtonStyle.success, emoji=config.EMOJI_MONEY)
    async def mark_paid(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            signup = await db.get_signup(self.session_id, interaction.user.id)
            if not signup:
                await interaction.followup.send(embed=create_error_embed("Not Signed Up"), ephemeral=True)
                return
            if signup['status'] == 'paid':
                await interaction.followup.send(embed=create_warning_embed("Already Verified"), ephemeral=True)
                return
            
            session = await db.get_jump_session(self.session_id)
            key_data = await db.get_user_api_key(interaction.user.id)
            
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            torn_api = get_torn_api()
            
            payment = await torn_api.verify_payment(
                api_key, session['host_torn_id'], session['payment_type'],
                session['payment_amount'], session.get('payment_item_id'),
                int(session['created_at'].timestamp())
            )
            
            if not payment:
                await interaction.followup.send(embed=create_error_embed(
                    "Payment Not Found", "Send payment and wait a few moments before trying again."), ephemeral=True)
                return
            
            jump_repo = JumpsRepository(db.pool)
            await jump_repo.mark_purchase_verified(self.session_id, interaction.user.id)
            await db.log_audit(interaction.user.id, "payment_verified", "session", self.session_id, payment)
            get_jump_monitor().mark_needs_refresh(self.session_id)
            
            # Try to update embed
            settings = await db.get_guild_settings(interaction.guild.id)
            channel_id = settings.get('jump_99k_channel_id')
            if channel_id and session.get('announcement_message_id'):
                try:
                    channel = interaction.guild.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(session['announcement_message_id'])
                        await update_jump_embed(self.session_id, message)
                except Exception:
                    pass
            
            await interaction.followup.send(
                embed=create_success_embed("Payment Verified!", "You're all set for the jump!"),
                view=InsuranceOfferView(self.session_id, interaction.user.id),
                ephemeral=True,
            )
        except Exception as e:
            log.exception(f"Payment verification error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)


class InsuranceOfferView(ui.View):
    def __init__(self, session_id: int, buyer_discord_id: int):
        super().__init__(timeout=300)
        self.session_id = session_id
        self.buyer_discord_id = buyer_discord_id

    @ui.button(label="Yes, request insurance", style=discord.ButtonStyle.success, emoji=config.EMOJI_SHIELD)
    async def request_insurance(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.buyer_discord_id:
            await interaction.followup.send(embed=create_error_embed("Not Authorized"), ephemeral=True)
            return

        db = get_database()
        session = await db.get_jump_session(self.session_id)
        if not session:
            await interaction.followup.send(embed=create_error_embed("Session Not Found"), ephemeral=True)
            return

        settings = await db.get_guild_settings(interaction.guild.id)
        insurance_channel_id = settings.get("insurance_channel_id")
        channel = interaction.guild.get_channel(int(insurance_channel_id)) if insurance_channel_id else None
        if channel is None:
            await interaction.followup.send(
                embed=create_error_embed("Insurance Channel Missing", "Ask an admin to configure `insurance_channel_id` in setup."),
                ephemeral=True,
            )
            return

        message = await channel.send(
            content=f"<@{interaction.user.id}> has requested 99k jump insurance for Jump #{self.session_id}",
            view=InsuranceClaimView(self.session_id, interaction.user.id),
        )
        await db.log_audit(interaction.user.id, "jump_insurance_requested", "session", self.session_id, {"insurance_message_id": message.id})
        await interaction.followup.send(embed=create_success_embed("Insurance Requested", "Your request was posted in the insurance channel."), ephemeral=True)

    @ui.button(label="No thanks", style=discord.ButtonStyle.secondary)
    async def no_thanks(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(embed=create_info_embed("No Problem", "You can request insurance later if needed."), ephemeral=True)


class InsuranceClaimView(ui.View):
    def __init__(self, session_id: int, requester_discord_id: int):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.requester_discord_id = requester_discord_id

    @ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def claim(self, interaction: discord.Interaction, button: ui.Button):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(embed=create_error_embed("Server Member Required"), ephemeral=True)
            return

        db = get_database()
        settings = await db.get_guild_settings(interaction.guild.id)
        insurer_role_id = settings.get("insurer_role_id")
        has_insurer_role = bool(insurer_role_id and any(role.id == int(insurer_role_id) for role in interaction.user.roles))

        if not has_insurer_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                embed=create_error_embed("Not Authorized", "Only verified insurers can claim this request."),
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            content=(
                f"<@{self.requester_discord_id}> has requested 99k jump insurance for Jump #{self.session_id}\n"
                f"✅ Claimed by <@{interaction.user.id}>"
            ),
            view=None,
        )

        try:
            requester = interaction.guild.get_member(self.requester_discord_id) or await interaction.guild.fetch_member(self.requester_discord_id)
            await requester.send(f"Your 99k jump insurance request for Jump #{self.session_id} was claimed by {interaction.user.mention}.")
        except Exception:
            pass

        try:
            await interaction.user.send(f"You claimed insurance request for Jump #{self.session_id} from <@{self.requester_discord_id}>.")
        except Exception:
            pass

        await db.log_audit(interaction.user.id, "jump_insurance_claimed", "session", self.session_id, {"requester_discord_id": self.requester_discord_id})


class StartJumpCustomDelayModal(ui.Modal, title="Custom Jump Delay"):
    delay = ui.TextInput(label="Delay (mm:ss or minutes)", placeholder="e.g. 3:30 or 5", max_length=8)

    def __init__(self, session_id: int):
        super().__init__()
        self.session_id = session_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.delay.value or "").strip()
        seconds = _parse_start_delay_seconds(raw)
        if seconds is None or seconds <= 0:
            await interaction.response.send_message(embed=create_error_embed("Invalid Delay", "Use `minutes` or `mm:ss` format."), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await _start_jump_countdown(interaction, self.session_id, seconds)


class StartJumpDelayView(ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=120)
        self.session_id = session_id

    @ui.select(
        placeholder="Choose delay",
        options=[
            discord.SelectOption(label="1 minute", value="60"),
            discord.SelectOption(label="2 minutes", value="120"),
            discord.SelectOption(label="3 minutes", value="180"),
            discord.SelectOption(label="5 minutes", value="300"),
            discord.SelectOption(label="10 minutes", value="600"),
        ],
    )
    async def select_delay(self, interaction: discord.Interaction, select: ui.Select):
        await interaction.response.defer(ephemeral=True)
        seconds = int(select.values[0])
        await _start_jump_countdown(interaction, self.session_id, seconds)

    @ui.button(label="Custom", style=discord.ButtonStyle.secondary)
    async def custom_delay(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(StartJumpCustomDelayModal(self.session_id))


class HostControlView(ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=300)
        self.session_id = session_id
    
    @ui.button(label="Lock Session", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_LOCK)
    async def lock(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        session = await db.get_jump_session(self.session_id)
        if session['host_discord_id'] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(embed=create_error_embed("Not Authorized"), ephemeral=True)
            return
        await db.update_jump_session(self.session_id, status='locked')
        await db.log_audit(interaction.user.id, "session_locked", "session", self.session_id)
        await interaction.followup.send(embed=create_success_embed("Session Locked"), ephemeral=True)
    
    @ui.button(label="Complete Session", style=discord.ButtonStyle.success, emoji=config.EMOJI_CHECK)
    async def complete(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        session = await db.get_jump_session(self.session_id)
        if session['host_discord_id'] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(embed=create_error_embed("Not Authorized"), ephemeral=True)
            return
        service = JumpService(db, get_torn_api(), get_security_manager())
        await service.end_jump(session_id=self.session_id, status="completed")
        await db.log_audit(interaction.user.id, "session_completed", "session", self.session_id)
        await interaction.followup.send(embed=create_success_embed("Session Completed"), view=RatingRequestView(self.session_id), ephemeral=True)
    
    @ui.button(label="Cancel Session", style=discord.ButtonStyle.danger, emoji=config.EMOJI_CROSS)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Confirm cancellation?", view=ConfirmCancelSessionView(self.session_id), ephemeral=True)
    
    @ui.button(label="Refresh Readiness", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        await refresh_session_readiness(self.session_id)
        await interaction.followup.send(embed=create_success_embed("Readiness Refreshed"), ephemeral=True)


class ConfirmCancelSessionView(ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=60)
        self.session_id = session_id
    
    @ui.button(label="Yes, Cancel", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        service = JumpService(db, get_torn_api(), get_security_manager())
        await service.end_jump(session_id=self.session_id, status="cancelled")
        await db.log_audit(interaction.user.id, "session_cancelled", "session", self.session_id)
        await interaction.followup.send(embed=create_success_embed("Session Cancelled"), ephemeral=True)
        self.stop()
    
    @ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Kept active.", ephemeral=True)
        self.stop()


class RatingRequestView(ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=300)
        self.session_id = session_id
        for i in range(1, 6):
            self.add_item(RatingButton(session_id, i))


class RatingButton(ui.Button):
    def __init__(self, session_id: int, rating: int):
        super().__init__(label="⭐" * rating, style=discord.ButtonStyle.secondary, custom_id=f"rate_{session_id}_{rating}")
        self.session_id = session_id
        self.rating = rating
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        session = await db.get_jump_session(self.session_id)
        await db.add_host_rating(session['host_discord_id'], interaction.user.id, self.session_id, self.rating)
        await interaction.followup.send(embed=create_success_embed("Rating Submitted", f"You rated the host {self.rating}/5 stars"), ephemeral=True)


def _coverage_label(value: str) -> str:
    labels = {
        "xanax": "Xanax Stack",
        "xanax_stack": "Xanax Stack",
        "ecstasy_after_stack": "Ecstasy After Stack",
        "all_drugs": "All Drugs",
    }
    return labels.get((value or "").lower(), value or "Unknown")


def _trim_text(value: str, limit: int = 180) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit-3]}..."


def _payout_line(items: List[Dict]) -> str:
    return f"Payout: {payout_items_to_human(items)}"


def _extract_log_id(log_entry: Dict) -> Optional[int]:
    return log_entry.get("id") or log_entry.get("log_id")


def _extract_counterparty_torn_id(log_entry: Dict) -> Optional[int]:
    details_id = (log_entry.get("details") or {}).get("id")
    data = log_entry.get("data") or {}
    if details_id == 4102:
        value = data.get("receiver")
        return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None
    if details_id == 4103:
        value = data.get("sender")
        return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None
    return None


def _count_item_qty_by_id(log_entry: Dict, item_id: int) -> int:
    if not item_id:
        return 0
    data = log_entry.get("data") or {}
    items = data.get("items") or []
    return sum(
        int(item.get("qty") or 0)
        for item in items
        if int(item.get("id") or 0) == item_id
    )



# ============================================================================
# INSURANCE VIEWS
# ============================================================================

class InsurerProviderSelect(ui.Select):
    def __init__(self, browser_view: "InsurerBrowserView", providers: List[Dict]):
        options: List[discord.SelectOption] = []
        for provider in providers[:25]:
            title = provider.get("company_name") or provider.get("display_name") or f"Provider {provider['provider_id']}"
            type_values = provider.get("policy_types") or []
            type_summary = ", ".join(_coverage_label(v) for v in type_values[:2]) or "None"
            if len(type_values) > 2:
                type_summary += "+"
            policy_count = provider.get("active_policy_count", 0) if browser_view.active_only else provider.get("total_policy_count", 0)
            desc = f"Types: {type_summary} • Policies: {policy_count}"
            options.append(discord.SelectOption(
                label=title[:100],
                description=desc[:100],
                value=str(provider["provider_id"]),
            ))

        super().__init__(
            placeholder="Select an insurer to view card...",
            options=options,
            custom_id=f"insurers:select:{browser_view.page}",
        )
        self.browser_view = browser_view

    async def callback(self, interaction: discord.Interaction):
        provider_id = int(self.values[0])
        card_view = InsurerCardView(
            guild_id=self.browser_view.guild_id,
            provider_id=provider_id,
            active_only=self.browser_view.active_only,
            coverage_type=self.browser_view.coverage_type,
            jump_type=self.browser_view.jump_type,
            parent_page=self.browser_view.page,
            timeout=self.browser_view.timeout,
        )
        embed = await card_view.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=card_view)


class InsurerBrowserView(ui.View):
    def __init__(
        self,
        guild_id: int,
        active_only: bool = True,
        coverage_type: Optional[str] = None,
        jump_type: str = "99k",
        page: int = 0,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.active_only = active_only
        self.coverage_type = coverage_type
        self.jump_type = jump_type
        self.page = max(0, page)
        self.providers: List[Dict] = []

    async def _load(self, client: discord.Client):
        db = get_database()
        rows = await db.get_approved_providers_for_browser(
            guild_id=self.guild_id,
            active_only=self.active_only,
            coverage_type=self.coverage_type,
            jump_type=self.jump_type,
        )
        for row in rows:
            row["display_name"] = f"Discord User {row['discord_id']}"
            user = client.get_user(int(row["discord_id"]))
            if user is None:
                try:
                    user = await client.fetch_user(int(row["discord_id"]))
                except Exception:
                    user = None
            if user:
                row["display_name"] = user.display_name
        self.providers = rows

    def _max_page(self) -> int:
        if not self.providers:
            return 0
        return (len(self.providers) - 1) // 25

    def _slice(self) -> List[Dict]:
        start = self.page * 25
        return self.providers[start:start + 25]

    async def build_embed(self, client: discord.Client) -> discord.Embed:
        await self._load(client)
        self.clear_items()
        max_page = self._max_page()
        self.page = min(self.page, max_page)
        page_items = self._slice()

        filter_text = f"active_only={self.active_only} • jump_type={self.jump_type or 'none'}"
        if self.coverage_type:
            filter_text += f" • coverage={_coverage_label(self.coverage_type)}"

        if not page_items:
            embed = create_info_embed(
                "Approved Insurers",
                "No approved insurers match these filters yet. Try changing filters or check back later.",
            )
            embed.set_footer(text=filter_text)
            self.add_item(self.refresh_button)
            return embed

        self.add_item(InsurerProviderSelect(self, page_items))
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= max_page
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.add_item(self.refresh_button)

        embed = create_info_embed(
            "Approved Insurers",
            f"Showing approved insurers for this server. Select one from dropdown below.\nPage {self.page + 1}/{max_page + 1}",
        )
        embed.set_footer(text=filter_text)
        return embed

    @ui.button(label="Prev", style=discord.ButtonStyle.secondary, custom_id="insurers:list:prev")
    async def prev_button(self, interaction: discord.Interaction, button: ui.Button):
        self.page = max(0, self.page - 1)
        embed = await self.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="insurers:list:next")
    async def next_button(self, interaction: discord.Interaction, button: ui.Button):
        self.page += 1
        embed = await self.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Refresh", style=discord.ButtonStyle.primary, custom_id="insurers:list:refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: ui.Button):
        embed = await self.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=self)


class InsurerCardView(ui.View):
    def __init__(
        self,
        guild_id: int,
        provider_id: int,
        active_only: bool = True,
        coverage_type: Optional[str] = None,
        jump_type: str = "99k",
        parent_page: int = 0,
        policy_page: int = 0,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.provider_id = provider_id
        self.active_only = active_only
        self.coverage_type = coverage_type
        self.jump_type = jump_type
        self.parent_page = max(0, parent_page)
        self.policy_page = max(0, policy_page)
        self.provider: Optional[Dict] = None
        self.policies: List[Dict] = []

    async def _load(self, client: discord.Client):
        db = get_database()
        self.provider = await db.get_provider_by_id(self.provider_id)
        self.policies = await db.get_provider_policies_for_browser(
            guild_id=self.guild_id,
            provider_id=self.provider_id,
            active_only=self.active_only,
            coverage_type=self.coverage_type,
            jump_type=self.jump_type,
        )
        if self.provider:
            self.provider = dict(self.provider)
            self.provider["display_name"] = f"Discord User {self.provider['discord_id']}"
            user = client.get_user(int(self.provider["discord_id"]))
            if user is None:
                try:
                    user = await client.fetch_user(int(self.provider["discord_id"]))
                except Exception:
                    user = None
            if user:
                self.provider["display_name"] = user.display_name

    async def build_embed(self, client: discord.Client) -> discord.Embed:
        await self._load(client)
        self.clear_items()
        self.add_item(self.back_button)

        if not self.provider:
            embed = create_error_embed("Insurer Not Found", "That insurer could not be loaded.")
            return embed

        policies_per_page = 5
        max_page = max((len(self.policies) - 1) // policies_per_page, 0) if self.policies else 0
        self.policy_page = min(self.policy_page, max_page)
        start = self.policy_page * policies_per_page
        subset = self.policies[start:start + policies_per_page]

        title = self.provider.get("company_name") or self.provider.get("display_name") or f"Provider {self.provider_id}"
        description = (
            f"**Provider:** <@{self.provider['discord_id']}>\n"
            f"**Torn ID:** `{self.provider.get('torn_user_id', 'Unknown')}`\n"
            f"**Company:** {title}"
        )

        app_data = self.provider.get("application_data") or {}
        forum_url = app_data.get("forum_url") if isinstance(app_data, dict) else None
        if forum_url:
            description += f"\n**Forum URL:** {forum_url}"

        embed = create_info_embed("Insurer Card", description)

        if not subset:
            embed.add_field(name="Policy Summary", value="No policies found for the selected filters.", inline=False)
        else:
            for policy in subset:
                covered = policy.get("covered_jump_types") or []
                covered_text = ", ".join(covered) if covered else "None"
                body = (
                    f"**Jump Types:** {covered_text}\n"
                    f"**Coverage:** {_coverage_label(policy.get('coverage_type'))}\n"
                    f"**Cost:** {policy.get('cost_type', 'unknown')} {policy.get('cost_amount', 0)}\n"
                    f"**Duration:** {policy.get('duration_hours', 0)} hours\n"
                    f"**{_payout_line(policy.get('payout_items') or [])}**\n"
                    f"**Description:** {_trim_text(policy.get('description') or 'No description provided.', 220)}"
                )
                embed.add_field(name=f"#{policy['policy_id']} • {policy.get('name', 'Policy')}", value=body, inline=False)

        if len(self.policies) > policies_per_page:
            self.prev_policies_button.disabled = self.policy_page <= 0
            self.next_policies_button.disabled = self.policy_page >= max_page
            self.add_item(self.prev_policies_button)
            self.add_item(self.next_policies_button)
            embed.set_footer(text=f"Policy page {self.policy_page + 1}/{max_page + 1}")

        return embed

    @ui.button(label="Back", style=discord.ButtonStyle.secondary, custom_id="insurers:card:back")
    async def back_button(self, interaction: discord.Interaction, button: ui.Button):
        list_view = InsurerBrowserView(
            guild_id=self.guild_id,
            active_only=self.active_only,
            coverage_type=self.coverage_type,
            jump_type=self.jump_type,
            page=self.parent_page,
            timeout=self.timeout,
        )
        embed = await list_view.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=list_view)

    @ui.button(label="Prev Policies", style=discord.ButtonStyle.secondary, custom_id="insurers:card:prev")
    async def prev_policies_button(self, interaction: discord.Interaction, button: ui.Button):
        self.policy_page = max(0, self.policy_page - 1)
        embed = await self.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Next Policies", style=discord.ButtonStyle.secondary, custom_id="insurers:card:next")
    async def next_policies_button(self, interaction: discord.Interaction, button: ui.Button):
        self.policy_page += 1
        embed = await self.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=self)


class InsurancePolicyView(ui.View):
    def __init__(self, policy_id: int):
        super().__init__(timeout=300)
        self.policy_id = policy_id
    
    @ui.button(label="Purchase Coverage", style=discord.ButtonStyle.success, emoji=config.EMOJI_SHIELD)
    async def purchase(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(PurchaseCoverageModal(self.policy_id))


class PurchaseCoverageModal(ui.Modal, title="Purchase Insurance Coverage"):
    xanax_count = ui.TextInput(label="Xanax to Cover", placeholder="How many Xanax?", min_length=1, max_length=4)
    
    def __init__(self, policy_id: int):
        super().__init__()
        self.policy_id = policy_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            xanax = int(self.xanax_count.value)
            if xanax < config.MIN_COVERAGE_XANAX or xanax > config.MAX_COVERAGE_XANAX:
                await interaction.followup.send(embed=create_error_embed("Invalid Amount", f"Must be {config.MIN_COVERAGE_XANAX}-{config.MAX_COVERAGE_XANAX}"), ephemeral=True)
                return
            
            db = get_database()
            policy = await db.get_policy(self.policy_id)
            if not policy or not policy['active']:
                await interaction.followup.send(embed=create_error_embed("Policy Unavailable"), ephemeral=True)
                return
            
            if xanax > policy['max_coverage_xanax']:
                await interaction.followup.send(embed=create_error_embed("Exceeds Max Coverage", f"Max: {policy['max_coverage_xanax']}"), ephemeral=True)
                return
            
            key_data = await db.get_user_api_key(interaction.user.id)
            if not key_data:
                await interaction.followup.send(embed=create_error_embed("API Key Required"), ephemeral=True)
                return
            
            premium = xanax * policy['premium_per_xanax']
            payout = xanax * policy['payout_per_xanax']
            expires_at = datetime.utcnow() + timedelta(hours=policy['duration_hours'])
            
            coverage_id = await db.create_coverage(
                self.policy_id, interaction.user.id, key_data['torn_user_id'],
                xanax, premium, payout, expires_at
            )
            
            provider = await db.get_provider_by_id(policy['provider_id'])
            
            await interaction.followup.send(embed=create_info_embed(
                "Coverage Created - Payment Required",
                f"**Premium:** ${premium:,}\n"
                f"**Send to:** <@{provider['discord_id']}> (Torn: `{provider['torn_user_id']}`)\n"
                f"**Payout if OD:** ${payout:,}\n"
                f"**Duration:** {policy['duration_hours']} hours"
            ), view=InsurancePaymentView(coverage_id), ephemeral=True)
        except ValueError:
            await interaction.followup.send(embed=create_error_embed("Invalid Number"), ephemeral=True)
        except Exception as e:
            log.exception(f"Coverage purchase error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)


class InsurancePaymentView(ui.View):
    def __init__(self, coverage_id: int):
        super().__init__(timeout=600)
        self.coverage_id = coverage_id
    
    @ui.button(label="Verify Payment", style=discord.ButtonStyle.success, emoji=config.EMOJI_MONEY)
    async def verify(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            coverage = await db.get_coverage(self.coverage_id)
            if not coverage or coverage['status'] != 'pending':
                await interaction.followup.send(embed=create_error_embed("Coverage Unavailable"), ephemeral=True)
                return
            
            key_data = await db.get_user_api_key(interaction.user.id)
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            
            policy = await db.get_policy(coverage['policy_id'])
            provider = await db.get_provider_by_id(policy['provider_id'])
            
            torn_api = get_torn_api()
            payment = await torn_api.verify_payment(
                api_key, provider['torn_user_id'], 'cash', coverage['premium_paid']
            )
            
            if not payment:
                await interaction.followup.send(embed=create_error_embed("Payment Not Found", "Send premium and try again"), ephemeral=True)
                return
            
            await db.activate_coverage(self.coverage_id)
            await db.log_audit(interaction.user.id, "coverage_activated", "insurance", self.coverage_id)
            
            await interaction.followup.send(embed=create_success_embed(
                "Coverage Activated!",
                f"You're now covered for {coverage['xanax_covered']} Xanax.\n"
                f"Payout if OD: ${coverage['payout_amount']:,}\n"
                f"Expires: <t:{int(coverage['expires_at'].timestamp())}:R>"
            ), ephemeral=True)
        except Exception as e:
            log.exception(f"Payment verification error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)


class ProviderClaimsView(ui.View):
    def __init__(self, claims: list):
        super().__init__(timeout=300)
        if claims:
            self.add_item(ClaimSelect(claims))


class ClaimSelect(ui.Select):
    def __init__(self, claims: list):
        options = [discord.SelectOption(label=f"Claim #{c['claim_id']}", description=f"{payout_items_to_human(c.get('payout_items') or [])[:70]} - {c['status']}", value=str(c['claim_id'])) for c in claims[:25]]
        super().__init__(placeholder="Select a claim...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        claim_id = int(self.values[0])
        await interaction.response.send_message(f"Managing Claim #{claim_id}", view=ClaimManageView(claim_id), ephemeral=True)


class SetClaimPayoutModal(ui.Modal, title="Set Claim Payout"):
    payout_items = ui.TextInput(
        label="Payout (items)",
        placeholder="xanax=4, edvd=6, ecstasy=1",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=300,
    )

    def __init__(self, claim_id: int, initial_value: str = ""):
        super().__init__()
        self.claim_id = claim_id
        if initial_value:
            self.payout_items.default = initial_value[:300]

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            parsed = parse_payout_string(self.payout_items.value)
            if not parsed:
                raise PayoutParseError("Payout cannot be empty. Example: xanax=4, edvd=6, ecstasy=1")
            db = get_database()
            await db.set_claim_payout_items(self.claim_id, parsed, resolved_by=interaction.user.id)
            await db.log_audit(interaction.user.id, "claim_payout_set", "claim", self.claim_id, {"payout_items": parsed})
            await interaction.followup.send(
                embed=create_success_embed("Payout Set", _payout_line(parsed) + "\nNow click **Verify Payout** after sending items."),
                ephemeral=True,
            )
        except PayoutParseError as err:
            await interaction.followup.send(
                embed=create_error_embed(
                    "Invalid Payout String",
                    f"{err}\nExamples: `xanax=4, edvd=6, ecstasy=1` or `xanax:4,edvd:6`",
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Set claim payout failed: %s", exc)
            await interaction.followup.send(embed=create_error_embed("Failed to Set Payout", str(exc)), ephemeral=True)


class ClaimManageView(ui.View):
    def __init__(self, claim_id: int):
        super().__init__(timeout=300)
        self.claim_id = claim_id

    @ui.button(label="Set Payout", style=discord.ButtonStyle.primary, emoji=config.EMOJI_PILL)
    async def set_payout(self, interaction: discord.Interaction, button: ui.Button):
        db = get_database()
        claim = await db.get_claim(self.claim_id)
        if not claim:
            await interaction.response.send_message(embed=create_error_embed("Claim Not Found"), ephemeral=True)
            return

        policy = await db.get_policy(claim['policy_id'])
        seed_items = claim.get('payout_items') or (policy.get('payout_items') if policy else []) or []
        await interaction.response.send_modal(SetClaimPayoutModal(self.claim_id, payout_items_to_string(seed_items)))

    @ui.button(label="Verify Payout", style=discord.ButtonStyle.success, emoji=config.EMOJI_CHECK)
    async def verify_payout(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            claim = await db.get_claim(self.claim_id)
            if not claim:
                await interaction.followup.send(embed=create_error_embed("Claim Not Found"), ephemeral=True)
                return
            payout_items = claim.get("payout_items") or []
            if not payout_items:
                await interaction.followup.send(embed=create_error_embed("Payout Not Set", "Use **Set Payout** first."), ephemeral=True)
                return

            key_data = await db.get_user_api_key(interaction.user.id)
            if not key_data:
                await interaction.followup.send(embed=create_error_embed("API Key Required", "Register your API key first."), ephemeral=True)
                return

            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            torn_api = get_torn_api()

            candidate_logs = await torn_api.get_user_logs(api_key, limit=5)

            resolver = ItemResolver(db.pool)
            resolved_payout_items = []
            for payout_item in payout_items:
                raw_name = str(payout_item.get("item") or "").strip()
                item_id = await resolver.resolve_item_id(raw_name)
                if not item_id:
                    await interaction.followup.send(
                        embed=create_error_embed(
                            "Unknown Item",
                            f"Could not resolve '{raw_name}' to a Torn item id. Run /refresh_item_icons or add an alias.",
                        ),
                        ephemeral=True,
                    )
                    return
                resolved_payout_items.append(
                    {"item": raw_name, "item_id": item_id, "qty": int(payout_item.get("qty") or 0)}
                )

            recipient_torn_id = int(claim['user_torn_id'])
            matched_log = None
            for entry in candidate_logs:
                details_id = (entry.get("details") or {}).get("id")
                if details_id != 4102:
                    continue

                counterparty = _extract_counterparty_torn_id(entry)
                if counterparty != recipient_torn_id:
                    continue

                if all(_count_item_qty_by_id(entry, i['item_id']) >= i['qty'] for i in resolved_payout_items):
                    matched_log = entry
                    break

            if not matched_log:
                await interaction.followup.send(embed=create_error_embed("Payout Verification Failed", "No matching payout log found yet. Send items and retry."), ephemeral=True)
                return

            await db.mark_claim_paid_with_log(
                self.claim_id,
                int(_extract_log_id(matched_log) or 0),
                int(matched_log.get('timestamp') or 0),
                json.dumps(matched_log, ensure_ascii=False),
            )
            await db.log_audit(interaction.user.id, "claim_paid", "claim", self.claim_id)
            await interaction.followup.send(embed=create_success_embed("Claim Paid", _payout_line(payout_items)), ephemeral=True)
        except TornAPIPermissionError:
            await interaction.followup.send(
                embed=create_error_embed(
                    "Verification Error",
                    "Your Torn API key lacks permission to read item logs (cat=85). Update key permissions and try again.",
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Verify payout failed: %s", exc)
            await interaction.followup.send(embed=create_error_embed("Verification Error", str(exc)), ephemeral=True)

    @ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji=config.EMOJI_CROSS)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(DenyClaimModal(self.claim_id))


class DenyClaimModal(ui.Modal, title="Deny Claim"):
    reason = ui.TextInput(label="Denial Reason", placeholder="Why is this claim being denied?", style=discord.TextStyle.paragraph)
    
    def __init__(self, claim_id: int):
        super().__init__()
        self.claim_id = claim_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        await db.reject_claim(self.claim_id, interaction.user.id, self.reason.value)
        await db.log_audit(interaction.user.id, "claim_denied", "claim", self.claim_id, {"reason": self.reason.value})
        await interaction.followup.send(embed=create_success_embed("Claim Denied"), ephemeral=True)


# ============================================================================
# RAFFLE VIEWS
# ============================================================================

class RaffleView(ui.View):
    def __init__(self, raffle_id: int):
        super().__init__(timeout=None)
        self.raffle_id = raffle_id
    
    @ui.button(label="Buy Tickets", style=discord.ButtonStyle.success, emoji=config.EMOJI_TICKET, custom_id="raffle_buy")
    async def buy(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(BuyTicketsModal(self.raffle_id))
    
    @ui.button(label="My Tickets", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_INFO, custom_id="raffle_info")
    async def info(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        entry = await db.get_raffle_entry(self.raffle_id, interaction.user.id)
        if not entry:
            await interaction.followup.send(embed=create_info_embed("No Tickets", "You haven't entered this raffle"), ephemeral=True)
            return
        
        status = "paid" if entry.get('payment_verified') else "reserved"
        info = f"**Tickets:** {entry['num_tickets']}\n**Status:** {status.title()}"
        if status == 'reserved' and entry.get('reserved_until'):
            info += f"\n**Expires:** <t:{int(entry['reserved_until'].timestamp())}:R>"
        
        await interaction.followup.send(embed=create_info_embed("Your Entry", info), ephemeral=True)


class BuyTicketsModal(ui.Modal, title="Buy Raffle Tickets"):
    ticket_count = ui.TextInput(label="Number of Tickets", placeholder="How many tickets?", min_length=1, max_length=4)
    
    def __init__(self, raffle_id: int):
        super().__init__()
        self.raffle_id = raffle_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            tickets = int(self.ticket_count.value)
            db = get_database()
            result = await RaffleService(db).reserve_tickets(
                raffle_id=self.raffle_id,
                user_id=interaction.user.id,
                tickets=tickets,
            )
            raffle = result["raffle"]
            reserved_until = result["reserved_until"]
            total_items = tickets * raffle['ticket_price']
            item_label = "Xanax" if raffle['ticket_payment_type'] == 'xanax' else "Erotic DVD"
            creator_key = await db.get_user_api_key(raffle['creator_discord_id'])
            creator_torn_id = creator_key['torn_user_id'] if creator_key else None
            recipient_line = f"**Send to:** <@{raffle['creator_discord_id']}>"
            if creator_torn_id:
                recipient_line += f" (Torn: `{creator_torn_id}`)"
            else:
                recipient_line += "\nCreator has not registered API key; contact creator."

            await interaction.followup.send(
                embed=create_info_embed(
                    "Tickets Reserved",
                    f"**Tickets:** {tickets}\n"
                    f"**Cost:** {total_items} {item_label}\n"
                    f"{recipient_line}\n"
                    f"**Expires:** <t:{int(reserved_until.timestamp())}:R>",
                ),
                view=RafflePaymentView(self.raffle_id),
                ephemeral=True,
            )
        except ValueError:
            await interaction.followup.send(embed=create_error_embed("Invalid Number"), ephemeral=True)
        except NotFound:
            await interaction.followup.send(embed=create_error_embed("Raffle Unavailable"), ephemeral=True)
        except InvalidInput:
            await interaction.followup.send(embed=create_error_embed("API Key Required"), ephemeral=True)
        except BusinessRuleViolation as exc:
            message = str(exc)
            if message.startswith("Max"):
                await interaction.followup.send(embed=create_error_embed("Exceeds Limit", message), ephemeral=True)
            elif message.startswith("Only"):
                await interaction.followup.send(embed=create_error_embed("Not Enough Tickets", message), ephemeral=True)
            else:
                await interaction.followup.send(embed=create_error_embed("Invalid Amount"), ephemeral=True)
        except Exception as e:
            log.exception("Ticket purchase error: %s", e)
            await interaction.followup.send(embed=create_error_embed("Error", "Unable to reserve tickets"), ephemeral=True)



class RafflePaymentView(ui.View):
    def __init__(self, raffle_id: int):
        super().__init__(timeout=600)
        self.raffle_id = raffle_id
    
    @ui.button(label="Verify Payment", style=discord.ButtonStyle.success, emoji=config.EMOJI_PILL)
    async def verify(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            entry = await db.get_raffle_entry(self.raffle_id, interaction.user.id)
            if not entry or entry.get('payment_verified'):
                await interaction.followup.send(embed=create_error_embed("Entry Unavailable"), ephemeral=True)
                return
            
            raffle = await db.get_raffle(self.raffle_id)
            key_data = await db.get_user_api_key(interaction.user.id)
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            
            torn_api = get_torn_api()
            creator_key = await db.get_user_api_key(raffle['creator_discord_id'])
            creator_torn_id = creator_key['torn_user_id'] if creator_key else None
            if not creator_torn_id:
                await interaction.followup.send(embed=create_error_embed("Creator Not Configured", "Creator has not registered API key; contact creator."), ephemeral=True)
                return

            amount = entry['num_tickets'] * raffle['ticket_price']
            if raffle['ticket_payment_type'] == 'xanax':
                payment = await torn_api.verify_xanax_payment(api_key, creator_torn_id, amount)
            elif raffle['ticket_payment_type'] == 'erotic_dvd':
                payment = await torn_api.verify_dvd_payment(api_key, creator_torn_id, amount)
            else:
                await interaction.followup.send(embed=create_error_embed("Unsupported Payment Type", raffle['ticket_payment_type']), ephemeral=True)
                return
            
            if not payment:
                item_label = "Xanax" if raffle['ticket_payment_type'] == 'xanax' else "Erotic DVD"
                await interaction.followup.send(embed=create_error_embed("Payment Not Found", f"Send {item_label} and try again"), ephemeral=True)
                return
            
            await db.verify_raffle_payment(self.raffle_id, interaction.user.id)
            await db.log_audit(interaction.user.id, "raffle_entry_verified", "raffle", self.raffle_id)
            
            await interaction.followup.send(embed=create_success_embed(
                "Entry Confirmed!",
                f"You have {entry['num_tickets']} tickets in the raffle. Good luck!"
            ), ephemeral=True)
            
            # Update raffle embed
            settings = await db.get_guild_settings(interaction.guild.id)
            if raffle.get('announcement_message_id'):
                await update_raffle_embed(self.raffle_id, interaction.guild, settings)
        except Exception as e:
            log.exception(f"Raffle payment error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)


# ============================================================================
# ADMIN VIEWS
# ============================================================================

class SetupView(ui.View):
    """Admin setup shortcuts view."""

    def __init__(self, guild_id: Optional[int] = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
    
    @ui.button(label="Set Host Role", style=discord.ButtonStyle.primary, emoji="👥")
    async def host_role(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Select host role:", view=RoleSelectView("host99k_role_id"), ephemeral=True)
    
    @ui.button(label="Set Insurer Role", style=discord.ButtonStyle.primary, emoji=config.EMOJI_SHIELD)
    async def insurer_role(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Select insurer role:", view=RoleSelectView("insurer_role_id"), ephemeral=True)
    
    @ui.button(label="Set 99k Channel", style=discord.ButtonStyle.secondary, emoji="#️⃣")
    async def jump_channel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Select 99k channel:", view=ChannelSelectView("jump_99k_channel_id"), ephemeral=True)
    
    @ui.button(label="Set Insurance Channel", style=discord.ButtonStyle.secondary, emoji="#️⃣")
    async def insurance_channel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Select insurance channel:", view=ChannelSelectView("insurance_channel_id"), ephemeral=True)
    
    @ui.button(label="Set Raffle Channel", style=discord.ButtonStyle.secondary, emoji="#️⃣")
    async def raffle_channel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Select raffle channel:", view=ChannelSelectView("raffle_channel_id"), ephemeral=True)


class RoleSelectView(ui.View):
    def __init__(self, setting_key: str):
        super().__init__(timeout=60)
        self.add_item(RoleSelectMenu(setting_key))


class RoleSelectMenu(ui.RoleSelect):
    def __init__(self, setting_key: str):
        super().__init__(placeholder="Select a role...")
        self.setting_key = setting_key
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        await db.update_guild_settings(interaction.guild.id, **{self.setting_key: self.values[0].id})
        await db.log_audit(interaction.user.id, f"set_{self.setting_key}", "guild", interaction.guild.id)
        await interaction.followup.send(embed=create_success_embed("Role Updated", f"Set to {self.values[0].mention}"), ephemeral=True)


class ChannelSelectView(ui.View):
    def __init__(self, setting_key: str):
        super().__init__(timeout=60)
        self.add_item(ChannelSelectMenu(setting_key))


class ChannelSelectMenu(ui.ChannelSelect):
    def __init__(self, setting_key: str):
        super().__init__(placeholder="Select a channel...", channel_types=[discord.ChannelType.text, discord.ChannelType.news])
        self.setting_key = setting_key
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        selected = self.values[0]
        resolved = await resolve_guild_channel(interaction, selected)
        if resolved is None:
            await interaction.followup.send(
                embed=create_error_embed(
                    "Channel unavailable",
                    "I couldn't resolve that channel in this server. Please select another channel.",
                ),
                ephemeral=True,
            )
            return

        mention = getattr(resolved, "mention", f"<#{resolved.id}>")
        db = get_database()
        await db.update_guild_settings(interaction.guild.id, **{self.setting_key: resolved.id})
        await db.log_audit(interaction.user.id, f"set_{self.setting_key}", "guild", interaction.guild.id)
        await interaction.followup.send(embed=create_success_embed("Channel Updated", f"Set to {mention}"), ephemeral=True)


class AdminDashboardView(ui.View):
    def __init__(self, session_options: list):
        super().__init__(timeout=300)
        if session_options:
            self.add_item(SessionSelectMenu(session_options))


class SessionSelectMenu(ui.Select):
    def __init__(self, options: list):
        super().__init__(placeholder="Select a session...", options=options[:25])
    
    async def callback(self, interaction: discord.Interaction):
        session_id = int(self.values[0])
        await interaction.response.send_message(f"Managing Session #{session_id}", view=HostControlView(session_id), ephemeral=True)



# ============================================================================
# HELPER FUNCTIONS
# ============================================================================



async def build_jump_status_panel_embed(session_id: int) -> discord.Embed:
    db = get_database()
    session = await db.get_jump_session(session_id)
    if not session:
        return create_error_embed("Session Not Found")

    signups = await db.get_session_signups(session_id)
    participant_ids = [int(session["host_discord_id"])]
    participant_ids.extend(int(s["discord_id"]) for s in signups)

    monitor = get_jump_monitor()
    status_map = monitor.get_status(session_id)
    now = datetime.utcnow()

    lines = []
    for discord_id in participant_ids:
        status = status_map.get(discord_id, {})
        ready = bool(status.get("ready_bool"))
        light = "✅" if ready else "❌"
        od_light = "🟧" if bool(status.get("od_any")) else ""

        if status.get("no_api_key"):
            energy_label = "No API Key"
            drug_label = "N/A"
            booster_label = "N/A"
        else:
            energy_value = status.get("energy_current")
            energy_label = f"{energy_value}/1000" if isinstance(energy_value, int) else "?/1000"
            drug_value = status.get("drug_cd")
            booster_value = status.get("booster_cd")
            drug_label = str(drug_value) if isinstance(drug_value, int) else "?"
            booster_label = str(booster_value) if isinstance(booster_value, int) else "?"

        updated_at = status.get("updated_at")
        if updated_at:
            if getattr(updated_at, "tzinfo", None) is not None:
                updated_at = updated_at.replace(tzinfo=None)
            age_seconds = int((now - updated_at).total_seconds())
            updated_label = f"{max(age_seconds, 0)}s ago"
        else:
            updated_label = "pending"

        lines.append(
            f"{light} {od_light} <@{discord_id}> • E {energy_label} • Drug {drug_label}s • Booster {booster_label}s • Updated {updated_label}"
        )

    embed = create_info_embed(
        "99k Jump Live Status Panel",
        "\n".join(lines) if lines else "No participants yet.",
    )
    starts_at = monitor.get_start_countdown(session_id)
    if starts_at:
        starts_unix = int(starts_at.timestamp())
        embed.add_field(name="Start", value=f"Jump starts in <t:{starts_unix}:R> (host initiated)", inline=False)
    embed.set_footer(text=f"Session #{session_id} • Auto-refresh every 10s")
    return embed


def _parse_start_delay_seconds(raw_value: str) -> Optional[int]:
    value = (raw_value or "").strip()
    if not value:
        return None
    if ":" in value:
        parts = value.split(":", 1)
        try:
            minutes = int(parts[0])
            seconds = int(parts[1])
        except ValueError:
            return None
        if minutes < 0 or seconds < 0 or seconds > 59:
            return None
        return minutes * 60 + seconds
    try:
        minutes = int(value)
    except ValueError:
        return None
    if minutes < 0:
        return None
    return minutes * 60


async def _start_jump_countdown(interaction: discord.Interaction, session_id: int, delay_seconds: int) -> None:
    db = get_database()
    session = await db.get_jump_session(session_id)
    if not session:
        await interaction.followup.send(embed=create_error_embed("Session Not Found"), ephemeral=True)
        return

    starts_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    mm, ss = divmod(delay_seconds, 60)
    countdown_text = f"{mm:02d}:{ss:02d}"

    signups = await db.get_session_signups(session_id)
    participant_ids = {int(s["discord_id"]) for s in signups}
    participant_ids.add(int(session["host_discord_id"]))

    announcement_url = ""
    if interaction.guild and session.get("announcement_channel_id") and session.get("announcement_message_id"):
        announcement_url = f"https://discord.com/channels/{interaction.guild.id}/{session['announcement_channel_id']}/{session['announcement_message_id']}"

    dm_text = (
        f"99k Happy Jump starting in {countdown_text}\n"
        f"Jump #{session_id}\n"
        f"{announcement_url}".strip()
    )

    sent_count = 0
    for discord_id in participant_ids:
        user = interaction.client.get_user(discord_id)
        if user is None:
            try:
                user = await interaction.client.fetch_user(discord_id)
            except Exception:
                user = None
        if not user:
            continue
        try:
            await user.send(dm_text)
            sent_count += 1
        except Exception:
            continue

    monitor = get_jump_monitor()
    monitor.set_start_countdown(session_id, starts_at)

    if interaction.guild and session.get("announcement_channel_id") and session.get("announcement_message_id"):
        try:
            channel = interaction.guild.get_channel(int(session["announcement_channel_id"]))
            if channel:
                announcement_message = await channel.fetch_message(session["announcement_message_id"])
                base_embed = announcement_message.embeds[0] if announcement_message.embeds else None
                if base_embed:
                    embed = base_embed.copy()
                    embed.add_field(name="Start", value=f"Jump starts in {countdown_text} (host initiated)", inline=False)
                    await announcement_message.edit(embed=embed, view=JumpSessionView(session_id))
        except Exception:
            pass

    await db.log_audit(interaction.user.id, "jump_start_initiated", "session", session_id, {"delay_seconds": delay_seconds, "dm_sent": sent_count})
    await interaction.followup.send(
        embed=create_success_embed("Jump Start Announced", f"DM sent to {sent_count} participant(s). Start in {countdown_text}."),
        ephemeral=True,
    )

async def update_jump_embed(session_id: int, message: discord.Message) -> str:
    db = get_database()
    session = await db.get_jump_session(session_id)
    if not session:
        return "error"

    signups = await db.get_session_signups(session_id)
    readiness = await db.get_session_readiness(session_id)
    embed = create_jump_session_embed(session, signups, readiness)

    try:
        try:
            await message.edit(embed=embed, view=JumpSessionView(session_id))
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Jump message edit failed guild=%s channel=%s message=%s", getattr(guild, "id", None), getattr(channel, "id", None), session.get("announcement_message_id"))
        return "ok"
    except discord.Forbidden:
        log.warning(
            "Update embed missing access guild_id=%s channel_id=%s message_id=%s session_id=%s",
            getattr(getattr(message, "guild", None), "id", None),
            getattr(getattr(message, "channel", None), "id", None),
            getattr(message, "id", None),
            session_id,
        )
    except discord.HTTPException:
        log.warning(
            "Update embed http error channel_id=%s message_id=%s session_id=%s",
            getattr(getattr(message, "channel", None), "id", None),
            getattr(message, "id", None),
            session_id,
            exc_info=True,
        )
    except Exception as e:
        log.exception(f"Update embed error: {e}")
        return "error"

    try:
        channel_id = session.get('announcement_channel_id')
        announcement_message_id = session.get('announcement_message_id')
        guild = getattr(message, 'guild', None)
        if not (guild and channel_id and announcement_message_id):
            return "missing_access"

        channel = guild.get_channel(channel_id)
        if not channel:
            channel = await guild.fetch_channel(channel_id)

        announcement_message = await channel.fetch_message(announcement_message_id)
        try:
            await announcement_message.edit(embed=embed, view=JumpSessionView(session_id))
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Jump announcement edit failed guild=%s channel=%s message=%s", getattr(guild, "id", None), settings.get("jump_99k_channel_id"), session.get("announcement_message_id"))
        return "ok"
    except discord.Forbidden:
        log.warning(
            "Update embed missing access guild_id=%s channel_id=%s message_id=%s session_id=%s",
            getattr(getattr(message, "guild", None), "id", None),
            session.get('announcement_channel_id'),
            session.get('announcement_message_id'),
            session_id,
        )
        return "missing_access"
    except discord.HTTPException:
        log.warning(
            "Update embed fallback http error channel_id=%s message_id=%s session_id=%s",
            session.get('announcement_channel_id'),
            session.get('announcement_message_id'),
            session_id,
            exc_info=True,
        )
        return "error"
    except Exception as e:
        log.exception(f"Update embed fallback error: {e}")
        return "error"


async def update_raffle_embed(raffle_id: int, guild: discord.Guild, settings: dict):
    try:
        db = get_database()
        raffle = await db.get_raffle(raffle_id)
        if not raffle:
            return
        entries = await db.get_raffle_entries(raffle_id)
        embed = create_raffle_embed(raffle, entries)
        
        channel_id = settings.get('raffle_channel_id')
        if channel_id and raffle.get('announcement_message_id'):
            channel = guild.get_channel(channel_id)
            if channel:
                message = await channel.fetch_message(raffle['announcement_message_id'])
                try:
                    await message.edit(embed=embed, view=RaffleView(raffle_id))
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("Raffle message edit failed guild=%s channel=%s message=%s", getattr(guild, "id", None), getattr(channel, "id", None), raffle.get("announcement_message_id"))
    except Exception as e:
        log.exception(f"Update raffle embed error: {e}")


async def refresh_session_readiness(session_id: int):
    try:
        db = get_database()
        signups = await db.get_session_signups(session_id)
        
        for signup in signups:
            try:
                key_data = await db.get_user_api_key(signup['discord_id'])
                if not key_data:
                    continue
                
                security = get_security_manager()
                api_key = security.decrypt(key_data['encrypted_key'])
                torn_api = get_torn_api()
                
                user_data = await torn_api.get_user_data(api_key)
                energy = user_data.get('bars', {}).get('energy', {})
                drug_cd = user_data.get('cooldowns', {}).get('drug', 0)
                
                status = "ready" if energy.get('current', 0) >= config.MIN_ENERGY_REQUIREMENT and drug_cd == 0 else "not_ready"
                
                await db.update_readiness(
                    session_id, signup['discord_id'],
                    energy.get('current', 0), energy.get('maximum', 150),
                    drug_cd, status
                )
            except Exception as e:
                log.warning(f"Failed to refresh readiness for {signup['discord_id']}: {e}")
    except Exception as e:
        log.exception(f"Readiness refresh error: {e}")
