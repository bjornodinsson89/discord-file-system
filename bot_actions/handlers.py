"""
Bot action handlers used by Discord commands.
"""

import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from utils import get_database, get_torn_api
from bot_actions.schemas import *
import config

log = logging.getLogger("happy_jumper.bot_actions")

# ============================================================================
# BOT INSTANCE (injected at runtime)
# ============================================================================

_bot_instance: Optional[object] = None

def set_bot_instance(bot: object):
    """Set the bot instance for posting to Discord."""
    global _bot_instance
    _bot_instance = bot
    log.info("Bot instance registered with bot actions")

def get_bot() -> object:
    if _bot_instance is None:
        raise RuntimeError("Bot runtime is unavailable for this action")
    return _bot_instance


def _session_embed_payload(session: Dict[str, Any], signups: Optional[list[Dict[str, Any]]] = None) -> Dict[str, Any]:
    signups = signups or []
    paid = sum(1 for s in signups if s.get("status") == "paid")
    reserved = sum(1 for s in signups if s.get("status") == "reserved")
    available = max(session.get("max_spots", 0) - len(signups), 0)

    if session.get("payment_type") == "xanax":
        payment = f"{session.get('payment_amount', 0)}x Xanax"
    elif session.get("payment_type") == "erotic_dvd":
        payment = f"{session.get('payment_amount', 0)}x Erotic DVD"
    else:
        payment = str(session.get("payment_amount", ""))

    status = session.get("status", "open").title()
    fields = [
        {"name": "Host", "value": f"<@{session['host_discord_id']}>\nTorn: `{session['host_torn_id']}`", "inline": True},
        {"name": "Spots", "value": f"Paid: {paid}\nReserved: {reserved}\nAvailable: {available}", "inline": True},
        {"name": "Payment", "value": payment, "inline": True},
    ]

    return {
        "title": f"Happy Jump #{session['id']} - {status}",
        "description": "Manage this session from Discord.",
        "color": config.COLOR_PRIMARY,
        "fields": fields,
        "footer": {"text": "Happy Jumper Bot"},
    }


def _raffle_embed_payload(raffle: Dict[str, Any], entries: Optional[list[Dict[str, Any]]] = None, winner: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entries = entries or []
    status = raffle.get("status", "active").title()
    description = f"Tickets sold: {len(entries)}/{raffle.get('tickets_available', 0)}"
    if winner:
        description += f"\nWinner: <@{winner['discord_id']}>"

    return {
        "title": f"Raffle #{raffle['raffle_id']} - {status}",
        "description": description,
        "color": config.COLOR_INFO,
        "fields": [
            {"name": "Prize", "value": raffle.get("prize", "Unknown"), "inline": False},
            {"name": "Ticket Price", "value": str(raffle.get("ticket_price", 0)), "inline": True},
        ],
        "footer": {"text": "Happy Jumper Bot"},
    }

# ============================================================================
# SESSION HANDLERS
# ============================================================================

async def create_session_handler(
    request: CreateSessionRequest,
    admin_discord_id: int
) -> SessionResponse:
    """Create a 99k jump session and post announcement to Discord."""
    db = get_database()

    bot = get_bot()
    guild = bot.get_guild(request.guild_id)
    if not guild:
        raise ValueError("Guild not found")
    channel = guild.get_channel(request.channel_id)
    if not channel:
        raise ValueError("Channel not found")

    api_key_data = await db.get_user_api_key(admin_discord_id)
    if not api_key_data:
        raise ValueError("Admin must have API key registered to create sessions")

    host_torn_id = api_key_data['torn_user_id']
    torn_api = get_torn_api()
    torn_time = await torn_api.get_torn_time()
    created_tct = torn_time
    estimated_jump_tct = created_tct + (request.start_delay_hours * 3600)

    payment_item_id = None
    if request.payment_type == "erotic_dvd":
        payment_item_id = config.DVD_ITEM_ID
    elif request.payment_type == "xanax":
        payment_item_id = config.XANAX_ITEM_ID

    xanax_count_map = {"1_xanax": 1, "2_xanax": 2, "3_xanax": 3, "full_stack": 100}
    xanax_count = xanax_count_map.get(request.xanax_stack, 1)

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

    await db.update_jump_session(
        session_id,
        xanax_stack=request.xanax_stack,
    )

    session_data = await db.get_jump_session(session_id)

    from utils.embeds import create_session_announcement_embed
    from views import JumpSessionView

    guild = bot.get_guild(request.guild_id)
    channel = guild.get_channel(request.channel_id)
    embed = create_session_announcement_embed(session_data, guild)
    view = JumpSessionView(session_id)
    msg = await channel.send(embed=embed, view=view)
    message_id = int(msg.id)

    await db.update_jump_session(
        session_id,
        announcement_message_id=message_id,
        announcement_channel_id=request.channel_id,
    )

    await db.log_audit(
        admin_discord_id,
        "session_created",
        "session",
        session_id,
        {"channel_id": request.channel_id}
    )

    session_data = await db.get_jump_session(session_id)
    message_url = f"https://discord.com/channels/{request.guild_id}/{request.channel_id}/{message_id}"

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


async def lock_session_handler(session_id: int, actor_discord_id: int, source: str = "discord") -> SuccessResponse:
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
    source: str = "discord"
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
    source: str = "discord"
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
    guild = bot.get_guild(request.guild_id)
    if not guild:
        raise ValueError("Guild not found")
    channel = guild.get_channel(request.channel_id)
    if not channel:
        raise ValueError("Channel not found")

    end_time = datetime.utcnow() + timedelta(hours=request.duration_hours)

    payment_item_id = None
    if request.ticket_payment_type == "erotic_dvd":
        payment_item_id = config.DVD_ITEM_ID
    elif request.ticket_payment_type == "xanax":
        payment_item_id = config.XANAX_ITEM_ID

    max_tickets_per_user = request.max_tickets_per_user if request.max_tickets_per_user > 0 else None

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
            request.tickets_available, max_tickets_per_user, end_time
        )
        raffle_id = row['raffle_id']


    raffle_data = await _get_raffle(raffle_id)

    if _is_web_mode():
        rest_client = DiscordRestClient()
        embed_payload = _raffle_embed_payload(raffle_data)
        message = await rest_client.send_message(
            request.channel_id,
            embeds=[embed_payload],
            content="New raffle is live. Use Discord interactions.",
        )
        message_id = int(message["id"])
    else:
        from utils.embeds import create_raffle_announcement_embed
        from views import RaffleView

        guild = bot.get_guild(request.guild_id)
        channel = guild.get_channel(request.channel_id)
        embed = create_raffle_announcement_embed(raffle_data, guild)
        view = RaffleView(raffle_id)
        msg = await channel.send(embed=embed, view=view)
        message_id = int(msg.id)

    async with db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE raffles SET announcement_message_id = $1, announcement_channel_id = $2 WHERE raffle_id = $3",
            message_id, request.channel_id, raffle_id
        )

    await db.log_audit(
        admin_discord_id,
        "raffle_created",
        "raffle",
        raffle_id,
        {"channel_id": request.channel_id}
    )

    raffle_data = await _get_raffle(raffle_id)
    message_url = f"https://discord.com/channels/{request.guild_id}/{request.channel_id}/{message_id}"
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
    source: str = "discord"
) -> SuccessResponse:
    """Draw a winner for a raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)

    if not raffle:
        raise ValueError("Raffle not found")
    if raffle['status'] not in ('active', 'open'):
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
    source: str = "discord"
) -> SuccessResponse:
    """Cancel a raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)

    if not raffle:
        raise ValueError("Raffle not found")
    if raffle['status'] not in ('active', 'open'):
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
        
    
    # Log audit
    await db.log_audit(
        provider_discord_id,
        "policy_created",
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
    source: str = "discord"
) -> SuccessResponse:
    """Approve, reject, or disable an insurance provider."""
    db = get_database()
    provider = await db.get_provider_by_id(provider_id)

    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if status == "approved":
        await db.approve_provider(provider_id, actor_discord_id)
    elif status == "rejected":
        await db.reject_provider(provider_id, actor_discord_id)
    elif status == "disabled":
        await db.set_provider_active(provider_id, False)
    else:
        raise HTTPException(status_code=400, detail="Invalid status. Must be 'approved', 'rejected', or 'disabled'")

    await db.log_audit(
        actor_discord_id, "provider_approval_updated", "provider", provider_id,
        {"status": status}, source=source
    )

    return SuccessResponse(message=f"Provider {status}")


async def approve_claim_handler(
    claim_id: int,
    actor_discord_id: int,
    source: str = "discord"
) -> SuccessResponse:
    """Approve an insurance claim."""
    db = get_database()
    claim = await db.get_claim(claim_id)

    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    if claim['status'] != 'pending':
        raise HTTPException(status_code=400, detail="Claim is not pending")

    await db.approve_claim(claim_id, actor_discord_id)
    await db.log_audit(
        actor_discord_id, "claim_approved", "claim", claim_id, source=source
    )

    return SuccessResponse(message="Claim approved")


async def reject_claim_handler(
    claim_id: int,
    actor_discord_id: int,
    notes: Optional[str] = None,
    source: str = "discord"
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
    source: str = "discord"
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
    source: str = "discord"
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
    source: str = "discord"
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
    
    return session.get('announcement_channel_id')


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
        session = await db.get_jump_session(session_id)
        if not session or not session.get('announcement_message_id'):
            return

        channel_id = session.get('announcement_channel_id')
        if not channel_id:
            settings = await db.get_guild_settings(session['guild_id'])
            channel_id = settings.get('jump_99k_channel_id')
        if not channel_id:
            return

        signups = await db.get_session_signups(session_id)

        if _is_web_mode():
            rest_client = DiscordRestClient()
            await rest_client.edit_message(
                int(channel_id),
                int(session['announcement_message_id']),
                embeds=[_session_embed_payload(session, signups)],
                content="Session updated.",
            )
            return

        bot = get_bot()
        guild = bot.get_guild(session['guild_id'])
        if not guild:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        message = await channel.fetch_message(session['announcement_message_id'])
        readiness = await db.get_session_readiness(session_id)
        from utils.embeds import create_jump_session_embed
        from views import JumpSessionView

        embed = create_jump_session_embed(session, signups, readiness)
        view = JumpSessionView(session_id) if session['status'] in ('open', 'locked') else None
        await message.edit(embed=embed, view=view)
    except Exception as e:
        log.error(f"Error updating session message: {e}")


async def update_raffle_message(raffle_id: int, winner: Optional[Dict] = None):
    """Update the Discord message for a raffle after changes."""
    try:
        db = get_database()
        raffle = await db.get_raffle(raffle_id)
        if not raffle or not raffle.get('announcement_message_id'):
            return

        settings = await db.get_guild_settings(raffle['guild_id'])
        channel_id = raffle.get('announcement_channel_id') or settings.get('raffle_channel_id')
        if not channel_id:
            return

        entries = await db.get_raffle_entries(raffle_id)

        if _is_web_mode():
            rest_client = DiscordRestClient()
            await rest_client.edit_message(
                int(channel_id),
                int(raffle['announcement_message_id']),
                embeds=[_raffle_embed_payload(raffle, entries, winner)],
                content="Raffle updated.",
            )
            if winner:
                await rest_client.send_message(
                    int(channel_id),
                    embeds=[_raffle_embed_payload(raffle, entries, winner)],
                    content=f"🎉 Winner: <@{winner['discord_id']}> (Ticket #{winner['ticket_number']})",
                )
            return

        bot = get_bot()
        guild = bot.get_guild(raffle['guild_id'])
        if not guild:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        message = await channel.fetch_message(raffle['announcement_message_id'])

        from utils.embeds import create_raffle_embed, create_raffle_winner_embed
        from views import RaffleView

        if raffle['status'] == 'completed' and winner:
            embed = create_raffle_winner_embed(raffle, winner)
        else:
            embed = create_raffle_embed(raffle, entries)

        view = RaffleView(raffle_id) if raffle['status'] in ('active', 'open') else None
        await message.edit(embed=embed, view=view)

        if winner:
            winner_embed = create_raffle_winner_embed(raffle, winner)
            await channel.send(embed=winner_embed)
    except Exception as e:
        log.error(f"Error updating raffle message: {e}")
