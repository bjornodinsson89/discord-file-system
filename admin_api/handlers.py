"""
Admin API Handlers
Implements create/edit actions and "post to Discord now" integration.
"""

import logging
from typing import Optional, Dict
from datetime import datetime, timedelta
import discord

from utils import get_database, get_torn_api
from utils.embeds import *
from admin_api.schemas import *
import config

log = logging.getLogger("happy_jumper.admin_api")

# ============================================================================
# BOT INSTANCE (injected at runtime)
# ============================================================================

_bot_instance: Optional[discord.Client] = None

def set_bot_instance(bot: discord.Client):
    """Set the bot instance for posting to Discord."""
    global _bot_instance
    _bot_instance = bot
    log.info("Bot instance registered with admin API")


def get_bot() -> discord.Client:
    """Get the bot instance."""
    if _bot_instance is None:
        raise RuntimeError("Bot instance not initialized")
    return _bot_instance


# ============================================================================
# SESSION HANDLERS
# ============================================================================

async def create_session_handler(
    request: CreateSessionRequest,
    admin_discord_id: int
) -> SessionResponse:
    """
    Create a 99k jump session and post announcement to Discord.
    
    Steps:
    1. Validate request data
    2. Insert session into database
    3. Post announcement embed to Discord channel
    4. Store message_id back to database
    5. Return session data with message link
    """
    db = get_database()
    bot = get_bot()
    
    # Get guild and channel
    guild = bot.get_guild(request.guild_id)
    if not guild:
        raise ValueError("Guild not found")
    
    channel = guild.get_channel(request.channel_id)
    if not channel:
        raise ValueError("Channel not found")
    
    # Get admin's Torn ID
    api_key_data = await db.get_user_api_key(admin_discord_id)
    if not api_key_data:
        raise ValueError("Admin must have API key registered to create sessions")
    
    host_torn_id = api_key_data['torn_user_id']
    
    # Calculate timing
    torn_api = get_torn_api()
    torn_time = await torn_api.get_torn_time()
    created_tct = torn_time
    estimated_jump_tct = created_tct + (request.start_delay_hours * 3600)
    
    # Determine payment item ID
    payment_item_id = None
    if request.payment_type == "erotic_dvd":
        payment_item_id = config.DVD_ITEM_ID
    elif request.payment_type == "xanax":
        payment_item_id = config.XANAX_ITEM_ID
    
    # Convert xanax_stack to xanax_count for database (backwards compatibility)
    xanax_count_map = {
        "1_xanax": 1,
        "2_xanax": 2,
        "3_xanax": 3,
        "full_stack": 100  # Represent full stack as 100
    }
    xanax_count = xanax_count_map.get(request.xanax_stack, 1)
    
    # Create session in database
    session_id = await db.create_jump_session(
        guild_id=request.guild_id,
        host_discord_id=admin_discord_id,
        host_torn_id=host_torn_id,
        max_spots=request.spots,
        xanax_count=xanax_count,
        start_in_hours=request.start_delay_hours,
        created_tct=created_tct,
        estimated_jump_tct=estimated_jump_tct,
        payment_type=request.payment_type,
        payment_amount=request.payment_amount,
        payment_item_id=payment_item_id
    )
    
    # Update with dashboard metadata
    await db.update_jump_session(
        session_id,
        xanax_stack=request.xanax_stack,
        created_by_dashboard=True,
        dashboard_admin_id=admin_discord_id
    )
    
    # Create and post announcement embed
    session_data = await db.get_jump_session(session_id)
    embed = create_session_announcement_embed(session_data, guild)
    
    # Import views here to avoid circular dependency
    from views import JumpSessionView
    view = JumpSessionView(session_id)
    
    message = await channel.send(embed=embed, view=view)
    
    # Store message ID
    await db.update_jump_session(session_id, announcement_message_id=message.id)
    
    # Log audit
    await db.log_audit(
        admin_discord_id,
        "session_created_dashboard",
        "session",
        session_id,
        {"channel_id": request.channel_id}
    )
    
    # Return response
    session_data = await db.get_jump_session(session_id)
    message_url = f"https://discord.com/channels/{request.guild_id}/{request.channel_id}/{message.id}"
    
    return SessionResponse(
        **session_data,
        message_url=message_url
    )


async def list_sessions_handler(
    guild_id: int,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 50
) -> SessionListResponse:
    """List sessions for a guild with pagination and filtering."""
    db = get_database()
    
    # Get sessions
    if status:
        sessions = await db.get_active_sessions(guild_id)
        sessions = [s for s in sessions if s['status'] == status]
    else:
        sessions = await db.get_session_history(guild_id, limit=per_page * page)
    
    # Paginate
    start = (page - 1) * per_page
    end = start + per_page
    paginated = sessions[start:end]
    
    # Build message URLs
    for session in paginated:
        if session.get('announcement_message_id'):
            channel_id = await _get_session_channel_id(session['id'])
            if channel_id:
                session['message_url'] = (
                    f"https://discord.com/channels/{guild_id}/"
                    f"{channel_id}/{session['announcement_message_id']}"
                )
    
    return SessionListResponse(
        sessions=[SessionResponse(**s) for s in paginated],
        total=len(sessions),
        page=page,
        per_page=per_page
    )


async def lock_session_handler(session_id: int, actor_discord_id: int, source: str = "dashboard") -> SuccessResponse:
    """Lock a session to prevent new signups."""
    db = get_database()
    session = await db.get_jump_session(session_id)

    if not session:
        raise ValueError("Session not found")
    if session['status'] not in ('open',):
        raise ValueError("Session cannot be locked")

    await db.lock_session(session_id)
    await db.log_audit(
        actor_discord_id, "session_locked", "session", session_id,
        guild_id=session['guild_id'], source=source
    )
    await update_session_message(session_id)

    return SuccessResponse(message="Session locked")


async def cancel_session_handler(
    session_id: int,
    actor_discord_id: int,
    reason: Optional[str] = None,
    source: str = "dashboard"
) -> SuccessResponse:
    """Cancel a session."""
    db = get_database()
    session = await db.get_jump_session(session_id)

    if not session:
        raise ValueError("Session not found")
    if session['status'] in ('completed', 'cancelled'):
        raise ValueError("Session already ended")

    await db.cancel_session(session_id)
    await db.log_audit(
        actor_discord_id, "session_cancelled", "session", session_id,
        {"reason": reason}, guild_id=session['guild_id'], source=source
    )
    await update_session_message(session_id)

    return SuccessResponse(message="Session cancelled")


async def complete_session_handler(
    session_id: int,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SuccessResponse:
    """Mark a session as completed."""
    db = get_database()
    session = await db.get_jump_session(session_id)

    if not session:
        raise ValueError("Session not found")
    if session['status'] not in ('open', 'locked'):
        raise ValueError("Session cannot be completed")

    await db.complete_session(session_id)
    await db.log_audit(
        actor_discord_id, "session_completed", "session", session_id,
        guild_id=session['guild_id'], source=source
    )
    await db.update_host_reputation(
        session['host_discord_id'], session['host_torn_id'], completed=True
    )
    await update_session_message(session_id)

    return SuccessResponse(message="Session completed")


# ============================================================================
# RAFFLE HANDLERS
# ============================================================================

async def create_raffle_handler(
    request: CreateRaffleRequest,
    admin_discord_id: int
) -> RaffleResponse:
    """Create a raffle and post announcement to Discord."""
    db = get_database()
    bot = get_bot()
    
    # Get guild and channel
    guild = bot.get_guild(request.guild_id)
    if not guild:
        raise ValueError("Guild not found")
    
    channel = guild.get_channel(request.channel_id)
    if not channel:
        raise ValueError("Channel not found")
    
    # Calculate end time
    end_time = datetime.utcnow() + timedelta(hours=request.duration_hours)
    
    # Determine payment item ID
    payment_item_id = None
    if request.ticket_payment_type == "erotic_dvd":
        payment_item_id = config.DVD_ITEM_ID
    elif request.ticket_payment_type == "xanax":
        payment_item_id = config.XANAX_ITEM_ID
    
    # Create raffle in database
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO raffles (
                guild_id, creator_discord_id, prize,
                ticket_payment_type, ticket_price, ticket_payment_item_id,
                tickets_available, max_tickets_per_user, status, end_time
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'active', $9)
            RETURNING raffle_id
        """, request.guild_id, admin_discord_id, request.prize,
            request.ticket_payment_type, request.ticket_price, payment_item_id,
            request.tickets_available, request.max_tickets_per_user, end_time
        )
        raffle_id = row['raffle_id']
        
        # Update with dashboard metadata
        await conn.execute("""
            UPDATE raffles
            SET created_by_dashboard = TRUE, dashboard_admin_id = $1
            WHERE raffle_id = $2
        """, admin_discord_id, raffle_id)
    
    # Create and post announcement embed
    raffle_data = await _get_raffle(raffle_id)
    embed = create_raffle_announcement_embed(raffle_data, guild)
    
    # Import views here to avoid circular dependency
    from views import RaffleView
    view = RaffleView(raffle_id)
    
    message = await channel.send(embed=embed, view=view)
    
    # Store message ID
    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE raffles SET announcement_message_id = $1 WHERE raffle_id = $2",
            message.id, raffle_id
        )
    
    # Log audit
    await db.log_audit(
        admin_discord_id,
        "raffle_created_dashboard",
        "raffle",
        raffle_id,
        {"channel_id": request.channel_id}
    )
    
    # Return response
    raffle_data = await _get_raffle(raffle_id)
    message_url = f"https://discord.com/channels/{request.guild_id}/{request.channel_id}/{message.id}"
    raffle_data['message_url'] = message_url
    
    return RaffleResponse(**raffle_data)


async def list_raffles_handler(
    guild_id: int,
    status: Optional[str] = None,
    page: int = 1,
    per_page: int = 50
) -> RaffleListResponse:
    """List raffles for a guild with pagination and filtering."""
    db = get_database()
    
    query = "SELECT * FROM raffles WHERE guild_id = $1"
    params = [guild_id]
    
    if status:
        query += " AND status = $2"
        params.append(status)
    
    query += " ORDER BY created_at DESC LIMIT $" + str(len(params) + 1)
    params.append(per_page * page)
    
    async with db.pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
        raffles = [dict(row) for row in rows]
    
    # Paginate
    start = (page - 1) * per_page
    end = start + per_page
    paginated = raffles[start:end]
    
    return RaffleListResponse(
        raffles=[RaffleResponse(**r) for r in paginated],
        total=len(raffles),
        page=page,
        per_page=per_page
    )


async def draw_raffle_handler(
    raffle_id: int,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SuccessResponse:
    """Draw a winner for a raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)

    if not raffle:
        raise ValueError("Raffle not found")
    if raffle['status'] != 'active':
        raise ValueError("Raffle is not active")

    winner = await db.draw_raffle_winner(raffle_id)
    await db.log_audit(
        actor_discord_id, "raffle_drawn", "raffle", raffle_id,
        {"winner_discord_id": winner['discord_id'] if winner else None},
        guild_id=raffle['guild_id'], source=source
    )
    await update_raffle_message(raffle_id, winner)

    if winner:
        return SuccessResponse(
            message=f"Winner drawn: <@{winner['discord_id']}> (Ticket #{winner['ticket_number']})"
        )
    return SuccessResponse(message="Raffle completed with no entries")


async def cancel_raffle_handler(
    raffle_id: int,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SuccessResponse:
    """Cancel a raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)

    if not raffle:
        raise ValueError("Raffle not found")
    if raffle['status'] != 'active':
        raise ValueError("Raffle is not active")

    await db.cancel_raffle(raffle_id)
    await db.log_audit(
        actor_discord_id, "raffle_cancelled", "raffle", raffle_id,
        guild_id=raffle['guild_id'], source=source
    )
    await update_raffle_message(raffle_id)

    return SuccessResponse(message="Raffle cancelled")


# ============================================================================
# INSURANCE HANDLERS
# ============================================================================

async def create_policy_handler(
    request: CreatePolicyRequest,
    provider_discord_id: int
) -> PolicyResponse:
    """Create an insurance policy for a provider."""
    db = get_database()
    
    # Get or create provider
    provider = await db.get_provider(provider_discord_id)
    if not provider:
        # Get Torn ID
        api_key_data = await db.get_user_api_key(provider_discord_id)
        if not api_key_data:
            raise ValueError("Provider must have API key registered")
        
        # Create provider
        async with db.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO insurance_providers (discord_id, torn_user_id)
                VALUES ($1, $2)
                RETURNING provider_id
            """, provider_discord_id, api_key_data['torn_user_id'])
            provider_id = row['provider_id']
    else:
        provider_id = provider['provider_id']
    
    # Create policy
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO insurance_policies (
                provider_id, name, description,
                cost_type, cost_amount, coverage_type,
                payout_description, duration_hours, active
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE)
            RETURNING policy_id
        """, provider_id, request.policy_name, request.description,
            request.cost_type, request.cost_amount, request.coverage_type,
            request.payout_description, request.duration_hours
        )
        policy_id = row['policy_id']
        
        # Update with dashboard metadata
        await conn.execute("""
            UPDATE insurance_policies
            SET created_by_dashboard = TRUE, dashboard_admin_id = $1
            WHERE policy_id = $2
        """, provider_discord_id, policy_id)
    
    # Log audit
    await db.log_audit(
        provider_discord_id,
        "policy_created_dashboard",
        "policy",
        policy_id,
        {"provider_id": provider_id}
    )
    
    # Return response
    policy_data = await _get_policy(policy_id)
    return PolicyResponse(**policy_data)


async def approve_provider_handler(
    provider_id: int,
    status: str,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SuccessResponse:
    """Approve, reject, or disable an insurance provider."""
    db = get_database()
    provider = await db.get_provider_by_id(provider_id)

    if not provider:
        raise ValueError("Provider not found")

    if status == "approved":
        await db.approve_provider(provider_id, actor_discord_id)
    elif status == "rejected":
        await db.reject_provider(provider_id, actor_discord_id)
    elif status == "disabled":
        await db.set_provider_active(provider_id, False)
    else:
        raise ValueError("Invalid status")

    await db.log_audit(
        actor_discord_id, "provider_approval_updated", "provider", provider_id,
        {"status": status}, source=source
    )

    return SuccessResponse(message=f"Provider {status}")


async def approve_claim_handler(
    claim_id: int,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SuccessResponse:
    """Approve an insurance claim."""
    db = get_database()
    claim = await db.get_claim(claim_id)

    if not claim:
        raise ValueError("Claim not found")
    if claim['status'] != 'pending':
        raise ValueError("Claim is not pending")

    await db.approve_claim(claim_id, actor_discord_id)
    await db.log_audit(
        actor_discord_id, "claim_approved", "claim", claim_id, source=source
    )

    return SuccessResponse(message="Claim approved")


async def reject_claim_handler(
    claim_id: int,
    actor_discord_id: int,
    notes: Optional[str] = None,
    source: str = "dashboard"
) -> SuccessResponse:
    """Reject an insurance claim."""
    db = get_database()
    claim = await db.get_claim(claim_id)

    if not claim:
        raise ValueError("Claim not found")
    if claim['status'] != 'pending':
        raise ValueError("Claim is not pending")

    await db.reject_claim(claim_id, actor_discord_id, notes)
    await db.log_audit(
        actor_discord_id, "claim_rejected", "claim", claim_id,
        {"notes": notes}, source=source
    )

    return SuccessResponse(message="Claim rejected")


async def add_blacklist_handler(
    guild_id: int,
    discord_id: int,
    reason: Optional[str],
    actor_discord_id: int,
    expires_at: Optional[datetime] = None,
    source: str = "dashboard"
) -> SuccessResponse:
    """Add a user to the blacklist."""
    db = get_database()
    await db.add_to_blacklist(
        guild_id,
        discord_id,
        reason or "No reason provided",
        actor_discord_id,
        expires_at
    )
    await db.log_audit(
        actor_discord_id, "blacklist_added", "member", discord_id,
        {"reason": reason, "expires_at": expires_at.isoformat() if expires_at else None},
        guild_id=guild_id, source=source
    )

    return SuccessResponse(message="User added to blacklist")


async def remove_blacklist_handler(
    guild_id: int,
    discord_id: int,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SuccessResponse:
    """Remove a user from the blacklist."""
    db = get_database()
    await db.remove_from_blacklist(guild_id, discord_id)
    await db.log_audit(
        actor_discord_id, "blacklist_removed", "member", discord_id,
        guild_id=guild_id, source=source
    )

    return SuccessResponse(message="User removed from blacklist")


async def update_settings_handler(
    request: UpdateSettingsRequest,
    actor_discord_id: int,
    source: str = "dashboard"
) -> SettingsResponse:
    """Update guild settings and return refreshed settings."""
    db = get_database()

    updates = {}
    for key, value in request.model_dump().items():
        if key != "guild_id" and value is not None:
            updates[key] = value

    if updates:
        await db.update_guild_settings(request.guild_id, **updates)
        await db.log_audit(
            actor_discord_id, "settings_updated", "guild", request.guild_id,
            updates, guild_id=request.guild_id, source=source
        )

    settings = await db.get_guild_settings(request.guild_id)
    return SettingsResponse(**settings)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def _get_session_channel_id(session_id: int) -> Optional[int]:
    """Get the channel ID where a session was posted."""
    db = get_database()
    session = await db.get_jump_session(session_id)
    if not session:
        return None
    
    settings = await db.get_guild_settings(session['guild_id'])
    return settings.get('jump_99k_channel_id')


async def _get_raffle(raffle_id: int) -> Dict:
    """Get raffle data."""
    db = get_database()
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM raffles WHERE raffle_id = $1", raffle_id)
        return dict(row) if row else None


async def _get_policy(policy_id: int) -> Dict:
    """Get policy data."""
    db = get_database()
    async with db.pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM insurance_policies WHERE policy_id = $1", policy_id)
        return dict(row) if row else None


async def update_session_message(session_id: int):
    """Update the Discord message for a session after changes."""
    try:
        db = get_database()
        bot = get_bot()
        
        session = await db.get_jump_session(session_id)
        if not session or not session.get('announcement_message_id'):
            return
        
        guild = bot.get_guild(session['guild_id'])
        if not guild:
            return
        
        settings = await db.get_guild_settings(session['guild_id'])
        channel_id = settings.get('jump_99k_channel_id')
        if not channel_id:
            return
        
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        
        try:
            message = await channel.fetch_message(session['announcement_message_id'])
            
            # Get updated data
            signups = await db.get_session_signups(session_id)
            readiness = await db.get_session_readiness(session_id)
            
            # Rebuild embed
            from utils.embeds import create_jump_session_embed
            embed = create_jump_session_embed(session, signups, readiness)
            
            # Update view based on status
            from views import JumpSessionView
            if session['status'] in ('open', 'locked'):
                view = JumpSessionView(session_id)
            else:
                view = None  # No buttons for completed/cancelled
            
            await message.edit(embed=embed, view=view)
            
        except discord.NotFound:
            log.warning(f"Session message {session['announcement_message_id']} not found")
        except discord.Forbidden:
            log.warning(f"Cannot edit session message - missing permissions")
            
    except Exception as e:
        log.error(f"Error updating session message: {e}")


async def update_raffle_message(raffle_id: int, winner: Optional[Dict] = None):
    """Update the Discord message for a raffle after changes."""
    try:
        db = get_database()
        bot = get_bot()
        
        raffle = await db.get_raffle(raffle_id)
        if not raffle or not raffle.get('announcement_message_id'):
            return
        
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
        
        try:
            message = await channel.fetch_message(raffle['announcement_message_id'])
            
            # Get updated data
            entries = await db.get_raffle_entries(raffle_id)
            
            # Rebuild embed based on status
            from utils.embeds import create_raffle_embed, create_raffle_winner_embed
            
            if raffle['status'] == 'completed' and winner:
                embed = create_raffle_winner_embed(raffle, winner)
            else:
                embed = create_raffle_embed(raffle, entries)
            
            # Update view based on status
            from views import RaffleView
            if raffle['status'] == 'active':
                view = RaffleView(raffle_id)
            else:
                view = None  # No buttons for completed/cancelled
            
            await message.edit(embed=embed, view=view)
            
            # If there's a winner, also send a new message
            if winner:
                winner_embed = create_raffle_winner_embed(raffle, winner)
                await channel.send(embed=winner_embed)
            
        except discord.NotFound:
            log.warning(f"Raffle message {raffle['announcement_message_id']} not found")
        except discord.Forbidden:
            log.warning(f"Cannot edit raffle message - missing permissions")
            
    except Exception as e:
        log.error(f"Error updating raffle message: {e}")
