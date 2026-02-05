"""
Happy Jump Discord Bot - vNext with Dashboard
Combined service running Discord bot + FastAPI dashboard in single Railway service.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import json
from datetime import datetime, timedelta
import sys
import uvicorn
from threading import Thread
import os

import config
from utils import init_database, get_database, init_torn_api, get_torn_api, init_security, get_security_manager
from utils.embeds import (
    create_success_embed, create_error_embed, create_warning_embed, create_info_embed,
    create_jump_session_embed, create_api_key_guide_embed, create_statistics_embed,
    create_raffle_embed, create_raffle_winner_embed, create_claim_notification_embed
)
from views import (
    ApiKeyIntroView, ConfirmRemoveKeyView, JumpSessionView, HostControlView,
    SetupView, AdminDashboardView, BlacklistView, InsurancePolicyView,
    ProviderClaimsView, RaffleView
)

# Import web app
from web.app import app as fastapi_app
from admin_api import handlers as admin_handlers

# ============================================================================
# LOGGING SETUP
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("happy_jumper")

# ============================================================================
# BOT SETUP
# ============================================================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ============================================================================
# BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot ready handler."""
    log.info(f"Bot logged in as {bot.user}")
    log.info(f"Bot ID: {bot.user.id}")
    log.info(f"Discord.py version: {discord.__version__}")
    log.info(f"Guilds: {len(bot.guilds)}")
    
    # Register bot instance with admin API
    admin_handlers.set_bot_instance(bot)
    log.info("Bot instance registered with admin API")
    
    # Initialize database
    db = await init_database()
    log.info("Database initialized")
    
    # Initialize Torn API
    init_torn_api()
    log.info("Torn API initialized")
    
    # Initialize security
    await init_security()
    log.info("Security initialized")
    
    # Sync commands
    try:
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"Commands synced to guild {config.GUILD_ID}: {len(synced)} commands")
        else:
            synced = await bot.tree.sync()
            log.info(f"Commands synced globally: {len(synced)} commands")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")
    
    # Start background workers
    if not cleanup_worker.is_running():
        cleanup_worker.start()
    if not readiness_worker.is_running():
        readiness_worker.start()
    if not insurance_monitor.is_running():
        insurance_monitor.start()
    if not raffle_completion_worker.is_running():
        raffle_completion_worker.start()
    
    log.info("✓ Bot is ready!")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Handle bot joining a new guild."""
    log.info(f"Joined guild: {guild.name} ({guild.id})")
    db = get_database()
    await db.get_guild_settings(guild.id)  # Create default settings


# ============================================================================
# SLASH COMMANDS - API KEY MANAGEMENT
# ============================================================================

@bot.tree.command(name="set_api_key", description="Register your Torn API key for bot features")
async def set_api_key(interaction: discord.Interaction):
    """Register or update user's Torn API key."""
    embed = create_api_key_guide_embed()
    view = ApiKeyIntroView()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="remove_api_key", description="Delete your stored Torn API key")
async def remove_api_key(interaction: discord.Interaction):
    """Remove user's stored API key."""
    db = get_database()
    existing = await db.get_user_api_key(interaction.user.id)
    
    if not existing:
        embed = create_error_embed("No API Key", "You don't have an API key registered.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    view = ConfirmRemoveKeyView(interaction.user.id)
    embed = create_warning_embed(
        "Remove API Key?",
        "Are you sure you want to remove your API key? You will need to re-register to use bot features."
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="my_sessions", description="View your active jump sessions and waitlist positions")
async def my_sessions(interaction: discord.Interaction):
    """Show user's current sessions and waitlist entries."""
    db = get_database()
    
    # Get all active sessions in this guild
    sessions = await db.get_active_sessions(interaction.guild_id)
    
    user_signups = []
    user_waitlist = []
    hosted_sessions = []
    
    for session in sessions:
        # Check if hosting
        if session['host_discord_id'] == interaction.user.id:
            hosted_sessions.append(session)
        
        # Check if signed up
        signup = await db.get_signup(session['id'], interaction.user.id)
        if signup:
            user_signups.append({'session': session, 'signup': signup})
        
        # Check if on waitlist
        waitlist_pos = await db.get_waitlist_position(session['id'], interaction.user.id)
        if waitlist_pos:
            user_waitlist.append({'session': session, 'position': waitlist_pos})
    
    embed = create_info_embed(f"{config.EMOJI_JUMP} Your Sessions")
    
    if hosted_sessions:
        hosted_text = "\n".join([
            f"**Session #{s['id']}** - {s['status'].title()} ({s['xanax_count']}x Xanax, {s['max_spots']} spots)"
            for s in hosted_sessions
        ])
        embed.add_field(name="Hosting", value=hosted_text, inline=False)
    
    if user_signups:
        signups_text = "\n".join([
            f"**Session #{s['session']['id']}** - Status: {s['signup']['status'].title()}"
            for s in user_signups
        ])
        embed.add_field(name="Signed Up", value=signups_text, inline=False)
    
    if user_waitlist:
        waitlist_text = "\n".join([
            f"**Session #{w['session']['id']}** - Position #{w['position']}"
            for w in user_waitlist
        ])
        embed.add_field(name="Waitlist", value=waitlist_text, inline=False)
    
    if not (hosted_sessions or user_signups or user_waitlist):
        embed.description = "You're not currently in any active sessions or waitlists."
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================================
# SLASH COMMANDS - ADMIN SETUP
# ============================================================================

@bot.tree.command(name="setup", description="Configure bot settings for this server (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    """Server setup command for administrators."""
    db = get_database()
    settings = await db.get_guild_settings(interaction.guild_id)
    
    embed = create_info_embed(
        f"{config.EMOJI_CHART} Server Setup",
        "Configure channels and roles for Happy Jumper features."
    )
    
    # Current settings
    host_role = interaction.guild.get_role(settings.get('host99k_role_id') or 0)
    insurer_role = interaction.guild.get_role(settings.get('insurer_role_id') or 0)
    admin_role = interaction.guild.get_role(settings.get('admin_role_id') or 0)
    
    jump_channel = interaction.guild.get_channel(settings.get('jump_99k_channel_id') or 0)
    insurance_channel = interaction.guild.get_channel(settings.get('insurance_channel_id') or 0)
    raffle_channel = interaction.guild.get_channel(settings.get('raffle_channel_id') or 0)
    
    embed.add_field(
        name="Roles",
        value=(
            f"**99k Host:** {host_role.mention if host_role else 'Not set'}\n"
            f"**Insurer:** {insurer_role.mention if insurer_role else 'Not set'}\n"
            f"**Dashboard Admin:** {admin_role.mention if admin_role else 'Not set'}"
        ),
        inline=True
    )
    
    embed.add_field(
        name="Channels",
        value=(
            f"**99k Jumps:** {jump_channel.mention if jump_channel else 'Not set'}\n"
            f"**Insurance:** {insurance_channel.mention if insurance_channel else 'Not set'}\n"
            f"**Raffles:** {raffle_channel.mention if raffle_channel else 'Not set'}"
        ),
        inline=True
    )
    
    embed.add_field(
        name="Settings",
        value=(
            f"**Reservation Timeout:** {settings.get('reservation_timeout_minutes', 5)} minutes\n"
            f"**Auto-Complete:** {'Enabled' if settings.get('auto_complete_enabled', True) else 'Disabled'}"
        ),
        inline=True
    )
    
    # Dashboard link
    embed.add_field(
        name=f"{config.EMOJI_CHART} Dashboard",
        value=f"Configure more settings at:\n{config.DASHBOARD_URL}",
        inline=False
    )
    
    view = SetupView(interaction.guild_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="stats", description="View server statistics")
async def stats(interaction: discord.Interaction):
    """Show server statistics."""
    db = get_database()
    stats = await db.get_guild_statistics(interaction.guild_id)
    
    embed = create_statistics_embed(stats, f"Statistics for {interaction.guild.name}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="dashboard", description="Get a link to the web dashboard")
async def dashboard(interaction: discord.Interaction):
    """Show dashboard link."""
    embed = create_info_embed(
        f"{config.EMOJI_CHART} Happy Jumper Dashboard",
        f"Access the admin dashboard to create sessions, raffles, and manage insurance.\n\n"
        f"**URL:** {config.DASHBOARD_URL}\n\n"
        f"*Login with Discord to access admin features.*"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================================
# BACKGROUND WORKERS
# ============================================================================

@tasks.loop(seconds=config.CLEANUP_INTERVAL)
async def cleanup_worker():
    """Clean up expired reservations and update session statuses."""
    try:
        db = get_database()
        
        # Clean up expired signup reservations
        deleted_signups = await db.cleanup_expired_reservations()
        if deleted_signups > 0:
            log.info(f"Cleaned up {deleted_signups} expired signup reservations")
        
        # Clean up expired raffle entries
        deleted_raffle = await db.cleanup_expired_raffle_entries()
        if deleted_raffle > 0:
            log.info(f"Cleaned up {deleted_raffle} expired raffle entry reservations")
        
        # Clean up expired coverage reservations
        deleted_coverage = await db.cleanup_expired_coverage_reservations()
        if deleted_coverage > 0:
            log.info(f"Cleaned up {deleted_coverage} expired coverage reservations")
        
        # Clean up expired dashboard sessions
        await db.cleanup_expired_dashboard_sessions()
        
        # Auto-promote from waitlist for sessions with open spots
        sessions = await db.get_sessions_with_expired_signups()
        for session in sessions:
            signups = await db.get_session_signups(session['id'])
            confirmed_count = sum(1 for s in signups if s['status'] in ('reserved', 'confirmed'))
            
            while confirmed_count < session['max_spots']:
                promoted = await db.promote_from_waitlist(session['id'])
                if not promoted:
                    break
                
                # Notify promoted user
                try:
                    guild = bot.get_guild(session['guild_id'])
                    if guild:
                        member = guild.get_member(promoted['discord_id'])
                        if member:
                            embed = create_success_embed(
                                "Promoted from Waitlist!",
                                f"A spot opened up in Session #{session['id']}! "
                                f"You have {config.DEFAULT_RESERVATION_TIMEOUT} minutes to confirm your payment."
                            )
                            try:
                                await member.send(embed=embed)
                            except discord.Forbidden:
                                pass
                except Exception as e:
                    log.warning(f"Could not notify promoted user: {e}")
                
                confirmed_count += 1
        
    except Exception as e:
        log.error(f"Cleanup worker error: {e}", exc_info=True)


@cleanup_worker.before_loop
async def before_cleanup_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.READINESS_REFRESH_INTERVAL)
async def readiness_worker():
    """Refresh readiness status for active sessions."""
    try:
        db = get_database()
        torn_api = get_torn_api()
        security = get_security_manager()
        
        # Get active sessions across all guilds
        all_sessions = []
        for guild in bot.guilds:
            sessions = await db.get_active_sessions(guild.id)
            all_sessions.extend(sessions)
        
        if not all_sessions:
            return
        
        # Refresh readiness for each session
        for session in all_sessions:
            if session['status'] != 'locked':
                continue  # Only track readiness for locked sessions
            
            signups = await db.get_confirmed_signups(session['id'])
            
            for signup in signups:
                try:
                    api_key_data = await db.get_user_api_key(signup['discord_id'])
                    if not api_key_data:
                        continue
                    
                    api_key = security.decrypt_api_key(api_key_data['encrypted_key'])
                    
                    # Get user bars
                    bars = await torn_api.get_user_bars(api_key)
                    drug_cd = await torn_api.get_drug_cooldown(api_key)
                    
                    await db.update_readiness(
                        session['id'],
                        signup['discord_id'],
                        energy=bars.get('energy', 0),
                        energy_max=bars.get('energy_max', 150),
                        drug_cooldown=drug_cd,
                        status_text=_get_readiness_status(bars, drug_cd)
                    )
                    
                except Exception as e:
                    log.warning(f"Failed to refresh readiness for user {signup['discord_id']}: {e}")
                
                # Small delay to respect rate limits
                await asyncio.sleep(0.5)
    
    except Exception as e:
        log.error(f"Readiness worker error: {e}", exc_info=True)


@readiness_worker.before_loop
async def before_readiness_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.INSURANCE_CHECK_INTERVAL)
async def insurance_monitor():
    """Monitor insurance coverage and process automatic claims."""
    try:
        db = get_database()
        torn_api = get_torn_api()
        security = get_security_manager()
        
        # Expire old coverage
        await db.expire_coverage()
        
        # Get all active coverage
        active_coverage = await db.get_active_coverage()
        
        for coverage in active_coverage:
            try:
                api_key_data = await db.get_user_api_key(coverage['user_discord_id'])
                if not api_key_data:
                    continue
                
                api_key = security.decrypt_api_key(api_key_data['encrypted_key'])
                
                # Get last checked timestamp
                last_check = coverage.get('last_log_timestamp', 0)
                
                # Check logs for drug events
                drug_logs = await torn_api.check_drug_use_logs(api_key, since_timestamp=last_check)
                
                for log_entry in drug_logs:
                    # Check if this is an overdose event
                    od_event = await torn_api.identify_overdose_event(log_entry)
                    
                    if od_event:
                        # Check if claim already exists
                        log_id = od_event.get('log_id') or log_entry.get('id') or log_entry.get('log_id')
                        if log_id:
                            existing = await db.check_existing_claim(coverage['coverage_id'], log_id)
                            if existing:
                                continue
                        
                        # Create claim
                        await _create_insurance_claim(coverage, od_event, log_entry)
                
                # Update last check timestamp
                if drug_logs:
                    latest_ts = max(l.get('timestamp', 0) for l in drug_logs)
                    if latest_ts > last_check:
                        await db.update_coverage_last_check(coverage['coverage_id'], latest_ts)
                
            except Exception as e:
                log.warning(f"Failed to monitor coverage {coverage['coverage_id']}: {e}")
            
            # Small delay between checks
            await asyncio.sleep(0.5)
    
    except Exception as e:
        log.error(f"Insurance monitor error: {e}", exc_info=True)


@insurance_monitor.before_loop
async def before_insurance_monitor():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.RAFFLE_COMPLETION_INTERVAL)
async def raffle_completion_worker():
    """Check for completed raffles and draw winners."""
    try:
        db = get_database()
        
        # Get raffles that need to be drawn
        raffles_to_draw = await db.get_raffles_to_draw()
        
        for raffle in raffles_to_draw:
            try:
                await _draw_raffle_winner(raffle)
            except Exception as e:
                log.error(f"Failed to draw raffle {raffle['raffle_id']}: {e}")
    
    except Exception as e:
        log.error(f"Raffle completion worker error: {e}", exc_info=True)


@raffle_completion_worker.before_loop
async def before_raffle_completion_worker():
    await bot.wait_until_ready()


# ============================================================================
# WORKER HELPERS
# ============================================================================

def _get_readiness_status(bars: dict, drug_cd: int) -> str:
    """Determine readiness status text."""
    energy = bars.get('energy', 0)
    energy_max = bars.get('energy_max', 150)
    
    if drug_cd > 0:
        minutes = drug_cd // 60
        return f"CD: {minutes}m"
    elif energy < config.MIN_ENERGY_REQUIREMENT:
        return f"Low E: {energy}/{energy_max}"
    else:
        return "Ready"


async def _create_insurance_claim(coverage: dict, od_event: dict, raw_log: dict):
    """Create an insurance claim from an overdose event."""
    db = get_database()
    
    try:
        # Get policy for payout calculation
        policy = await db.get_policy(coverage['policy_id'])
        if not policy:
            return
        
        # Determine claim type and payout
        claim_type = od_event.get('type', 'xanax_overdose')
        xanax_lost = coverage.get('xanax_covered', 1)
        payout_amount = coverage.get('payout_amount', 0)
        
        # Create claim
        claim_id = await db.create_claim(
            coverage_id=coverage['coverage_id'],
            policy_id=coverage['policy_id'],
            user_discord_id=coverage['user_discord_id'],
            provider_id=policy['provider_id'],
            claim_type=claim_type,
            xanax_lost=xanax_lost,
            payout_amount=payout_amount,
            torn_log_id=od_event.get('log_id'),
            torn_log_timestamp=od_event.get('timestamp'),
            torn_log_evidence=json.dumps(raw_log)
        )
        
        log.info(f"Created insurance claim #{claim_id} for coverage #{coverage['coverage_id']}")
        
        # Notify provider
        try:
            provider = await db.get_provider_by_id(policy['provider_id'])
            if provider:
                for guild in bot.guilds:
                    member = guild.get_member(provider['discord_id'])
                    if member:
                        claim = await db.get_claim(claim_id)
                        embed = create_claim_notification_embed(claim, coverage)
                        try:
                            await member.send(embed=embed)
                        except discord.Forbidden:
                            pass
                        break
        except Exception as e:
            log.warning(f"Failed to notify provider of claim: {e}")
        
    except Exception as e:
        log.error(f"Failed to create insurance claim: {e}")


async def _draw_raffle_winner(raffle: dict):
    """Draw a winner for a completed raffle."""
    db = get_database()
    
    raffle_id = raffle['raffle_id']
    
    # Mark as drawing to prevent duplicate draws
    await db.update_raffle(raffle_id, status='drawing')
    
    try:
        # Draw winner
        winner = await db.draw_raffle_winner(raffle_id)
        
        # Log the draw
        await db.log_audit(
            None,  # System action
            "raffle_auto_drawn",
            "raffle",
            raffle_id,
            {"winner_discord_id": winner['discord_id'] if winner else None},
            guild_id=raffle['guild_id'],
            source='system'
        )
        
        # Get channel to announce winner
        guild = bot.get_guild(raffle['guild_id'])
        if not guild:
            return
        
        settings = await db.get_guild_settings(raffle['guild_id'])
        channel_id = raffle.get('announcement_channel_id') or settings.get('raffle_channel_id')
        
        if not channel_id:
            return
        
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        
        # Get updated raffle data
        updated_raffle = await db.get_raffle(raffle_id)
        
        if winner:
            # Announce winner
            embed = create_raffle_winner_embed(updated_raffle, winner)
            await channel.send(embed=embed)
            
            # Try to DM winner
            try:
                winner_member = guild.get_member(winner['discord_id'])
                if winner_member:
                    dm_embed = create_success_embed(
                        f"{config.EMOJI_TROPHY} You Won the Raffle!",
                        f"Congratulations! You won the raffle for:\n\n**{raffle['prize']}**\n\n"
                        f"Your winning ticket: #{winner.get('ticket_number', '?')} of {winner.get('total_tickets', '?')}\n\n"
                        f"Please contact the raffle creator to claim your prize."
                    )
                    await winner_member.send(embed=dm_embed)
            except discord.Forbidden:
                pass
        else:
            # No entries - announce no winner
            embed = create_info_embed(
                f"{config.EMOJI_TICKET} Raffle Ended - No Winner",
                f"The raffle for **{raffle['prize']}** has ended with no valid entries."
            )
            await channel.send(embed=embed)
        
        # Update original message if possible
        if raffle.get('announcement_message_id'):
            try:
                message = await channel.fetch_message(raffle['announcement_message_id'])
                entries = await db.get_raffle_entries(raffle_id)
                
                if winner:
                    embed = create_raffle_winner_embed(updated_raffle, winner)
                else:
                    embed = create_raffle_embed(updated_raffle, entries)
                
                await message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.Forbidden):
                pass
        
        log.info(f"Raffle #{raffle_id} completed. Winner: {winner['discord_id'] if winner else 'None'}")
        
    except Exception as e:
        log.error(f"Error drawing raffle {raffle_id}: {e}")
        # Reset status if draw failed
        await db.update_raffle(raffle_id, status='active')
        raise


# ============================================================================
# WEB SERVER
# ============================================================================

def run_fastapi():
    """Run FastAPI server in separate thread."""
    port = 8000
    if config.DASHBOARD_URL and ":" in config.DASHBOARD_URL:
        try:
            port = int(config.DASHBOARD_URL.split(":")[-1].split("/")[0])
        except ValueError:
            pass
    
    # Use PORT env var if set (Railway provides this)
    port = int(os.getenv("PORT", port))
    
    uvicorn.run(
        fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )


# ============================================================================
# MAIN
# ============================================================================

async def main():
    """Main entry point - runs both bot and web server."""
    config.validate_config()
    
    # Start FastAPI in background thread
    web_thread = Thread(target=run_fastapi, daemon=True)
    web_thread.start()
    log.info("FastAPI web server started")
    
    # Start bot
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
