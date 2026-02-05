"""
Admin API Routes
FastAPI route definitions for all admin endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List

from web.permissions import get_current_user, require_guild_admin
from admin_api.schemas import *
from admin_api.handlers import *
from utils import get_database

# ============================================================================
# GUILD INFO ROUTER (for dropdowns)
# ============================================================================

guild_router = APIRouter()

@guild_router.get("/{guild_id}/channels")
async def get_guild_channels(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get list of text channels in guild."""
    await require_guild_admin(guild_id, user)
    
    try:
        bot = get_bot()
        guild = bot.get_guild(guild_id)
        
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found or bot not in guild")
        
        channels = [
            {"id": str(c.id), "name": c.name, "type": "text"}
            for c in guild.text_channels
            if c.permissions_for(guild.me).send_messages
        ]
        
        return {"channels": channels}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@guild_router.get("/{guild_id}/roles")
async def get_guild_roles(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get list of roles in guild."""
    await require_guild_admin(guild_id, user)
    
    try:
        bot = get_bot()
        guild = bot.get_guild(guild_id)
        
        if not guild:
            raise HTTPException(status_code=404, detail="Guild not found or bot not in guild")
        
        # Get roles excluding @everyone and bot roles
        roles = [
            {"id": str(r.id), "name": r.name, "color": str(r.color)}
            for r in guild.roles
            if r.name != "@everyone" and not r.is_bot_managed()
        ]
        
        return {"roles": sorted(roles, key=lambda r: r['name'])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# SESSIONS ROUTER
# ============================================================================

sessions_router = APIRouter()

@sessions_router.post("/create", response_model=SessionResponse)
async def create_session(
    request: CreateSessionRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new 99k jump session and post to Discord."""
    await require_guild_admin(request.guild_id, user)
    
    try:
        return await create_session_handler(request, int(user["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@sessions_router.get("/list", response_model=SessionListResponse)
async def list_sessions(
    guild_id: int,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user)
):
    """List sessions for a guild."""
    await require_guild_admin(guild_id, user)
    
    return await list_sessions_handler(guild_id, status, page, per_page)


@sessions_router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: int,
    user: dict = Depends(get_current_user)
):
    """Get a specific session."""
    db = get_database()
    session = await db.get_jump_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    await require_guild_admin(session['guild_id'], user)
    
    return SessionResponse(**session)


@sessions_router.post("/{session_id}/lock")
async def lock_session(
    session_id: int,
    user: dict = Depends(get_current_user)
):
    """Lock a session to prevent new signups."""
    db = get_database()
    session = await db.get_jump_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    await require_guild_admin(session['guild_id'], user)
    
    if session['status'] not in ('open',):
        raise HTTPException(status_code=400, detail="Session cannot be locked")
    
    await db.lock_session(session_id)
    await db.log_audit(
        int(user["id"]), "session_locked", "session", session_id,
        guild_id=session['guild_id'], source='dashboard'
    )
    
    # Update Discord message
    await update_session_message(session_id)
    
    return SuccessResponse(message="Session locked")


@sessions_router.post("/{session_id}/cancel")
async def cancel_session(
    session_id: int,
    reason: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """Cancel a session."""
    db = get_database()
    session = await db.get_jump_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    await require_guild_admin(session['guild_id'], user)
    
    if session['status'] in ('completed', 'cancelled'):
        raise HTTPException(status_code=400, detail="Session already ended")
    
    await db.cancel_session(session_id)
    await db.log_audit(
        int(user["id"]), "session_cancelled", "session", session_id,
        {"reason": reason}, guild_id=session['guild_id'], source='dashboard'
    )
    
    # Update Discord message
    await update_session_message(session_id)
    
    return SuccessResponse(message="Session cancelled")


@sessions_router.post("/{session_id}/complete")
async def complete_session(
    session_id: int,
    user: dict = Depends(get_current_user)
):
    """Mark a session as completed."""
    db = get_database()
    session = await db.get_jump_session(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    await require_guild_admin(session['guild_id'], user)
    
    if session['status'] not in ('open', 'locked'):
        raise HTTPException(status_code=400, detail="Session cannot be completed")
    
    await db.complete_session(session_id)
    await db.log_audit(
        int(user["id"]), "session_completed", "session", session_id,
        guild_id=session['guild_id'], source='dashboard'
    )
    
    # Update host reputation
    await db.update_host_reputation(
        session['host_discord_id'], session['host_torn_id'], completed=True
    )
    
    # Update Discord message
    await update_session_message(session_id)
    
    return SuccessResponse(message="Session completed")


# ============================================================================
# RAFFLES ROUTER
# ============================================================================

raffles_router = APIRouter()

@raffles_router.post("/create", response_model=RaffleResponse)
async def create_raffle(
    request: CreateRaffleRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new raffle and post to Discord."""
    await require_guild_admin(request.guild_id, user)
    
    try:
        return await create_raffle_handler(request, int(user["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@raffles_router.get("/list", response_model=RaffleListResponse)
async def list_raffles(
    guild_id: int,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user)
):
    """List raffles for a guild."""
    await require_guild_admin(guild_id, user)
    
    return await list_raffles_handler(guild_id, status, page, per_page)


@raffles_router.get("/{raffle_id}", response_model=RaffleResponse)
async def get_raffle(
    raffle_id: int,
    user: dict = Depends(get_current_user)
):
    """Get a specific raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)
    
    if not raffle:
        raise HTTPException(status_code=404, detail="Raffle not found")
    
    await require_guild_admin(raffle['guild_id'], user)
    
    return RaffleResponse(**raffle)


@raffles_router.post("/{raffle_id}/draw")
async def draw_raffle(
    raffle_id: int,
    user: dict = Depends(get_current_user)
):
    """Draw a winner for a raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)
    
    if not raffle:
        raise HTTPException(status_code=404, detail="Raffle not found")
    
    await require_guild_admin(raffle['guild_id'], user)
    
    if raffle['status'] != 'active':
        raise HTTPException(status_code=400, detail="Raffle is not active")
    
    # Draw winner
    winner = await db.draw_raffle_winner(raffle_id)
    
    await db.log_audit(
        int(user["id"]), "raffle_drawn", "raffle", raffle_id,
        {"winner_discord_id": winner['discord_id'] if winner else None},
        guild_id=raffle['guild_id'], source='dashboard'
    )
    
    # Update Discord message
    await update_raffle_message(raffle_id, winner)
    
    if winner:
        return SuccessResponse(
            message=f"Winner drawn: <@{winner['discord_id']}> (Ticket #{winner['ticket_number']})"
        )
    else:
        return SuccessResponse(message="Raffle completed with no entries")


@raffles_router.post("/{raffle_id}/cancel")
async def cancel_raffle(
    raffle_id: int,
    user: dict = Depends(get_current_user)
):
    """Cancel a raffle."""
    db = get_database()
    raffle = await db.get_raffle(raffle_id)
    
    if not raffle:
        raise HTTPException(status_code=404, detail="Raffle not found")
    
    await require_guild_admin(raffle['guild_id'], user)
    
    if raffle['status'] != 'active':
        raise HTTPException(status_code=400, detail="Raffle is not active")
    
    await db.cancel_raffle(raffle_id)
    await db.log_audit(
        int(user["id"]), "raffle_cancelled", "raffle", raffle_id,
        guild_id=raffle['guild_id'], source='dashboard'
    )
    
    # Update Discord message
    await update_raffle_message(raffle_id)
    
    return SuccessResponse(message="Raffle cancelled")


# ============================================================================
# INSURANCE ROUTER
# ============================================================================

insurance_router = APIRouter()

@insurance_router.post("/policy/create", response_model=PolicyResponse)
async def create_policy(
    request: CreatePolicyRequest,
    user: dict = Depends(get_current_user)
):
    """Create a new insurance policy."""
    if request.guild_id:
        await require_guild_admin(request.guild_id, user)
    
    try:
        return await create_policy_handler(request, int(user["id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@insurance_router.get("/policies/list")
async def list_policies(
    guild_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    user: dict = Depends(get_current_user)
):
    """List insurance policies."""
    db = get_database()
    
    if guild_id:
        policies = await db.get_active_policies(guild_id)
    elif provider_id:
        policies = await db.get_provider_policies(provider_id)
    else:
        # Return all active policies
        policies = await db.get_active_policies()
    
    return {"policies": policies}


@insurance_router.get("/providers/list")
async def list_providers(
    approval_status: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """List insurance providers."""
    db = get_database()
    providers = await db.get_all_providers(approval_status)
    return {"providers": providers}


@insurance_router.post("/provider/approve")
async def approve_provider(
    request: ApproveProviderRequest,
    user: dict = Depends(get_current_user)
):
    """Approve or reject an insurance provider."""
    db = get_database()
    provider = await db.get_provider_by_id(request.provider_id)
    
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    
    if request.status == "approved":
        await db.approve_provider(request.provider_id, int(user["id"]))
    elif request.status == "rejected":
        await db.reject_provider(request.provider_id, int(user["id"]))
    else:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    await db.log_audit(
        int(user["id"]), "provider_approval_updated", "provider", request.provider_id,
        {"status": request.status}, source='dashboard'
    )
    
    return SuccessResponse(message=f"Provider {request.status}")


@insurance_router.get("/claims/list")
async def list_claims(
    status: Optional[str] = "pending",
    provider_id: Optional[int] = None,
    user: dict = Depends(get_current_user)
):
    """List insurance claims."""
    db = get_database()
    
    if provider_id:
        claims = await db.get_provider_claims(provider_id, status)
    else:
        claims = await db.get_pending_claims()
    
    return {"claims": claims}


@insurance_router.post("/claims/{claim_id}/approve")
async def approve_claim(
    claim_id: int,
    user: dict = Depends(get_current_user)
):
    """Approve an insurance claim."""
    db = get_database()
    claim = await db.get_claim(claim_id)
    
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    
    if claim['status'] != 'pending':
        raise HTTPException(status_code=400, detail="Claim is not pending")
    
    await db.approve_claim(claim_id, int(user["id"]))
    await db.log_audit(
        int(user["id"]), "claim_approved", "claim", claim_id, source='dashboard'
    )
    
    return SuccessResponse(message="Claim approved")


@insurance_router.post("/claims/{claim_id}/reject")
async def reject_claim(
    claim_id: int,
    notes: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """Reject an insurance claim."""
    db = get_database()
    claim = await db.get_claim(claim_id)
    
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    
    if claim['status'] != 'pending':
        raise HTTPException(status_code=400, detail="Claim is not pending")
    
    await db.reject_claim(claim_id, int(user["id"]), notes)
    await db.log_audit(
        int(user["id"]), "claim_rejected", "claim", claim_id,
        {"notes": notes}, source='dashboard'
    )
    
    return SuccessResponse(message="Claim rejected")


# ============================================================================
# SETTINGS ROUTER
# ============================================================================

settings_router = APIRouter()

@settings_router.get("/{guild_id}", response_model=SettingsResponse)
async def get_settings(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get guild settings."""
    await require_guild_admin(guild_id, user)
    
    db = get_database()
    settings = await db.get_guild_settings(guild_id)
    
    return SettingsResponse(**settings)


@settings_router.post("/update", response_model=SettingsResponse)
async def update_settings(
    request: UpdateSettingsRequest,
    user: dict = Depends(get_current_user)
):
    """Update guild settings."""
    await require_guild_admin(request.guild_id, user)
    
    db = get_database()
    
    # Build update dict, excluding None values
    updates = {}
    for key, value in request.dict().items():
        if key != "guild_id" and value is not None:
            updates[key] = value
    
    if updates:
        await db.update_guild_settings(request.guild_id, **updates)
        await db.log_audit(
            int(user["id"]), "settings_updated", "guild", request.guild_id,
            updates, guild_id=request.guild_id, source='dashboard'
        )
    
    settings = await db.get_guild_settings(request.guild_id)
    return SettingsResponse(**settings)


# ============================================================================
# AUDIT ROUTER
# ============================================================================

audit_router = APIRouter()

@audit_router.get("/log", response_model=AuditLogResponse)
async def get_audit_log(
    guild_id: int,
    action: Optional[str] = None,
    actor_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user)
):
    """Get audit log entries."""
    await require_guild_admin(guild_id, user)
    
    db = get_database()
    
    entries = await db.get_audit_logs(
        guild_id=guild_id,
        limit=per_page,
        page=page,
        actor_discord_id=actor_id,
        action=action
    )
    
    total = await db.get_audit_log_count(
        guild_id=guild_id,
        actor_discord_id=actor_id,
        action=action
    )
    
    return AuditLogResponse(
        entries=[AuditLogEntry(**e) for e in entries],
        total=total,
        page=page,
        per_page=per_page
    )


# ============================================================================
# DASHBOARD STATS ROUTER
# ============================================================================

stats_router = APIRouter()

@stats_router.get("/{guild_id}")
async def get_dashboard_stats(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get dashboard statistics for a guild."""
    await require_guild_admin(guild_id, user)
    
    db = get_database()
    stats = await db.get_dashboard_stats(guild_id)
    
    return stats
