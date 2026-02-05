"""Discord views for all Happy Jumper features."""

import discord
from discord import ui
import logging
from typing import Optional
from datetime import datetime, timedelta
from utils import get_database, get_security_manager, get_torn_api
from utils.torn_api import TornAPIError, TornAPIPermissionError
from utils.embeds import *
import config

log = logging.getLogger("happy_jumper.views")


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
            discord_id_api, torn_id, perms = await torn_api.validate_api_key(api_key)
            
            if discord_id_api != interaction.user.id:
                await interaction.followup.send(embed=create_error_embed(
                    "Discord ID Mismatch", f"Key belongs to Discord user {discord_id_api}"), ephemeral=True)
                return
            
            security = get_security_manager()
            encrypted = security.encrypt(api_key)
            
            db = get_database()
            await db.set_user_api_key(interaction.user.id, torn_id, encrypted)
            await db.log_audit(interaction.user.id, "api_key_registered", "user", interaction.user.id, {"torn_id": torn_id})
            
            await interaction.followup.send(embed=create_success_embed(
                "API Key Registered", f"Torn ID: `{torn_id}`\nPermissions: `{', '.join(sorted(perms))}`"), ephemeral=True)
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
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        self.session_id = session_id
    
    @ui.button(label="Join Jump", style=discord.ButtonStyle.success, emoji=config.EMOJI_JUMP, custom_id="jump_join")
    async def join(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            
            # Check blacklist
            blacklist = await db.is_blacklisted(interaction.guild.id, interaction.user.id)
            if blacklist:
                await interaction.followup.send(embed=create_error_embed("Blacklisted", blacklist['reason']), ephemeral=True)
                return
            
            session = await db.get_jump_session(self.session_id)
            if not session or session['status'] != 'open':
                await interaction.followup.send(embed=create_error_embed("Session Unavailable"), ephemeral=True)
                return
            
            key_data = await db.get_user_api_key(interaction.user.id)
            if not key_data:
                await interaction.followup.send(embed=create_error_embed("API Key Required", "Use `/set_api_key`"), ephemeral=True)
                return
            
            existing = await db.get_signup(self.session_id, interaction.user.id)
            if existing:
                await interaction.followup.send(embed=create_warning_embed("Already Signed Up", f"Status: {existing['status']}"), ephemeral=True)
                return
            
            signups = await db.get_session_signups(self.session_id)
            if len(signups) >= session['max_spots']:
                # Add to waitlist
                position = await db.add_to_waitlist(self.session_id, interaction.user.id, key_data['torn_user_id'])
                await interaction.followup.send(embed=create_info_embed("Session Full", f"Added to waitlist at position #{position}"), ephemeral=True)
                return
            
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            torn_api = get_torn_api()
            
            user_data = await torn_api.get_user_data(api_key)
            energy = user_data.get('bars', {}).get('energy', {})
            current_energy = energy.get('current', 0)
            max_energy = energy.get('maximum', 150)
            drug_cd = user_data.get('cooldowns', {}).get('drug', 0)
            
            if current_energy < config.MIN_ENERGY_REQUIREMENT:
                await interaction.followup.send(embed=create_warning_embed("Low Energy", f"Need {config.MIN_ENERGY_REQUIREMENT}, have {current_energy}"), ephemeral=True)
                return
            
            if drug_cd > 0:
                await interaction.followup.send(embed=create_warning_embed("Drug Cooldown", f"{drug_cd}s remaining"), ephemeral=True)
                return
            
            settings = await db.get_guild_settings(interaction.guild.id)
            timeout = settings.get('reservation_timeout_minutes', config.DEFAULT_RESERVATION_TIMEOUT)
            reserved_until = datetime.utcnow() + timedelta(minutes=timeout)
            
            await db.create_signup(self.session_id, interaction.user.id, key_data['torn_user_id'], reserved_until)
            await db.update_participant_readiness(self.session_id, interaction.user.id, current_energy, max_energy, drug_cd, "ready")
            await db.log_audit(interaction.user.id, "jump_signup", "session", self.session_id)
            
            await update_jump_embed(self.session_id, interaction.message)
            
            payment = f"${session['payment_amount']:,}" if session['payment_type'] == 'cash' else f"{session['payment_amount']}x DVD"
            await interaction.followup.send(embed=create_success_embed(
                "Reservation Created",
                f"**Payment:** {payment}\n**Send to:** Torn ID `{session['host_torn_id']}`\n**Expires:** <t:{int(reserved_until.timestamp())}:R>"
            ), view=PaymentView(self.session_id), ephemeral=True)
        except Exception as e:
            log.exception(f"Join error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)
    
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
            await interaction.followup.send(embed=create_success_embed("Left Session"), ephemeral=True)
        except Exception as e:
            log.exception(f"Leave error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)
    
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
        key_data = await db.get_user_api_key(interaction.user.id)
        if not key_data:
            await interaction.followup.send(embed=create_error_embed("API Key Required"), ephemeral=True)
            return
        
        existing = await db.get_signup(self.session_id, interaction.user.id)
        if existing:
            await interaction.followup.send(embed=create_warning_embed("Already Signed Up"), ephemeral=True)
            return
        
        waitlist = await db.get_session_waitlist(self.session_id)
        if any(w['discord_id'] == interaction.user.id for w in waitlist):
            await interaction.followup.send(embed=create_warning_embed("Already on Waitlist"), ephemeral=True)
            return
        
        position = await db.add_to_waitlist(self.session_id, interaction.user.id, key_data['torn_user_id'])
        await interaction.followup.send(embed=create_success_embed("Added to Waitlist", f"Position: #{position}"), ephemeral=True)


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
            
            await db.update_signup(self.session_id, interaction.user.id, status='paid', reserved_until=None)
            await db.log_audit(interaction.user.id, "payment_verified", "session", self.session_id, payment)
            
            # Try to update embed
            settings = await db.get_guild_settings(interaction.guild.id)
            channel_id = settings.get('jump_99k_channel_id')
            if channel_id and session.get('announcement_message_id'):
                try:
                    channel = interaction.guild.get_channel(channel_id)
                    if channel:
                        message = await channel.fetch_message(session['announcement_message_id'])
                        await update_jump_embed(self.session_id, message)
                except:
                    pass
            
            await interaction.followup.send(embed=create_success_embed("Payment Verified!", "You're all set for the jump!"), ephemeral=True)
        except Exception as e:
            log.exception(f"Payment verification error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)


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
        await db.complete_session(self.session_id, True)
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
        await db.update_jump_session(self.session_id, status='cancelled')
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
        await db.record_host_rating(session['host_discord_id'], interaction.user.id, self.session_id, self.rating)
        await interaction.followup.send(embed=create_success_embed("Rating Submitted", f"You rated the host {self.rating}/5 stars"), ephemeral=True)


# ============================================================================
# INSURANCE VIEWS
# ============================================================================

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
            
            coverage_id = await db.create_insurance_coverage(
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
        options = [discord.SelectOption(label=f"Claim #{c['claim_id']}", description=f"${c['payout_amount']:,} - {c['status']}", value=str(c['claim_id'])) for c in claims[:25]]
        super().__init__(placeholder="Select a claim...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        claim_id = int(self.values[0])
        await interaction.response.send_message(f"Managing Claim #{claim_id}", view=ClaimManageView(claim_id), ephemeral=True)


class ClaimManageView(ui.View):
    def __init__(self, claim_id: int):
        super().__init__(timeout=300)
        self.claim_id = claim_id
    
    @ui.button(label="Approve & Pay", style=discord.ButtonStyle.success, emoji=config.EMOJI_CHECK)
    async def approve(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        await db.approve_claim(self.claim_id)
        await db.log_audit(interaction.user.id, "claim_approved", "claim", self.claim_id)
        
        claim = await db.get_claim(self.claim_id)
        await interaction.followup.send(embed=create_success_embed(
            "Claim Approved",
            f"Pay ${claim['payout_amount']:,} to Torn ID `{claim['user_torn_id']}`\n"
            f"Use `/insurance_verify_payout` after sending."
        ), ephemeral=True)
    
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
        await db.deny_claim(self.claim_id, self.reason.value)
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
        entry = await db.get_user_raffle_entry(self.raffle_id, interaction.user.id)
        if not entry:
            await interaction.followup.send(embed=create_info_embed("No Tickets", "You haven't entered this raffle"), ephemeral=True)
            return
        
        status = entry['status']
        info = f"**Tickets:** {entry['tickets']}\n**Status:** {status.title()}"
        if status == 'reserved' and entry['reserved_until']:
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
            if tickets < 1:
                await interaction.followup.send(embed=create_error_embed("Invalid Amount"), ephemeral=True)
                return
            
            db = get_database()
            raffle = await db.get_raffle(self.raffle_id)
            if not raffle or raffle['status'] != 'open':
                await interaction.followup.send(embed=create_error_embed("Raffle Unavailable"), ephemeral=True)
                return
            
            # Check user's current tickets
            entry = await db.get_user_raffle_entry(self.raffle_id, interaction.user.id)
            current = entry['tickets'] if entry else 0
            if current + tickets > raffle['max_tickets_per_user']:
                await interaction.followup.send(embed=create_error_embed("Exceeds Limit", f"Max {raffle['max_tickets_per_user']} per user"), ephemeral=True)
                return
            
            # Check total tickets
            sold = await db.get_raffle_ticket_count(self.raffle_id)
            if sold + tickets > raffle['max_tickets']:
                await interaction.followup.send(embed=create_error_embed("Not Enough Tickets", f"Only {raffle['max_tickets'] - sold} available"), ephemeral=True)
                return
            
            key_data = await db.get_user_api_key(interaction.user.id)
            if not key_data:
                await interaction.followup.send(embed=create_error_embed("API Key Required"), ephemeral=True)
                return
            
            reserved_until = datetime.utcnow() + timedelta(minutes=config.RAFFLE_RESERVATION_TIMEOUT)
            await db.create_raffle_entry(self.raffle_id, interaction.user.id, key_data['torn_user_id'], tickets, reserved_until)
            
            total_xanax = tickets * raffle['ticket_price_xanax']
            await interaction.followup.send(embed=create_info_embed(
                "Tickets Reserved",
                f"**Tickets:** {tickets}\n"
                f"**Cost:** {total_xanax} Xanax\n"
                f"**Send to:** <@{raffle['admin_discord_id']}> (Torn: `{raffle['admin_torn_id']}`)\n"
                f"**Expires:** <t:{int(reserved_until.timestamp())}:R>"
            ), view=RafflePaymentView(self.raffle_id), ephemeral=True)
        except ValueError:
            await interaction.followup.send(embed=create_error_embed("Invalid Number"), ephemeral=True)
        except Exception as e:
            log.exception(f"Ticket purchase error: {e}")
            await interaction.followup.send(embed=create_error_embed("Error", str(e)), ephemeral=True)


class RafflePaymentView(ui.View):
    def __init__(self, raffle_id: int):
        super().__init__(timeout=600)
        self.raffle_id = raffle_id
    
    @ui.button(label="Verify Xanax Payment", style=discord.ButtonStyle.success, emoji=config.EMOJI_PILL)
    async def verify(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            db = get_database()
            entry = await db.get_user_raffle_entry(self.raffle_id, interaction.user.id)
            if not entry or entry['status'] == 'paid':
                await interaction.followup.send(embed=create_error_embed("Entry Unavailable"), ephemeral=True)
                return
            
            raffle = await db.get_raffle(self.raffle_id)
            key_data = await db.get_user_api_key(interaction.user.id)
            security = get_security_manager()
            api_key = security.decrypt(key_data['encrypted_key'])
            
            torn_api = get_torn_api()
            total_xanax = entry['tickets'] * raffle['ticket_price_xanax']
            
            payment = await torn_api.verify_xanax_payment(api_key, raffle['admin_torn_id'], total_xanax)
            
            if not payment:
                await interaction.followup.send(embed=create_error_embed("Payment Not Found", "Send Xanax and try again"), ephemeral=True)
                return
            
            await db.verify_raffle_entry(self.raffle_id, interaction.user.id)
            await db.log_audit(interaction.user.id, "raffle_entry_verified", "raffle", self.raffle_id)
            
            await interaction.followup.send(embed=create_success_embed(
                "Entry Confirmed!",
                f"You have {entry['tickets']} tickets in the raffle. Good luck!"
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
        super().__init__(placeholder="Select a channel...", channel_types=[discord.ChannelType.text])
        self.setting_key = setting_key
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        await db.update_guild_settings(interaction.guild.id, **{self.setting_key: self.values[0].id})
        await db.log_audit(interaction.user.id, f"set_{self.setting_key}", "guild", interaction.guild.id)
        await interaction.followup.send(embed=create_success_embed("Channel Updated", f"Set to {self.values[0].mention}"), ephemeral=True)


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


class BlacklistView(ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    
    @ui.button(label="Add to Blacklist", style=discord.ButtonStyle.danger, emoji=config.EMOJI_CROSS)
    async def add(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AddBlacklistModal())
    
    @ui.button(label="View Blacklist", style=discord.ButtonStyle.secondary, emoji=config.EMOJI_LIST)
    async def view(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        db = get_database()
        blacklist = await db.get_guild_blacklist(interaction.guild.id)
        await interaction.followup.send(embed=create_blacklist_embed(blacklist), ephemeral=True)


class AddBlacklistModal(ui.Modal, title="Add to Blacklist"):
    user_id = ui.TextInput(label="Discord User ID", placeholder="Enter Discord user ID")
    reason = ui.TextInput(label="Reason", placeholder="Why are they being blacklisted?", style=discord.TextStyle.paragraph)
    duration = ui.TextInput(label="Duration (hours, 0=permanent)", placeholder="0", default="0")
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            discord_id = int(self.user_id.value)
            hours = int(self.duration.value)
            expires_at = datetime.utcnow() + timedelta(hours=hours) if hours > 0 else None
            
            db = get_database()
            await db.add_to_blacklist(interaction.guild.id, discord_id, self.reason.value, interaction.user.id, expires_at)
            await db.log_audit(interaction.user.id, "blacklist_add", "user", discord_id, {"reason": self.reason.value})
            
            await interaction.followup.send(embed=create_success_embed("User Blacklisted", f"<@{discord_id}> has been blacklisted"), ephemeral=True)
        except ValueError:
            await interaction.followup.send(embed=create_error_embed("Invalid Input"), ephemeral=True)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def update_jump_embed(session_id: int, message: discord.Message):
    try:
        db = get_database()
        session = await db.get_jump_session(session_id)
        if not session:
            return
        signups = await db.get_session_signups(session_id)
        readiness = await db.get_session_readiness(session_id)
        embed = create_jump_session_embed(session, signups, readiness)
        await message.edit(embed=embed, view=JumpSessionView(session_id))
    except Exception as e:
        log.exception(f"Update embed error: {e}")


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
                await message.edit(embed=embed, view=RaffleView(raffle_id))
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
                
                await db.update_participant_readiness(
                    session_id, signup['discord_id'],
                    energy.get('current', 0), energy.get('maximum', 150),
                    drug_cd, status
                )
            except Exception as e:
                log.warning(f"Failed to refresh readiness for {signup['discord_id']}: {e}")
    except Exception as e:
        log.exception(f"Readiness refresh error: {e}")
