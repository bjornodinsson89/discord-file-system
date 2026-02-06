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
from admin_api.schemas import (
    CreateSessionRequest,
    CreateRaffleRequest,
    CreatePolicyRequest,
    UpdateSettingsRequest
)

# ============================================================================
# LOGGING SETUP
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("happy_jumper")


def normalize_dashboard_url(url: str) -> str:
    """Ensure dashboard URLs include an explicit http/https scheme."""
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    return f"https://{url}"


async def ensure_admin(interaction: discord.Interaction) -> bool:
    """Ensure the invoking user has administrator permissions."""
    if not interaction.guild or not interaction.user.guild_permissions.administrator:
        embed = create_error_embed("Not Authorized", "Administrator permission required.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return True

# ============================================================================
# BOT SETUP
# ============================================================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.synced = False

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
    
    # Sync commands once per process lifecycle
    if not bot.synced:
        try:
            if config.GUILD_ID:
                guild = discord.Object(id=config.GUILD_ID)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                log.info(f"Commands synced to guild {config.GUILD_ID}: {len(synced)} commands")
            else:
                synced = await bot.tree.sync()
                log.info(f"Commands synced globally: {len(synced)} commands")
            bot.synced = True
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")
    else:
        log.info("Command sync skipped (already synced).")
    
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
    await interaction.response.defer(ephemeral=True)
    embed = create_api_key_guide_embed()
    view = ApiKeyIntroView()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="remove_api_key", description="Delete your stored Torn API key")
async def remove_api_key(interaction: discord.Interaction):
    """Remove user's stored API key."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    existing = await db.get_user_api_key(interaction.user.id)
    
    if not existing:
        embed = create_error_embed("No API Key", "You don't have an API key registered.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    view = ConfirmRemoveKeyView(interaction.user.id)
    embed = create_warning_embed(
        "Remove API Key?",
        "Are you sure you want to remove your API key? You will need to re-register to use bot features."
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="my_sessions", description="View your active jump sessions and waitlist positions")
async def my_sessions(interaction: discord.Interaction):
    """Show user's current sessions and waitlist entries."""
    await interaction.response.defer(ephemeral=True)
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
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================================
# SLASH COMMANDS - ADMIN SETUP
# ============================================================================

@bot.tree.command(name="setup", description="Configure bot settings for this server (Admin only)")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    """Server setup command for administrators."""
    await interaction.response.defer(ephemeral=True)
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
        value=f"Configure more settings at:\n{normalize_dashboard_url(config.FRONTEND_URL)}",
        inline=False
    )
    
    view = SetupView(interaction.guild_id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="stats", description="View server statistics")
async def stats(interaction: discord.Interaction):
    """Show server statistics."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    stats = await db.get_guild_statistics(interaction.guild_id)
    
    embed = create_statistics_embed(stats, f"Statistics for {interaction.guild.name}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="dashboard", description="Get a link to the web dashboard")
async def dashboard(interaction: discord.Interaction):
    """Show dashboard link."""
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(
        title=f"{config.EMOJI_CHART} Happy Jumper Dashboard",
        description=(
            "Access the admin dashboard to manage all bot features from a beautiful web interface.\n\n"
            f"**Dashboard URL:**\n{normalize_dashboard_url(config.FRONTEND_URL)}\n\n"
            "**Features:**\n"
            f"{config.EMOJI_JUMP} Create & manage 99k jump sessions\n"
            f"{config.EMOJI_TICKET} Create & run raffles\n"
            f"{config.EMOJI_SHIELD} Insurance policies & claims\n"
            f"{config.EMOJI_LIST} Full audit logging\n"
            f"{config.EMOJI_USER} Member management\n\n"
            "*Login with Discord to access admin features.*"
        ),
        color=config.COLOR_PRIMARY
    )
    embed.set_footer(text="Secure OAuth2 authentication via Discord")

    # Add a button view for direct access
    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Open Dashboard",
        url=normalize_dashboard_url(config.FRONTEND_URL),
        style=discord.ButtonStyle.link
    ))

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ============================================================================
# SLASH COMMANDS - ADMIN ACTIONS
# ============================================================================

@bot.tree.command(name="session_create", description="Create a 99k jump session (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    channel="Channel to post the session announcement",
    payment_type="Payment type per spot",
    payment_amount="Amount per spot",
    spots="Number of spots",
    xanax_stack="Xanax stack size",
    start_delay_hours="Delay before start in hours"
)
@app_commands.choices(
    payment_type=[
        app_commands.Choice(name="Xanax", value="xanax"),
        app_commands.Choice(name="Erotic DVD", value="erotic_dvd"),
    ],
    xanax_stack=[
        app_commands.Choice(name="1 Xanax", value="1_xanax"),
        app_commands.Choice(name="2 Xanax", value="2_xanax"),
        app_commands.Choice(name="3 Xanax", value="3_xanax"),
        app_commands.Choice(name="Full Stack", value="full_stack"),
    ]
)
async def session_create(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    payment_type: app_commands.Choice[str],
    payment_amount: int,
    spots: int,
    xanax_stack: app_commands.Choice[str],
    start_delay_hours: int
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        request = CreateSessionRequest(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            payment_type=payment_type.value,
            payment_amount=payment_amount,
            spots=spots,
            xanax_stack=xanax_stack.value,
            start_delay_hours=start_delay_hours
        )
        response = await admin_handlers.create_session_handler(request, interaction.user.id)
        embed = create_success_embed(
            "Session Created",
            f"Session #{response.id} created.\n{response.message_url or 'Announcement posted.'}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Session create failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Session Create Failed", str(e)), ephemeral=True)


@bot.tree.command(name="session_list", description="List jump sessions (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(status="Filter by status", page="Page number", per_page="Sessions per page")
@app_commands.choices(
    status=[
        app_commands.Choice(name="Open", value="open"),
        app_commands.Choice(name="Locked", value="locked"),
        app_commands.Choice(name="Completed", value="completed"),
        app_commands.Choice(name="Cancelled", value="cancelled"),
    ]
)
async def session_list(
    interaction: discord.Interaction,
    status: app_commands.Choice[str] = None,
    page: int = 1,
    per_page: int = 10
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        data = await admin_handlers.list_sessions_handler(
            interaction.guild_id,
            status.value if status else None,
            page,
            per_page
        )
        if not data.sessions:
            await interaction.followup.send(embed=create_info_embed("Sessions", "No sessions found."), ephemeral=True)
            return
        lines = []
        for session in data.sessions:
            lines.append(
                f"#{session.id} • {session.status} • {session.xanax_stack} • {session.max_spots} spots"
            )
        embed = create_info_embed("Sessions", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Session list failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Session List Failed", str(e)), ephemeral=True)


@bot.tree.command(name="session_lock", description="Lock a jump session (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(session_id="Session ID to lock")
async def session_lock(interaction: discord.Interaction, session_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.lock_session_handler(session_id, interaction.user.id, source="discord")
        await interaction.followup.send(embed=create_success_embed("Session Locked", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Session lock failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Session Lock Failed", str(e)), ephemeral=True)


@bot.tree.command(name="session_cancel", description="Cancel a jump session (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(session_id="Session ID to cancel", reason="Optional cancellation reason")
async def session_cancel(interaction: discord.Interaction, session_id: int, reason: str = None):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.cancel_session_handler(
            session_id, interaction.user.id, reason=reason, source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Session Cancelled", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Session cancel failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Session Cancel Failed", str(e)), ephemeral=True)


@bot.tree.command(name="session_complete", description="Complete a jump session (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(session_id="Session ID to complete")
async def session_complete(interaction: discord.Interaction, session_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.complete_session_handler(
            session_id, interaction.user.id, source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Session Completed", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Session complete failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Session Complete Failed", str(e)), ephemeral=True)


@bot.tree.command(name="raffle_create", description="Create a raffle (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    channel="Channel to post the raffle announcement",
    prize="Prize description",
    ticket_payment_type="Ticket payment type",
    ticket_price="Ticket price",
    tickets_available="Total tickets available",
    max_tickets_per_user="Max tickets per user (0 = unlimited)",
    duration_hours="Raffle duration in hours"
)
@app_commands.choices(
    ticket_payment_type=[
        app_commands.Choice(name="Xanax", value="xanax"),
        app_commands.Choice(name="Erotic DVD", value="erotic_dvd"),
    ]
)
async def raffle_create(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    prize: str,
    ticket_payment_type: app_commands.Choice[str],
    ticket_price: int,
    tickets_available: int,
    max_tickets_per_user: int,
    duration_hours: int
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        request = CreateRaffleRequest(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            prize=prize,
            ticket_payment_type=ticket_payment_type.value,
            ticket_price=ticket_price,
            tickets_available=tickets_available,
            max_tickets_per_user=max_tickets_per_user,
            duration_hours=duration_hours
        )
        response = await admin_handlers.create_raffle_handler(request, interaction.user.id)
        embed = create_success_embed(
            "Raffle Created",
            f"Raffle #{response.raffle_id} created.\n{response.message_url or 'Announcement posted.'}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Raffle create failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Raffle Create Failed", str(e)), ephemeral=True)


@bot.tree.command(name="raffle_list", description="List raffles (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(status="Filter by status", page="Page number", per_page="Raffles per page")
@app_commands.choices(
    status=[
        app_commands.Choice(name="Active", value="active"),
        app_commands.Choice(name="Completed", value="completed"),
        app_commands.Choice(name="Cancelled", value="cancelled"),
    ]
)
async def raffle_list(
    interaction: discord.Interaction,
    status: app_commands.Choice[str] = None,
    page: int = 1,
    per_page: int = 10
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        data = await admin_handlers.list_raffles_handler(
            interaction.guild_id,
            status.value if status else None,
            page,
            per_page
        )
        if not data.raffles:
            await interaction.followup.send(embed=create_info_embed("Raffles", "No raffles found."), ephemeral=True)
            return
        lines = []
        for raffle in data.raffles:
            lines.append(
                f"#{raffle.raffle_id} • {raffle.status} • {raffle.prize}"
            )
        embed = create_info_embed("Raffles", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Raffle list failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Raffle List Failed", str(e)), ephemeral=True)


@bot.tree.command(name="raffle_draw", description="Draw a raffle winner (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(raffle_id="Raffle ID to draw")
async def raffle_draw(interaction: discord.Interaction, raffle_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.draw_raffle_handler(raffle_id, interaction.user.id, source="discord")
        await interaction.followup.send(embed=create_success_embed("Raffle Drawn", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Raffle draw failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Raffle Draw Failed", str(e)), ephemeral=True)


@bot.tree.command(name="raffle_cancel", description="Cancel a raffle (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(raffle_id="Raffle ID to cancel")
async def raffle_cancel(interaction: discord.Interaction, raffle_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.cancel_raffle_handler(raffle_id, interaction.user.id, source="discord")
        await interaction.followup.send(embed=create_success_embed("Raffle Cancelled", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Raffle cancel failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Raffle Cancel Failed", str(e)), ephemeral=True)


@bot.tree.command(name="policy_create", description="Create an insurance policy (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    provider="Provider Discord user",
    policy_name="Policy name",
    description="Policy description",
    cost_type="Premium payment type",
    cost_amount="Premium amount",
    coverage_type="Coverage type",
    payout_description="Payout terms",
    duration_hours="Policy duration in hours"
)
@app_commands.choices(
    cost_type=[
        app_commands.Choice(name="Xanax", value="xanax"),
        app_commands.Choice(name="Erotic DVD", value="erotic_dvd"),
    ],
    coverage_type=[
        app_commands.Choice(name="Xanax Stack", value="xanax_stack"),
        app_commands.Choice(name="Ecstasy After Stack", value="ecstasy_after_stack"),
        app_commands.Choice(name="All Drugs", value="all_drugs"),
    ]
)
async def policy_create(
    interaction: discord.Interaction,
    provider: discord.Member,
    policy_name: str,
    description: str,
    cost_type: app_commands.Choice[str],
    cost_amount: int,
    coverage_type: app_commands.Choice[str],
    payout_description: str,
    duration_hours: int
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        request = CreatePolicyRequest(
            guild_id=interaction.guild_id,
            provider_discord_id=provider.id,
            policy_name=policy_name,
            description=description,
            cost_type=cost_type.value,
            cost_amount=cost_amount,
            coverage_type=coverage_type.value,
            payout_description=payout_description,
            duration_hours=duration_hours
        )
        response = await admin_handlers.create_policy_handler(request, provider.id)
        embed = create_success_embed("Policy Created", f"Policy #{response.policy_id} created.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Policy create failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Policy Create Failed", str(e)), ephemeral=True)


@bot.tree.command(name="provider_approve", description="Approve or reject an insurance provider (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(provider_id="Provider ID", status="Approval status")
@app_commands.choices(
    status=[
        app_commands.Choice(name="Approved", value="approved"),
        app_commands.Choice(name="Rejected", value="rejected"),
        app_commands.Choice(name="Disabled", value="disabled"),
    ]
)
async def provider_approve(
    interaction: discord.Interaction,
    provider_id: int,
    status: app_commands.Choice[str]
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.approve_provider_handler(
            provider_id,
            status.value,
            interaction.user.id,
            source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Provider Updated", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Provider approval failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Provider Update Failed", str(e)), ephemeral=True)


@bot.tree.command(name="claim_approve", description="Approve an insurance claim (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(claim_id="Claim ID to approve")
async def claim_approve(interaction: discord.Interaction, claim_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.approve_claim_handler(claim_id, interaction.user.id, source="discord")
        await interaction.followup.send(embed=create_success_embed("Claim Approved", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Claim approve failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Claim Approve Failed", str(e)), ephemeral=True)


@bot.tree.command(name="claim_reject", description="Reject an insurance claim (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(claim_id="Claim ID to reject", notes="Optional rejection notes")
async def claim_reject(interaction: discord.Interaction, claim_id: int, notes: str = None):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.reject_claim_handler(
            claim_id,
            interaction.user.id,
            notes=notes,
            source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Claim Rejected", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Claim reject failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Claim Reject Failed", str(e)), ephemeral=True)


@bot.tree.command(name="blacklist_add", description="Add a user to the blacklist (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    member="Member to blacklist",
    reason="Reason for blacklist",
    expires_in_hours="Optional expiration in hours"
)
async def blacklist_add(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = None,
    expires_in_hours: int = None
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        expires_at = None
        if expires_in_hours:
            expires_at = datetime.utcnow() + timedelta(hours=expires_in_hours)
        response = await admin_handlers.add_blacklist_handler(
            interaction.guild_id,
            member.id,
            reason,
            interaction.user.id,
            expires_at=expires_at,
            source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Blacklist Updated", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Blacklist add failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Blacklist Add Failed", str(e)), ephemeral=True)


@bot.tree.command(name="blacklist_remove", description="Remove a user from the blacklist (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Member to remove from blacklist")
async def blacklist_remove(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.remove_blacklist_handler(
            interaction.guild_id,
            member.id,
            interaction.user.id,
            source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Blacklist Updated", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Blacklist remove failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Blacklist Remove Failed", str(e)), ephemeral=True)


@bot.tree.command(name="blacklist_list", description="List blacklisted users (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(search="Search by reason or ID")
async def blacklist_list(interaction: discord.Interaction, search: str = None):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        db = get_database()
        entries = await db.get_blacklist(interaction.guild_id)
        if search:
            lowered = search.lower()
            entries = [
                e for e in entries
                if lowered in str(e.get("discord_id"))
                or lowered in (e.get("reason") or "").lower()
            ]
        if not entries:
            await interaction.followup.send(embed=create_info_embed("Blacklist", "No blacklist entries found."), ephemeral=True)
            return
        lines = [
            f"<@{entry['discord_id']}> • {entry.get('reason', 'No reason')}"
            for entry in entries[:20]
        ]
        embed = create_info_embed("Blacklist", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Blacklist list failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Blacklist List Failed", str(e)), ephemeral=True)


@bot.tree.command(name="settings_show", description="Show current guild settings (Admin only)")
@app_commands.default_permissions(administrator=True)
async def settings_show(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        db = get_database()
        settings = await db.get_guild_settings(interaction.guild_id)
        embed = create_info_embed(
            "Guild Settings",
            (
                f"Reservation Timeout: {settings.get('reservation_timeout_minutes')} minutes\n"
                f"Auto Complete: {'Enabled' if settings.get('auto_complete_enabled') else 'Disabled'}"
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Settings show failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Settings Show Failed", str(e)), ephemeral=True)


@bot.tree.command(name="settings_update", description="Update guild settings (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    host_role="Role for 99k hosts",
    insurer_role="Role for insurers",
    admin_role="Role for dashboard admins",
    jump_channel="Channel for 99k jumps",
    insurance_channel="Channel for insurance",
    raffle_channel="Channel for raffles",
    reservation_timeout_minutes="Reservation timeout in minutes",
    auto_complete_enabled="Auto-complete sessions"
)
async def settings_update(
    interaction: discord.Interaction,
    host_role: discord.Role = None,
    insurer_role: discord.Role = None,
    admin_role: discord.Role = None,
    jump_channel: discord.TextChannel = None,
    insurance_channel: discord.TextChannel = None,
    raffle_channel: discord.TextChannel = None,
    reservation_timeout_minutes: int = None,
    auto_complete_enabled: bool = None
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        request = UpdateSettingsRequest(
            guild_id=interaction.guild_id,
            host99k_role_id=host_role.id if host_role else None,
            insurer_role_id=insurer_role.id if insurer_role else None,
            admin_role_id=admin_role.id if admin_role else None,
            jump_99k_channel_id=jump_channel.id if jump_channel else None,
            insurance_channel_id=insurance_channel.id if insurance_channel else None,
            raffle_channel_id=raffle_channel.id if raffle_channel else None,
            reservation_timeout_minutes=reservation_timeout_minutes,
            auto_complete_enabled=auto_complete_enabled
        )
        updated = await admin_handlers.update_settings_handler(
            request,
            interaction.user.id,
            source="discord"
        )
        embed = create_success_embed(
            "Settings Updated",
            f"Reservation Timeout: {updated.reservation_timeout_minutes} minutes\n"
            f"Auto Complete: {'Enabled' if updated.auto_complete_enabled else 'Disabled'}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Settings update failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Settings Update Failed", str(e)), ephemeral=True)


@bot.tree.command(name="audit_log", description="View recent audit log entries (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(limit="Number of entries to show (max 20)")
async def audit_log(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        db = get_database()
        entries = await db.get_audit_logs(guild_id=interaction.guild_id, limit=min(limit, 20))
        if not entries:
            await interaction.followup.send(embed=create_info_embed("Audit Log", "No audit entries found."), ephemeral=True)
            return
        lines = []
        for entry in entries:
            lines.append(
                f"{entry['action']} • actor {entry.get('actor_discord_id')} • target {entry.get('target_id')}"
            )
        embed = create_info_embed("Audit Log", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Audit log failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Audit Log Failed", str(e)), ephemeral=True)


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
