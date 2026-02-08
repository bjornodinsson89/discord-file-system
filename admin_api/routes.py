"""
Admin API Routes
FastAPI route definitions for all admin endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, List

from web.permissions import get_current_user, require_guild_admin, get_user_guilds
from web.discord_api import get_guild_channels as discord_get_guild_channels, get_guild_roles as discord_get_guild_roles
from admin_api.schemas import *
from admin_api.handlers import *
from utils import get_database

# ============================================================================
# GUILD INFO ROUTER (for dropdowns)
# ============================================================================

guild_router = APIRouter()


@guild_router.get("/admin")
async def list_admin_guilds(
    user: dict = Depends(get_current_user)
):
    """List guilds the user can administer."""
    guilds = await get_user_guilds(user)
    return {"guilds": guilds}


@guild_router.get("/{guild_id}/channels")
async def get_guild_channels(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get list of text channels in guild via Discord REST."""
    await require_guild_admin(guild_id, user)
    try:
        channels = await discord_get_guild_channels(guild_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"channels": channels}


@guild_router.get("/{guild_id}/roles")
async def get_guild_roles(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get list of roles in guild via Discord REST."""
    await require_guild_admin(guild_id, user)
    try:
        roles = await discord_get_guild_roles(guild_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"roles": roles}


@guild_router.get("/{guild_id}", response_model=GuildInfoResponse)
async def get_guild_info(
    guild_id: int,
    user: dict = Depends(get_current_user)
):
    """Get basic guild info."""
    await require_guild_admin(guild_id, user)

    guild = next((g for g in user.get("guilds", []) if int(g.get("id", 0)) == guild_id), None)
    if not guild:
        raise HTTPException(status_code=404, detail="Guild not found in OAuth guild list")

    return GuildInfoResponse(
        id=int(guild["id"]),
        name=guild.get("name", "Unknown Server"),
        icon=(f"https://cdn.discordapp.com/icons/{guild['id']}/{guild['icon']}.png" if guild.get("icon") else None),
        member_count=None,
    )


# ============================================================================
# MEMBERS ROUTER
# ============================================================================

members_router = APIRouter()

@members_router.get("/list", response_model=MemberListResponse)
async def list_members(
    guild_id: int,
    search: Optional[str] = None,
    filter: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: dict = Depends(get_current_user)
):
    """List registered users in a guild with dashboard metadata."""
    await require_guild_admin(guild_id, user)

    db = get_database()
    rows = await db.get_registered_users_for_guild(guild_id)

    entries: List[MemberSummary] = []
    for row in rows:
        discord_id = int(row["discord_id"])
        username = row.get("username") or f"User {discord_id}"
        entry = MemberSummary(
            discord_id=discord_id,
            username=username,
            display_name=row.get("display_name") or username,
            avatar=row.get("avatar"),
            torn_user_id=row.get("torn_user_id"),
            has_api_key=bool(row.get("has_api_key")),
            is_host=bool(row.get("is_host")),
            is_insurer=bool(row.get("is_insurer")),
            sessions_joined=row.get("sessions_joined", 0),
            sessions_hosted=row.get("sessions_hosted", 0),
            created_at=row.get("created_at"),
        )
        entries.append(entry)

    if search:
        lowered = search.lower()
        def matches(entry: MemberSummary) -> bool:
            if lowered in str(entry.discord_id):
                return True
            if entry.torn_user_id and lowered in str(entry.torn_user_id):
                return True
            if entry.username and lowered in entry.username.lower():
                return True
            if entry.display_name and lowered in entry.display_name.lower():
                return True
            return False
        entries = [entry for entry in entries if matches(entry)]

    if filter == "with_key":
        entries = [entry for entry in entries if entry.has_api_key]
    elif filter == "hosts":
        entries = [entry for entry in entries if entry.is_host]
    elif filter == "insurers":
        entries = [entry for entry in entries if entry.is_insurer]

    entries.sort(key=lambda e: (e.username or "").lower())
    total = len(entries)
    start = (page - 1) * per_page
    end = start + per_page

    return MemberListResponse(
        members=entries[start:end],
        total=total,
        page=page,
        per_page=per_page
    )


# ============================================================================
# BLACKLIST ROUTER
# ============================================================================

blacklist_router = APIRouter()

@blacklist_router.get("/list", response_model=BlacklistListResponse)
async def list_blacklist(
    guild_id: int,
    search: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """List blacklisted users for a guild."""
    await require_guild_admin(guild_id, user)

    db = get_database()
    entries = await db.get_blacklist(guild_id)
    discord_ids = [entry["discord_id"] for entry in entries]
    api_keys = await db.get_user_api_keys_by_ids(discord_ids)
    api_map = {row["discord_id"]: row for row in api_keys}

    results: List[BlacklistEntry] = []
    for entry in entries:
        api_key = api_map.get(entry["discord_id"])
        results.append(BlacklistEntry(
            guild_id=entry["guild_id"],
            discord_id=entry["discord_id"],
            username=None,
            torn_user_id=api_key["torn_user_id"] if api_key else None,
            reason=entry.get("reason"),
            banned_by=entry.get("banned_by"),
            expires_at=entry.get("expires_at"),
            created_at=entry.get("created_at")
        ))

    if search:
        lowered = search.lower()
        def matches(entry: BlacklistEntry) -> bool:
            if lowered in str(entry.discord_id):
                return True
            if entry.torn_user_id and lowered in str(entry.torn_user_id):
                return True
            if entry.reason and lowered in entry.reason.lower():
                return True
            if entry.username and lowered in entry.username.lower():
                return True
            return False
        results = [entry for entry in results if matches(entry)]

    return BlacklistListResponse(entries=results)


@blacklist_router.post("/add", response_model=SuccessResponse)
async def add_blacklist_entry(
    request: AddBlacklistRequest,
    user: dict = Depends(get_current_user)
):
    """Add a user to the blacklist."""
    await require_guild_admin(request.guild_id, user)
    try:
        return await add_blacklist_handler(
            request.guild_id,
            request.discord_id,
            request.reason,
            int(user["id"]),
            request.expires_at,
            source="dashboard"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@blacklist_router.post("/{guild_id}/{discord_id}/remove", response_model=SuccessResponse)
async def remove_blacklist_entry(
    guild_id: int,
    discord_id: int,
    user: dict = Depends(get_current_user)
):
    """Remove a user from the blacklist."""
    await require_guild_admin(guild_id, user)
    try:
        return await remove_blacklist_handler(
            guild_id,
            discord_id,
            int(user["id"]),
            source="dashboard"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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

    try:
        return await lock_session_handler(session_id, int(user["id"]), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

    try:
        return await cancel_session_handler(
            session_id, int(user["id"]), reason=reason, source="dashboard"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

    try:
        return await complete_session_handler(session_id, int(user["id"]), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

    try:
        return await draw_raffle_handler(raffle_id, int(user["id"]), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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

    try:
        return await cancel_raffle_handler(raffle_id, int(user["id"]), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    try:
        return await approve_provider_handler(
            request.provider_id,
            request.status,
            int(user["id"]),
            source="dashboard"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    try:
        return await approve_claim_handler(claim_id, int(user["id"]), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@insurance_router.post("/claims/{claim_id}/reject")
async def reject_claim(
    claim_id: int,
    notes: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    """Reject an insurance claim."""
    try:
        return await reject_claim_handler(
            claim_id,
            int(user["id"]),
            notes=notes,
            source="dashboard"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    try:
        return await update_settings_handler(request, int(user["id"]), source="dashboard")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# AUDIT ROUTER
# ============================================================================

audit_router = APIRouter()

@audit_router.get("/log", response_model=AuditLogResponse)
async def get_audit_log(
    guild_id: int,
    action: Optional[str] = None,
    actor_id: Optional[int] = None,
    search: Optional[str] = None,
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
        action=action,
        search=search
    )
    
    total = await db.get_audit_log_count(
        guild_id=guild_id,
        actor_discord_id=actor_id,
        action=action,
        search=search
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
