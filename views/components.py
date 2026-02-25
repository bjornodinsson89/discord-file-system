"""Discord views for all Happy Jumper features."""

import asyncio
import discord
from discord import ui
import logging
import json
import re
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta, timezone
from utils import get_database, get_security_manager, get_torn_api, require_api_key, has_api_key
from utils.discord_channels import resolve_guild_channel
from utils.torn_api import TornAPIError, TornAPIPermissionError
from utils.item_resolver import ItemResolver
from utils.embeds import *
from utils.payouts import parse_payout_string, payout_items_to_human, payout_items_to_string, PayoutParseError
from services import JumpService, RaffleService, DomainError, AlreadyExists, NotFound, InvalidInput, BusinessRuleViolation, PaymentReceiptService
from services.jump_monitor import get_jump_monitor
from utils.tasks import TaskSupervisor, supervise
from repositories.jumps import JumpsRepository
from repositories.users import UsersRepository
from repositories.audit import AuditRepository
from repositories.raffles import RafflesRepository
from repositories.insurance import InsuranceRepository
from repositories.applications import ApplicationsRepository
from constants.insurers import INSURER_CATEGORIES
from utils.guild_settings_repository import GuildSettingsRepository
from services.raffle_payment import RafflePaymentService
import config
from .timezone_picker import TimezonePromptView

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
        self.add_item(
            ui.Button(
                label="Full Torn API Disclaimer",
                style=discord.ButtonStyle.link,
                url=config.TORN_API_DISCLAIMER_URL,
            )
        )

    @ui.button(label="Enter API Key", style=discord.ButtonStyle.primary, emoji=config.EMOJI_LOCK)
    async def enter_key(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ApiKeyModal())

    @ui.button(label="View Guide", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_INFO)
    async def guide(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message(embed=create_api_key_guide_embed(), ephemeral=True)



class ApiKeyModal(ui.Modal, title="Register Torn API Key"):
    api_key = ui.TextInput(label="Torn API key", placeholder="Paste a Full Access API key, or tap Create API Key to generate a scoped key for this bot.", min_length=16, max_length=16)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            api_key = self.api_key.value.strip()

            torn_api = get_torn_api()
            discord_id_api, torn_id, torn_name, _perms = await torn_api.validate_api_key(api_key)

            if discord_id_api != interaction.user.id:
                await interaction.followup.send(embed=create_error_embed(
                    "Discord ID Mismatch", f"Key belongs to Discord user {discord_id_api}"), ephemeral=True)
                return

            security = get_security_manager()
            encrypted = security.encrypt(api_key)

            db = get_database()
            users_repo = UsersRepository(db.pool)
            await users_repo.upsert_user_api_key(
                discord_id=interaction.user.id,
                torn_user_id=torn_id,
                torn_name=torn_name,
                encrypted_key=encrypted,
                timezone_name=None,
            )
            try:
                await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="api_key_registered", target_type="user", target_id=interaction.user.id, payload={"torn_id": torn_id}, guild_id=interaction.guild_id, source="views/components.py:ApiKeyModal.on_submit")
            except Exception:
                log.exception("Failed to write api_key_registered audit log")

            await interaction.followup.send(embed=create_success_embed(
                "API Key Registered", f"Torn ID: `{torn_id}`"), ephemeral=True)
            saved_row = await users_repo.get_user_api_key(interaction.user.id)
            if not str((saved_row or {}).get("timezone_name") or "").strip():
                await interaction.followup.send(
                    "Your timezone isn’t set. Set it to ensure 99k start times are correct.",
                    view=TimezonePromptView(),
                    ephemeral=True,
                )
        except TornAPIPermissionError as e:
            await interaction.followup.send(embed=create_error_embed("Insufficient Permissions", str(e)), ephemeral=True)
        except TornAPIError as e:
            message = str(e)
            lowered = message.lower()
            if any(token in lowered for token in ("timed out", "timeout", "cloudflare", "522", "unreachable")):
                message = f"{message}\nTorn API server may be down. Please try again in a minute."
            await interaction.followup.send(embed=create_error_embed("Validation Failed", message), ephemeral=True)
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
        await UsersRepository(db.pool).delete_user_api_key(interaction.user.id)
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="api_key_removed", target_type="user", target_id=interaction.user.id, payload={}, guild_id=interaction.guild_id, source="views/components.py:ConfirmRemoveKeyView.confirm")
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
    _status_panel_tasks: dict[tuple[int, int], TaskSupervisor] = {}

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
            await require_api_key(interaction, get_database(), "join a 99k jump")
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
            signup = await JumpsRepository(db.pool).get_signup(self.session_id, interaction.user.id)
            if not signup:
                await interaction.followup.send(embed=create_error_embed("Not Signed Up"), ephemeral=True)
                return
            if signup.get('payment_verified') is True:
                await interaction.followup.send(embed=create_warning_embed("Payment Verified", "Contact host to cancel"), ephemeral=True)
                return
            
            await JumpsRepository(db.pool).cancel_signup(session_id=self.session_id, discord_id=interaction.user.id)
            await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="jump_leave", target_type="session", target_id=self.session_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:JumpSessionView.leave_jump")
            
            # Promote from waitlist
            next_user = None
            if next_user:
                settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
                timeout = settings.get('reservation_timeout_minutes', config.DEFAULT_RESERVATION_TIMEOUT)
                reserved_until = datetime.utcnow() + timedelta(minutes=timeout)
                await JumpsRepository(db.pool).create_or_restore_signup(session_id=self.session_id, guild_id=interaction.guild.id, discord_id=next_user['discord_id'], torn_user_id=next_user['torn_user_id'])
            
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
        existing_task = self._status_panel_tasks.pop(key, None)
        if existing_task is not None:
            await existing_task.stop()

        self._status_panel_tasks[key] = supervise(
            name=f"status_panel:{interaction.user.id}:{self.session_id}",
            coro_factory=lambda: self._run_status_panel_refresh(interaction),
            restart=True,
            backoff=(1, 2, 5, 10),
            logger=log,
        )

    @ui.button(label="Start Jump", style=discord.ButtonStyle.primary, emoji="🚀", custom_id="jump_start")
    async def start_jump(self, interaction: discord.Interaction, button: ui.Button):
        db = get_database()
        session = await JumpsRepository(db.pool).get_session(self.session_id)
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
                session = await JumpsRepository(db.pool).get_session(self.session_id)
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
                raise

    @ui.button(label="Session Info", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_INFO, custom_id="jump_info")
    async def info(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        session = await JumpsRepository(db.pool).get_session(self.session_id)
        if not session:
            await interaction.followup.send(embed=create_error_embed("Session Not Found"), ephemeral=True)
            return
        
        signups = await JumpsRepository(db.pool).list_signups(self.session_id)
        user_signup = next((s for s in signups if s['discord_id'] == interaction.user.id), None)
        waitlist = []
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
            await require_api_key(interaction, get_database(), "join the waitlist")
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
            signup = await JumpsRepository(db.pool).get_signup(self.session_id, interaction.user.id)
            if not signup:
                await interaction.followup.send(embed=create_error_embed("Not Signed Up"), ephemeral=True)
                return
            if signup.get('payment_verified') is True:
                await interaction.followup.send(embed=create_warning_embed("Already Verified"), ephemeral=True)
                return
            
            session = await JumpsRepository(db.pool).get_session(self.session_id)
            if not await require_api_key(interaction, db, "verify your payment"):
                return
            key_data = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)

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
            receipts = PaymentReceiptService(db.pool)
            receipt_id = await receipts.create_and_verify(
                featureType="jump_99k",
                featureRefId=self.session_id,
                payer_discord_id=interaction.user.id,
                payer_torn_id=key_data.get('torn_user_id'),
                payee_discord_id=session.get('host_discord_id'),
                payee_torn_id=session.get('host_torn_id'),
                amount=session['payment_amount'],
                currency_type=session['payment_type'],
                metadata=payment,
            )
            await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="payment_verified", target_type="session", target_id=self.session_id, payload=payment, guild_id=interaction.guild_id, source="views/components.py:ManualPaymentVerifyButton.verify")
            get_jump_monitor().mark_needs_refresh(self.session_id)
            
            # Try to update embed
            settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
            channel_id = settings.get('jump_99k_channel_id')
            if channel_id and session.get('announce_message_id'):
                try:
                    channel = interaction.guild.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(session['announce_message_id'])
                        await update_jump_embed(self.session_id, message)
                except Exception:
                    log.exception("Failed to refresh jump embed after payment verification session_id=%s", self.session_id)
            
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

    @ui.button(label="Request Insurance", style=discord.ButtonStyle.success, emoji=config.EMOJI_SHIELD)
    async def request_insurance(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.buyer_discord_id:
            await interaction.followup.send(embed=create_error_embed("Not Authorized"), ephemeral=True)
            return

        db = get_database()
        repo = JumpsRepository(db.pool)
        session = await repo.get_session(self.session_id)
        if not session or str(session.get("status", "")).lower() != "open":
            for child in self.children:
                child.disabled = True
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
            await interaction.followup.send(embed=create_error_embed("Session Closed", "This jump session is no longer open."), ephemeral=True)
            return

        settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
        insurance_channel_id = settings.get("insurance_channel_id")
        channel = interaction.guild.get_channel(int(insurance_channel_id)) if insurance_channel_id else None
        if channel is None and insurance_channel_id:
            try:
                fetched = await interaction.guild.fetch_channel(int(insurance_channel_id))
                if hasattr(fetched, "send"):
                    channel = fetched
            except Exception:
                channel = None
        if channel is None:
            await interaction.followup.send(
                embed=create_info_embed("Insurance Request Saved", "Insurance announcements are disabled or not configured. Your jump purchase remains valid."),
                ephemeral=True,
            )
            return

        content = f"{interaction.user.display_name} has requested insurance for their 99k Happy jump (Session #{self.session_id})."
        try:
            message = await channel.send(content=content, view=InsuranceClaimView(self.session_id, interaction.user.id))
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            await interaction.followup.send(
                embed=create_info_embed("Insurance Request Saved", "Insurance announcement channel is unavailable right now. Your jump purchase remains valid."),
                ephemeral=True,
            )
            return
        request_id = await repo.create_insurance_request(
            session_id=self.session_id,
            participant_discord_id=interaction.user.id,
            channel_id=int(channel.id),
            message_id=int(message.id),
        )
        await AuditRepository(db.pool).log_audit(
            actor_discord_id=interaction.user.id,
            action="jump_insurance_requested",
            target_type="session",
            target_id=self.session_id,
            payload={"insurance_message_id": message.id, "request_id": request_id},
            guild_id=interaction.guild_id,
            source="views/components.py:InsuranceOfferView.request_insurance",
        )
        await interaction.followup.send(embed=create_success_embed("Insurance Requested", "Your request was sent to insurers."), ephemeral=True)

    @ui.button(label="No Thanks", style=discord.ButtonStyle.secondary)
    async def no_thanks(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            log.exception("Failed to update 99k panel with jump countdown session_id=%s", session_id)
        await interaction.followup.send(embed=create_info_embed("No Problem", "You can request insurance later if needed."), ephemeral=True)


class InsuranceDecisionDMView(ui.View):
    def __init__(self, *, session_id: int, request_id: int, requester_discord_id: int, insurer_discord_id: int, insurer_torn_id: int, fee_text: str):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.request_id = request_id
        self.requester_discord_id = requester_discord_id
        self.insurer_discord_id = insurer_discord_id
        self.insurer_torn_id = insurer_torn_id
        self.fee_text = fee_text

    async def _disable_on_message(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @ui.button(label="Accept Insurance", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.requester_discord_id:
            await interaction.followup.send("This action is only for the requester.", ephemeral=True)
            return

        db = get_database()
        repo = JumpsRepository(db.pool)
        req = await repo.get_insurance_request(self.request_id)
        if not req or req.get("status") != "claimed":
            await self._disable_on_message(interaction)
            await interaction.followup.send("This insurance request is no longer claimable.", ephemeral=True)
            return

        await repo.set_insurance_request_status(request_id=self.request_id, status="accepted")
        await self._disable_on_message(interaction)

        insurer_key = await UsersRepository(db.pool).get_user_api_key(self.insurer_discord_id)
        insurer_torn_id = int((insurer_key or {}).get("torn_user_id") or self.insurer_torn_id or 0)
        insurer_name = f"Insurer {self.insurer_discord_id}"
        try:
            insurer_user = interaction.client.get_user(self.insurer_discord_id) or await interaction.client.fetch_user(self.insurer_discord_id)
            if insurer_user:
                insurer_name = insurer_user.display_name
        except Exception:
            pass

        await interaction.followup.send(
            f"Send {self.fee_text} to {insurer_name} [{insurer_torn_id}] in Torn, then press Verify Payment.",
            view=InsuranceFeeVerifyView(
                session_id=self.session_id,
                request_id=self.request_id,
                requester_discord_id=self.requester_discord_id,
                insurer_discord_id=self.insurer_discord_id,
                insurer_torn_id=insurer_torn_id,
                fee_text=self.fee_text,
            ),
            ephemeral=True,
        )

    @ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id != self.requester_discord_id:
            await interaction.followup.send("This action is only for the requester.", ephemeral=True)
            return

        db = get_database()
        repo = JumpsRepository(db.pool)
        await repo.set_insurance_request_status(request_id=self.request_id, status="declined")
        await self._disable_on_message(interaction)

        try:
            insurer = interaction.client.get_user(self.insurer_discord_id) or await interaction.client.fetch_user(self.insurer_discord_id)
            await insurer.send("User declined.")
        except Exception:
            pass
        await interaction.followup.send("Insurance request denied.", ephemeral=True)


class InsuranceFeeVerifyView(ui.View):
    def __init__(self, *, session_id: int, request_id: int, requester_discord_id: int, insurer_discord_id: int, insurer_torn_id: int, fee_text: str):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.request_id = request_id
        self.requester_discord_id = requester_discord_id
        self.insurer_discord_id = insurer_discord_id
        self.insurer_torn_id = insurer_torn_id
        self.fee_text = fee_text

    @ui.button(label="Verify Payment", style=discord.ButtonStyle.success)
    async def verify_payment(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if interaction.user.id != self.requester_discord_id:
                await interaction.followup.send("This action is only for the requester.", ephemeral=True)
                return

            db = get_database()
            repo = JumpsRepository(db.pool)
            req = await repo.get_insurance_request(self.request_id)
            if not req or req.get("status") not in {"accepted", "completed"}:
                for child in self.children:
                    child.disabled = True
                try:
                    await interaction.message.edit(view=self)
                except Exception:
                    pass
                await interaction.followup.send("This insurance request is no longer active.", ephemeral=True)
                return
            if req.get("status") == "completed":
                await interaction.followup.send("Insurance is already active ✅", ephemeral=True)
                return

            if not await require_api_key(interaction, db, "verify insurance payment"):
                return
            key_row = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
            encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")

            profile = await ApplicationsRepository(db.pool).get_insurer_profile(guild_id=interaction.guild_id, user_id=self.insurer_discord_id)
            pricing_text = str((profile or {}).get("pricing_text") or "")
            qty = 1
            if pricing_text:
                m = re.search(r"(\d+)", pricing_text)
                if m:
                    qty = int(m.group(1))

            security = get_security_manager()
            api_key = security.decrypt_api_key(encrypted_key)
            since_ts = int((req.get("accepted_at") or datetime.now(timezone.utc)).timestamp())

            try:
                payment = await get_torn_api().verify_item_payment(
                    api_key=api_key,
                    recipient_torn_id=int(self.insurer_torn_id),
                    required_item_id=config.XANAX_ITEM_ID,
                    amount=max(1, qty),
                    since_timestamp=since_ts,
                )
            except TornAPIError:
                await interaction.followup.send("Torn API may be down right now. Please try again in a minute.", ephemeral=True)
                return

            if not payment:
                await interaction.followup.send("Payment not found yet. Please try again shortly.", ephemeral=True)
                return

            await repo.mark_insurance_payment_verified(request_id=self.request_id)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

            await interaction.followup.send("Insurance active ✅", ephemeral=True)
            try:
                insurer = interaction.client.get_user(self.insurer_discord_id) or await interaction.client.fetch_user(self.insurer_discord_id)
                await insurer.send(f"Insurance purchased by {interaction.user.display_name} ✅")
            except Exception:
                pass
        except Exception:
            custom_id = str((interaction.data or {}).get("custom_id") or "")
            log.exception(
                "insurance verify_payment failed guild_id=%s jump_id=%s user_id=%s custom_id=%s",
                interaction.guild_id,
                self.session_id,
                interaction.user.id if interaction.user else None,
                custom_id,
            )
            await interaction.followup.send(
                "Payment verification hit an internal error (database schema mismatch). The admin has been notified. Try again in a minute.",
                ephemeral=True,
            )


class InsuranceClaimView(ui.View):
    def __init__(self, session_id: int, requester_discord_id: int):
        super().__init__(timeout=None)
        self.session_id = session_id
        self.requester_discord_id = requester_discord_id

    @ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🛡️")
    async def claim(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(embed=create_error_embed("Server Member Required"), ephemeral=True)
            return

        db = get_database()
        settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
        insurer_role_id = settings.get("insurer_role_id")
        has_insurer_role = bool(insurer_role_id and any(role.id == int(insurer_role_id) for role in interaction.user.roles))

        if not has_insurer_role and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(embed=create_error_embed("Not Authorized", "Only HJ_Insureance_provider can claim this request."), ephemeral=True)
            return

        repo = JumpsRepository(db.pool)
        req = await repo.get_insurance_request_for_signup(session_id=self.session_id, participant_discord_id=self.requester_discord_id)
        if not req or req.get("status") != "requested":
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await interaction.followup.send("This request is no longer available.", ephemeral=True)
            return

        ok = await repo.claim_insurance_request(request_id=int(req["id"]), claimed_by_discord_id=interaction.user.id)
        if not ok:
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await interaction.followup.send("Already claimed.", ephemeral=True)
            return

        for child in self.children:
            if isinstance(child, ui.Button):
                child.disabled = True
                if child.label == "Claim":
                    child.label = f"Claimed by {interaction.user.display_name[:40]}"
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        profile = await ApplicationsRepository(db.pool).get_insurer_profile(guild_id=interaction.guild_id, user_id=interaction.user.id)
        fee_text = str((profile or {}).get("pricing_text") or "provider fee")
        coverage_window = str((profile or {}).get("coverage_duration_minutes") or "?")

        embed = create_info_embed(
            "Insurance Info Card",
            f"Provider: {interaction.user.display_name}\nCoverage window: {coverage_window} minutes\nFee: {fee_text}\nOD detection is automated while in a jump session.",
        )
        image_url = (profile or {}).get("image_url")
        if image_url:
            embed.set_image(url=image_url)

        insurer_torn_id = 0
        insurer_key = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
        if insurer_key and insurer_key.get("torn_user_id"):
            insurer_torn_id = int(insurer_key["torn_user_id"])

        try:
            requester = interaction.guild.get_member(self.requester_discord_id) or await interaction.guild.fetch_member(self.requester_discord_id)
            await requester.send(
                embed=embed,
                view=InsuranceDecisionDMView(
                    session_id=self.session_id,
                    request_id=int(req["id"]),
                    requester_discord_id=self.requester_discord_id,
                    insurer_discord_id=interaction.user.id,
                    insurer_torn_id=insurer_torn_id,
                    fee_text=fee_text,
                ),
            )
        except Exception:
            await interaction.followup.send("Claimed, but could not DM requester.", ephemeral=True)
            return

        await interaction.followup.send("Insurance request claimed and card sent to requester.", ephemeral=True)
        await AuditRepository(db.pool).log_audit(
            actor_discord_id=interaction.user.id,
            action="jump_insurance_claimed",
            target_type="session",
            target_id=self.session_id,
            payload={"requester_discord_id": self.requester_discord_id},
            guild_id=interaction.guild_id,
            source="views/components.py:InsuranceClaimView.claim",
        )


class StartJumpCustomDelayModal(ui.Modal, title="Custom Jump Delay"):
    delay = ui.TextInput(label="Delay", placeholder="3:30", max_length=8)

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
        session = await JumpsRepository(db.pool).get_session(self.session_id)
        if session['host_discord_id'] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(embed=create_error_embed("Not Authorized"), ephemeral=True)
            return
        await JumpsRepository(db.pool).update_session_status(self.session_id, "locked")
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="session_locked", target_type="session", target_id=self.session_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:HostControlView.lock")
        await interaction.followup.send(embed=create_success_embed("Session Locked"), ephemeral=True)
    
    @ui.button(label="Complete Session", style=discord.ButtonStyle.success, emoji=config.EMOJI_CHECK)
    async def complete(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        session = await JumpsRepository(db.pool).get_session(self.session_id)
        if session['host_discord_id'] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(embed=create_error_embed("Not Authorized"), ephemeral=True)
            return
        service = JumpService(db, get_torn_api(), get_security_manager())
        await service.end_jump(session_id=self.session_id, status="completed")
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="session_completed", target_type="session", target_id=self.session_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:HostControlView.complete")
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
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="session_cancelled", target_type="session", target_id=self.session_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:HostControlView.cancel")
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
        session = await JumpsRepository(db.pool).get_session(self.session_id)
        await InsuranceRepository(db.pool).add_host_rating(session['host_discord_id'], interaction.user.id, self.session_id, self.rating)
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
    data = log_entry.get("data") or {}
    for key in ("receiver", "sender"):
        value = data.get(key)
        if isinstance(value, (int, str)) and str(value).isdigit():
            return int(value)
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
            user_id = int(provider.get("user_id") or provider.get("discord_id") or 0)
            title = provider.get("display_name") or (f"Insurer {user_id}" if user_id else "Insurer")
            coverage = (provider.get("coverage_summary") or "No coverage summary")
            desc = _trim_text(coverage.replace("\n", " "), 100)
            options.append(discord.SelectOption(
                label=title[:100],
                description=desc[:100],
                value=str(user_id),
            ))

        super().__init__(
            placeholder="Select an insurer to view card...",
            options=options,
            custom_id=f"insurers:select:{browser_view.page}",
        )
        self.browser_view = browser_view

    async def callback(self, interaction: discord.Interaction):
        insurer_user_id = int(self.values[0])
        card_view = InsurerCardView(
            guild_id=self.browser_view.guild_id,
            insurer_user_id=insurer_user_id,
            category=self.browser_view.category,
            parent_page=self.browser_view.page,
            timeout=self.browser_view.timeout,
        )
        embed = await card_view.build_embed(interaction.client)
        await interaction.response.edit_message(embed=embed, view=card_view)


class InsurerBrowserView(ui.View):
    def __init__(
        self,
        guild_id: int,
        category: Optional[str] = None,
        page: int = 0,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.category = category if category in INSURER_CATEGORIES else None
        self.page = max(0, page)
        self.providers: List[Dict] = []

    async def _load(self, client: discord.Client):
        db = get_database()
        rows = await ApplicationsRepository(db.pool).list_approved_insurers_for_browser(
            guild_id=self.guild_id,
            category=self.category,
        )
        for row in rows:
            discord_id = int(row.get("user_id") or row.get("discord_id") or 0)
            row["user_id"] = discord_id
            row["discord_id"] = discord_id
            row["display_name"] = row.get("display_name") or f"Discord User {discord_id}"
            if discord_id:
                user = client.get_user(discord_id)
                if user is None:
                    try:
                        user = await client.fetch_user(discord_id)
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

        filter_text = f"category={self.category or 'All insurers'}"

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
        insurer_user_id: int,
        category: Optional[str] = None,
        parent_page: int = 0,
        policy_page: int = 0,
        timeout: int = 300,
    ):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.insurer_user_id = insurer_user_id
        self.category = category if category in INSURER_CATEGORIES else None
        self.parent_page = max(0, parent_page)
        self.policy_page = max(0, policy_page)
        self.provider: Optional[Dict] = None
        self.policies: List[Dict] = []

    async def _load(self, client: discord.Client):
        db = get_database()
        rows = await ApplicationsRepository(db.pool).list_approved_insurers_for_browser(
            guild_id=self.guild_id,
            category=self.category,
        )
        self.provider = next((dict(r) for r in rows if int(r.get("user_id") or 0) == self.insurer_user_id), None)
        self.policies = []
        if self.provider:
            discord_id = int(self.provider.get("user_id") or self.provider.get("discord_id") or 0)
            self.provider["user_id"] = discord_id
            self.provider["discord_id"] = discord_id
            self.provider["display_name"] = self.provider.get("display_name") or f"Discord User {discord_id}"
            user = client.get_user(discord_id)
            if user is None and discord_id:
                try:
                    user = await client.fetch_user(discord_id)
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

        title = self.provider.get("display_name") or f"Insurer {self.insurer_user_id}"
        description = f"**Provider:** <@{self.provider['discord_id']}>\n**Display Name:** {title}"

        embed = create_info_embed("Insurer Card", description)
        embed.add_field(
            name="Coverage Summary",
            value=_trim_text(self.provider.get("coverage_summary") or "Not provided.", 900),
            inline=False,
        )
        embed.add_field(
            name="Pricing",
            value=_trim_text(self.provider.get("pricing_text") or "Not provided.", 900),
            inline=False,
        )
        embed.add_field(
            name="Rules / Exclusions",
            value=_trim_text(self.provider.get("rules_exclusions") or "Not provided.", 900),
            inline=False,
        )
        if self.provider.get("response_time_text"):
            embed.add_field(name="Response Time", value=_trim_text(self.provider.get("response_time_text"), 900), inline=False)
        if self.provider.get("contact_notes"):
            embed.add_field(name="Contact Notes", value=_trim_text(self.provider.get("contact_notes"), 900), inline=False)

        if self.provider.get("activation_delay_minutes") is not None or self.provider.get("coverage_duration_minutes") is not None:
            meta_bits = []
            if self.provider.get("activation_delay_minutes") is not None:
                meta_bits.append(f"Activation delay: {self.provider.get('activation_delay_minutes')} minutes")
            if self.provider.get("coverage_duration_minutes") is not None:
                meta_bits.append(f"Coverage duration: {self.provider.get('coverage_duration_minutes')} minutes")
            embed.add_field(name="Timing", value="\n".join(meta_bits), inline=False)

        categories = self.provider.get("categories") or []
        embed.add_field(name="Categories", value=", ".join(categories) if categories else "None", inline=False)

        if self.provider.get("image_url"):
            embed.set_thumbnail(url=self.provider.get("image_url"))

        return embed

    @ui.button(label="Back", style=discord.ButtonStyle.secondary, custom_id="insurers:card:back")
    async def back_button(self, interaction: discord.Interaction, button: ui.Button):
        list_view = InsurerBrowserView(
            guild_id=self.guild_id,
            category=self.category,
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
    xanax_count = ui.TextInput(label="Xanax to cover", placeholder="10", min_length=1, max_length=4)
    
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
            policy = await InsuranceRepository(db.pool).get_policy(self.policy_id)
            if not policy or not policy['active']:
                await interaction.followup.send(embed=create_error_embed("Policy Unavailable"), ephemeral=True)
                return
            
            if xanax > policy['max_coverage_xanax']:
                await interaction.followup.send(embed=create_error_embed("Exceeds Max Coverage", f"Max: {policy['max_coverage_xanax']}"), ephemeral=True)
                return
            
            if not await require_api_key(interaction, db, "request insurance"):
                return
            key_data = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
            
            premium = xanax * policy['premium_per_xanax']
            payout = xanax * policy['payout_per_xanax']
            expires_at = datetime.utcnow() + timedelta(hours=policy['duration_hours'])
            
            coverage_id = await InsuranceRepository(db.pool).create_coverage(
                self.policy_id, interaction.user.id, key_data['torn_user_id'],
                xanax, premium, payout, expires_at
            )
            
            provider = await InsuranceRepository(db.pool).get_provider_by_id(policy['provider_id'])
            
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
            coverage = await InsuranceRepository(db.pool).get_coverage(self.coverage_id)
            if not coverage or coverage['status'] != 'pending':
                await interaction.followup.send(embed=create_error_embed("Coverage Unavailable"), ephemeral=True)
                return
            
            if not await require_api_key(interaction, db, "verify insurance payment"):
                return
            key_data = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            
            policy = await InsuranceRepository(db.pool).get_policy(coverage['policy_id'])
            provider = await InsuranceRepository(db.pool).get_provider_by_id(policy['provider_id'])
            
            torn_api = get_torn_api()
            payment = await torn_api.verify_payment(
                api_key, provider['torn_user_id'], 'cash', coverage['premium_paid']
            )
            
            if not payment:
                await interaction.followup.send(embed=create_error_embed("Payment Not Found", "Send premium and try again"), ephemeral=True)
                return
            
            await InsuranceRepository(db.pool).activate_coverage(self.coverage_id)
            receipts = PaymentReceiptService(db.pool)
            receipt_id = await receipts.create_and_verify(
                featureType="insurance",
                featureRefId=self.coverage_id,
                payer_discord_id=interaction.user.id,
                payer_torn_id=key_data.get('torn_user_id'),
                payee_discord_id=provider.get('discord_id'),
                payee_torn_id=provider.get('torn_user_id'),
                amount=coverage['premium_paid'],
                currency_type='cash',
                metadata=payment,
            )
            await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="coverage_activated", target_type="insurance", target_id=self.coverage_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:ActivateCoverageView.confirm")
            
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
            await InsuranceRepository(db.pool).set_claim_payout_items(self.claim_id, parsed, resolved_by=interaction.user.id)
            await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="claim_payout_set", target_type="claim", target_id=self.claim_id, payload={"payout_items": parsed}, guild_id=interaction.guild_id, source="views/components.py:SetClaimPayoutModal.on_submit")
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
        claim = await InsuranceRepository(db.pool).get_claim(self.claim_id)
        if not claim:
            await interaction.response.send_message(embed=create_error_embed("Claim Not Found"), ephemeral=True)
            return

        policy = await InsuranceRepository(db.pool).get_policy(claim['policy_id'])
        seed_items = claim.get('payout_items') or (policy.get('payout_items') if policy else []) or []
        await interaction.response.send_modal(SetClaimPayoutModal(self.claim_id, payout_items_to_string(seed_items)))

    @ui.button(label="Verify Payout", style=discord.ButtonStyle.success, emoji=config.EMOJI_CHECK)
    async def verify_payout(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            claim = await InsuranceRepository(db.pool).get_claim(self.claim_id)
            if not claim:
                await interaction.followup.send(embed=create_error_embed("Claim Not Found"), ephemeral=True)
                return
            payout_items = claim.get("payout_items") or []
            if not payout_items:
                await interaction.followup.send(embed=create_error_embed("Payout Not Set", "Use **Set Payout** first."), ephemeral=True)
                return

            if not await require_api_key(interaction, db, "verify claim payout"):
                return
            key_data = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)

            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            torn_api = get_torn_api()

            candidate_logs = await torn_api.get_item_send_receive_logs(
                api_key, limit=config.PAYMENT_VERIFICATION_LOG_LIMIT
            )

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
                counterparty = _extract_counterparty_torn_id(entry)
                if counterparty != recipient_torn_id:
                    continue

                if all(_count_item_qty_by_id(entry, i['item_id']) >= i['qty'] for i in resolved_payout_items):
                    matched_log = entry
                    break

            if not matched_log:
                await interaction.followup.send(embed=create_error_embed("Payout Verification Failed", "No matching payout log found yet. Send items and retry."), ephemeral=True)
                return

            await InsuranceRepository(db.pool).mark_claim_paid_with_log(
                self.claim_id,
                int(_extract_log_id(matched_log) or 0),
                int(matched_log.get('timestamp') or 0),
                json.dumps(matched_log, ensure_ascii=False),
            )
            await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="claim_paid", target_type="claim", target_id=self.claim_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:ClaimPaidButton.confirm")
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
    reason = ui.TextInput(label="Reason", placeholder="Missing proof", style=discord.TextStyle.paragraph)
    
    def __init__(self, claim_id: int):
        super().__init__()
        self.claim_id = claim_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        await InsuranceRepository(db.pool).reject_claim(self.claim_id, interaction.user.id, self.reason.value)
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="claim_denied", target_type="claim", target_id=self.claim_id, payload={"reason": self.reason.value}, guild_id=interaction.guild_id, source="views/components.py:DenyClaimModal.on_submit")
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
        entry = await RafflesRepository(db.pool).get_entry_by_raffle_and_discord(self.raffle_id, interaction.user.id)
        if not entry:
            await interaction.followup.send(embed=create_info_embed("No Tickets", "You haven't entered this raffle"), ephemeral=True)
            return
        
        status = "verified" if entry.get('payment_verified') is True else "reserved"
        info = f"**Tickets:** {entry['num_tickets']}\n**Status:** {status.title()}"
        if status == 'reserved' and entry.get('reserved_until'):
            info += f"\n**Expires:** <t:{int(entry['reserved_until'].timestamp())}:R>"
        
        await interaction.followup.send(embed=create_info_embed("Your Entry", info), ephemeral=True)


class BuyTicketsModal(ui.Modal, title="Buy Raffle Tickets"):
    ticket_count = ui.TextInput(label="Ticket count", placeholder="2", min_length=1, max_length=4)
    
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
            creator_key = await UsersRepository(db.pool).get_user_api_key(raffle['creator_discord_id'])
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
            await require_api_key(interaction, get_database(), "enter a raffle")
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
            entry = await RafflesRepository(db.pool).get_entry_by_raffle_and_discord(self.raffle_id, interaction.user.id)
            if not entry or entry.get('payment_verified'):
                await interaction.followup.send(embed=create_error_embed("Entry Unavailable"), ephemeral=True)
                return
            
            raffle = await RafflesRepository(db.pool).get_raffle(self.raffle_id)
            if not await require_api_key(interaction, db, "verify a raffle purchase"):
                return
            key_data = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            
            torn_api = get_torn_api()
            creator_key = await UsersRepository(db.pool).get_user_api_key(raffle['creator_discord_id'])
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
            
            await RafflePaymentService(db).verify_entry_payment(self.entry_id, manual=True)
            await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="raffle_entry_verified", target_type="raffle", target_id=self.raffle_id, payload={}, guild_id=interaction.guild_id, source="views/components.py:RaffleVerifyPaymentButton.verify")
            
            await interaction.followup.send(embed=create_success_embed(
                "Entry Confirmed!",
                f"You have {entry['num_tickets']} tickets in the raffle. Good luck!"
            ), ephemeral=True)
            
            # Update raffle embed
            settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
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
        await interaction.response.send_message("Select 99k_Jump_Host role:", view=RoleSelectView("host99k_role_id"), ephemeral=True)
    
    @ui.button(label="Set Insurer Role", style=discord.ButtonStyle.primary, emoji=config.EMOJI_SHIELD)
    async def insurer_role(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_message("Select HJ_Insureance_provider role:", view=RoleSelectView("insurer_role_id"), ephemeral=True)
    
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
        await GuildSettingsRepository(db).upsert_settings(interaction.guild.id, **{self.setting_key: self.values[0].id})
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action=f"set_{self.setting_key}", target_type="guild", target_id=interaction.guild.id, payload={}, guild_id=interaction.guild_id, source="views/components.py:GuildSettingSelect.callback")
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
        await GuildSettingsRepository(db).upsert_settings(interaction.guild.id, **{self.setting_key: resolved.id})
        await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action=f"set_{self.setting_key}", target_type="guild", target_id=interaction.guild.id, payload={}, guild_id=interaction.guild_id, source="views/components.py:GuildSettingSelect.callback")
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
    session = await JumpsRepository(db.pool).get_session(session_id)
    if not session:
        return create_error_embed("Session Not Found")

    signups = await JumpsRepository(db.pool).list_signups(session_id)
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


def get_session_announce_ids(session: dict) -> tuple[int | None, int | None]:
    channel_id = session.get("announce_channel_id")
    message_id = session.get("announce_message_id")
    try:
        channel_int = int(channel_id) if channel_id else None
    except (TypeError, ValueError):
        channel_int = None
    try:
        message_int = int(message_id) if message_id else None
    except (TypeError, ValueError):
        message_int = None
    return channel_int, message_int



async def _start_jump_countdown(interaction: discord.Interaction, session_id: int, delay_seconds: int) -> None:
    db = get_database()
    session = await JumpsRepository(db.pool).get_session(session_id)
    if not session:
        await interaction.followup.send(embed=create_error_embed("Session Not Found"), ephemeral=True)
        return

    starts_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
    mm, ss = divmod(delay_seconds, 60)
    countdown_text = f"{mm:02d}:{ss:02d}"

    signups = await JumpsRepository(db.pool).list_signups(session_id)
    participant_ids = {int(s["discord_id"]) for s in signups}
    participant_ids.add(int(session["host_discord_id"]))

    announcement_url = ""
    if interaction.guild and session.get("announce_channel_id") and session.get("announce_message_id"):
        announcement_url = f"https://discord.com/channels/{interaction.guild.id}/{session['announce_channel_id']}/{session['announce_message_id']}"

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

    if interaction.guild and session.get("announce_channel_id") and session.get("announce_message_id"):
        try:
            channel = interaction.guild.get_channel(int(session["announce_channel_id"]))
            if channel:
                announcement_message = await channel.fetch_message(session["announce_message_id"])
                base_embed = announcement_message.embeds[0] if announcement_message.embeds else None
                if base_embed:
                    embed = base_embed.copy()
                    embed.add_field(name="Start", value=f"Jump starts in {countdown_text} (host initiated)", inline=False)
                    await announcement_message.edit(embed=embed, view=JumpSessionView(session_id))
        except Exception:
            pass

    await AuditRepository(db.pool).log_audit(actor_discord_id=interaction.user.id, action="jump_start_initiated", target_type="session", target_id=session_id, payload={"delay_seconds": delay_seconds, "dm_sent": sent_count}, guild_id=interaction.guild_id, source="views/components.py:start_jump_session")
    await interaction.followup.send(
        embed=create_success_embed("Jump Start Announced", f"DM sent to {sent_count} participant(s). Start in {countdown_text}."),
        ephemeral=True,
    )

async def update_jump_embed(session_id: int, message: discord.Message) -> str:
    db = get_database()
    session = await JumpsRepository(db.pool).get_session(session_id)
    if not session:
        return "error"

    guild = getattr(message, "guild", None)

    signups = await JumpsRepository(db.pool).list_signups(session_id)
    readiness = await JumpsRepository(db.pool).list_readiness(session_id)
    embed = create_jump_session_embed(session, signups, readiness)

    try:
        try:
            await message.edit(embed=embed, view=JumpSessionView(session_id))
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Jump message edit failed guild=%s channel=%s message=%s", getattr(guild, "id", None), getattr(getattr(message, "channel", None), "id", None), session.get("announce_message_id"))
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
        channel_id, announcement_message_id = get_session_announce_ids(session)
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
            log.warning("Jump announcement edit failed guild=%s channel=%s message=%s", getattr(guild, "id", None), channel_id, session.get("announce_message_id"))
        return "ok"
    except discord.Forbidden:
        log.warning(
            "Update embed missing access guild_id=%s channel_id=%s message_id=%s session_id=%s",
            getattr(getattr(message, "guild", None), "id", None),
            session.get('announce_channel_id'),
            session.get('announce_message_id'),
            session_id,
        )
        return "missing_access"
    except discord.HTTPException:
        log.warning(
            "Update embed fallback http error channel_id=%s message_id=%s session_id=%s",
            session.get('announce_channel_id'),
            session.get('announce_message_id'),
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
        raffle = await RafflesRepository(db.pool).get_raffle(raffle_id)
        if not raffle:
            return
        entries = await RafflesRepository(db.pool).get_raffle_entries(raffle_id)
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
        signups = await JumpsRepository(db.pool).list_signups(session_id)
        
        for signup in signups:
            try:
                key_data = await UsersRepository(db.pool).get_user_api_key(signup['discord_id'])
                if not key_data:
                    log.warning("Skipping readiness refresh due to missing API key discord_id=%s guild_id=%s", signup['discord_id'], signup.get('guild_id'))
                    continue
                
                security = get_security_manager()
                api_key = security.decrypt(key_data['encrypted_key'])
                torn_api = get_torn_api()
                
                user_data = await torn_api.get_user_data(api_key)
                energy_current = int(user_data.get('bars', {}).get('energy', {}).get('current', 0) or 0)
                energy_max = int(user_data.get('bars', {}).get('energy', {}).get('maximum', 0) or 0)
                drug_cd = int(user_data.get('cooldowns', {}).get('drug', 0) or 0)
                booster_cd = int(user_data.get('cooldowns', {}).get('booster', 0) or 0)

                status = "ready" if energy_current >= config.MIN_ENERGY_REQUIREMENT and drug_cd == 0 else "not_ready"

                await JumpsRepository(db.pool).upsert_readiness_snapshot(
                    session_id=session_id,
                    guild_id=signup['guild_id'],
                    discord_id=signup['discord_id'],
                    energy=energy_current,
                    energy_max=energy_max,
                    drug_cooldown=drug_cd,
                    booster_cooldown=booster_cd,
                    status_text=status,
                )
            except Exception as e:
                log.warning(f"Failed to refresh readiness for {signup['discord_id']}: {e}")
    except Exception as e:
        log.exception(f"Readiness refresh error: {e}")


async def shutdown_status_panel_tasks() -> None:
    tasks = list(JumpSessionView._status_panel_tasks.items())
    JumpSessionView._status_panel_tasks.clear()
    for _key, task in tasks:
        await task.stop()
