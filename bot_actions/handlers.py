"""
Happy Jumper Discord Bot - Business logic handlers
All database operations use Repository pattern
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from repositories.jumps import JumpsRepository
from repositories.raffles import RafflesRepository
from repositories.insurance import InsuranceRepository
from repositories.guilds import GuildsRepository
from services.jump_monitor import get_jump_monitor
from bot_actions.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    CreateRaffleRequest,
    CreateRaffleResponse,
    CreatePolicyRequest,
    CreatePolicyResponse,
)

log = logging.getLogger("happy_jumper")

# Bot instance for sending messages
_bot_instance = None

def set_bot_instance(bot):
    global _bot_instance
    _bot_instance = bot

def get_bot_instance():
    return _bot_instance


class SessionInfo:
    def __init__(self, data):
        self.id = data.get('id')
        self.status = data.get('status')
        self.xanax_count = data.get('xanax_count')
        self.max_spots = data.get('max_spots')
        self.payment_type = data.get('payment_type')
        self.payment_amount = data.get('payment_amount')


class RaffleInfo:
    def __init__(self, data):
        self.raffle_id = data.get('raffle_id')
        self.status = data.get('status')
        self.prize = data.get('prize')
        self.ticket_price = data.get('ticket_price', 0)
        self.tickets_available = data.get('tickets_available', 0)
        self.tickets_sold = data.get('tickets_sold', 0)
        self.end_trigger = data.get('end_trigger', 'time')


class PolicyInfo:
    def __init__(self, data):
        self.policy_id = data.get('policy_id')
        self.name = data.get('name')
        self.active = data.get('active')


class ListSessionsResponse:
    def __init__(self, sessions_list):
        self.sessions = [SessionInfo(s) for s in sessions_list]


class ListRafflesResponse:
    def __init__(self, raffles_list):
        self.raffles = [RaffleInfo(r) for r in raffles_list]


class LockSessionResponse:
    def __init__(self, message):
        self.message = message


class CancelSessionResponse:
    def __init__(self, message):
        self.message = message


class CompleteSessionResponse:
    def __init__(self, message):
        self.message = message


class DrawRaffleResponse:
    def __init__(self, winner_data, raffle_data):
        self.winner = winner_data
        self.raffle_id = raffle_data.get('raffle_id')
        self.message = f"Winner drawn for raffle #{self.raffle_id}!"
        if winner_data:
            self.message += f" Congratulations to <@{winner_data['discord_id']}>!"
        else:
            self.message += " No valid entries found."


class CancelRaffleResponse:
    def __init__(self, raffle_id):
        self.message = f"Raffle #{raffle_id} has been cancelled."


class ApproveProviderResponse:
    def __init__(self, provider_id, status):
        self.message = f"Provider #{provider_id} status updated to {status}."


class ApproveClaimResponse:
    def __init__(self, claim_id):
        self.message = f"Claim #{claim_id} has been approved."


class RejectClaimResponse:
    def __init__(self, claim_id):
        self.message = f"Claim #{claim_id} has been rejected."


# ============================================================================
# SESSION HANDLERS
# ============================================================================

async def create_session_handler(request: CreateSessionRequest, admin_discord_id: int) -> CreateSessionResponse:
    """Create a new Happy Jump session."""
    from utils import get_database
    db = get_database()
    jumps_repo = JumpsRepository(db.pool)
    
    session_id = await jumps_repo.create_session(
        guild_id=request.guild_id,
        host_discord_id=admin_discord_id,
        title="99k Session",
        scheduled_start_text=None,
        start_time=None,
        max_slots=request.spots,
        notes=None,
        price_item="xanax",
        price_amount=99,
        announce_channel_id=request.channel_id,
        announce_message_id=None,
    )

    await get_jump_monitor().start(session_id)
    return CreateSessionResponse(id=session_id, message_url=None)


async def list_sessions_handler(guild_id: int, status: Optional[str], page: int, per_page: int):
    """List sessions with pagination."""
    from utils import get_database
    db = get_database()
    jumps_repo = JumpsRepository(db.pool)
    
    offset = (page - 1) * per_page
    sessions = await jumps_repo.list_sessions(
        guild_id=guild_id,
        status=status,
        limit=per_page,
        offset=offset
    )
    
    return ListSessionsResponse(sessions)


async def lock_session_handler(session_id: int, admin_discord_id: int, source: str = "discord") -> LockSessionResponse:
    """Lock a session for readiness tracking."""
    from utils import get_database
    db = get_database()
    jumps_repo = JumpsRepository(db.pool)
    
    success = await jumps_repo.lock_session(session_id)
    if not success:
        raise ValueError(f"Session {session_id} not found or already closed")
    
    return LockSessionResponse(f"Session #{session_id} has been locked for jump preparation.")


async def cancel_session_handler(session_id: int, admin_discord_id: int, reason: Optional[str] = None, source: str = "discord") -> CancelSessionResponse:
    """Cancel a session."""
    from utils import get_database
    db = get_database()
    jumps_repo = JumpsRepository(db.pool)
    
    success = await jumps_repo.cancel_session(session_id)
    if not success:
        raise ValueError(f"Session {session_id} not found or already cancelled/completed")
    
    await get_jump_monitor().stop(session_id)

    msg = f"Session #{session_id} has been cancelled."
    if reason:
        msg += f" Reason: {reason}"
    
    return CancelSessionResponse(msg)


async def complete_session_handler(session_id: int, admin_discord_id: int, source: str = "discord") -> CompleteSessionResponse:
    """Complete a session."""
    from utils import get_database
    db = get_database()
    jumps_repo = JumpsRepository(db.pool)
    
    success = await jumps_repo.complete_session(session_id)
    if not success:
        raise ValueError(f"Session {session_id} not found or not in valid state to complete")

    await get_jump_monitor().stop(session_id)
    return CompleteSessionResponse(f"Session #{session_id} has been completed.")


# ============================================================================
# RAFFLE HANDLERS
# ============================================================================

async def create_raffle_handler(request: CreateRaffleRequest, admin_discord_id: int) -> CreateRaffleResponse:
    """Create a raffle with flexible end conditions."""
    from utils import get_database
    db = get_database()
    raffles_repo = RafflesRepository(db.pool)
    
    # Validate: need either duration_hours OR hours_after_sold_out based on trigger
    if request.end_trigger == "time":
        if not request.duration_hours:
            raise ValueError("duration_hours is required for time-based raffles")
        end_time = datetime.utcnow() + timedelta(hours=request.duration_hours)
        hours_after_sold_out = None
    elif request.end_trigger == "tickets_sold":
        if request.hours_after_sold_out is None:
            raise ValueError("hours_after_sold_out is required for sell-out raffles")
        end_time = None  # No fixed end time
    else:
        raise ValueError("end_trigger must be 'time' or 'tickets_sold'")
    
    # Create the raffle
    raffle_id = await raffles_repo.create_raffle(
        guild_id=request.guild_id,
        creator_discord_id=admin_discord_id,
        prize=request.prize,
        ticket_payment_type=request.ticket_payment_type,
        ticket_price=request.ticket_price,
        tickets_available=request.tickets_available,
        max_tickets_per_user=request.max_tickets_per_user,
        end_time=end_time,
        end_trigger=request.end_trigger,
        hours_after_sold_out=request.hours_after_sold_out,
    )
    
    log.info(f"Created raffle {raffle_id} with trigger={request.end_trigger}")
    return CreateRaffleResponse(raffle_id=raffle_id, message_url=None)


async def list_raffles_handler(guild_id: Optional[int], status: Optional[str], page: int, per_page: int):
    """List raffles with pagination."""
    from utils import get_database
    db = get_database()
    raffles_repo = RafflesRepository(db.pool)
    
    offset = (page - 1) * per_page
    raffles = await raffles_repo.list_raffles(
        guild_id=guild_id,
        status=status,
        limit=per_page,
        offset=offset
    )
    
    return ListRafflesResponse(raffles)


async def draw_raffle_handler(raffle_id: int, admin_discord_id: int, source: str = "discord") -> DrawRaffleResponse:
    """Draw a raffle winner manually."""
    from utils import get_database
    db = get_database()
    raffles_repo = RafflesRepository(db.pool)
    
    raffle = await raffles_repo.get_raffle(raffle_id)
    if not raffle:
        raise ValueError(f"Raffle {raffle_id} not found")
    
    if raffle['status'] != 'active':
        raise ValueError(f"Raffle {raffle_id} is not active (status: {raffle['status']})")
    
    # Draw winner
    winner = await raffles_repo.draw_raffle_winner(raffle_id)
    
    return DrawRaffleResponse(winner, raffle)


async def cancel_raffle_handler(raffle_id: int, admin_discord_id: int, source: str = "discord") -> CancelRaffleResponse:
    """Cancel an active raffle."""
    from utils import get_database
    db = get_database()
    raffles_repo = RafflesRepository(db.pool)
    
    success = await raffles_repo.cancel_raffle(raffle_id)
    if not success:
        raise ValueError(f"Could not cancel raffle {raffle_id} (may not exist or not active)")
    
    return CancelRaffleResponse(raffle_id)


# ============================================================================
# INSURANCE HANDLERS
# ============================================================================

async def create_policy_handler(request: CreatePolicyRequest, provider_discord_id: int) -> CreatePolicyResponse:
    """Create an insurance policy."""
    from utils import get_database
    db = get_database()
    insurance_repo = InsuranceRepository(db.pool)
    
    policy_id = await insurance_repo.create_policy(
        guild_id=request.guild_id,
        provider_id=provider_discord_id,  # This needs to be resolved to actual provider_id
        name=request.policy_name,
        description=request.description,
        cost_type=request.cost_type,
        cost_amount=request.cost_amount,
        coverage_type=request.coverage_type,
        payout_description=request.payout_description,
        payout_items=request.payout_items,
        duration_hours=request.duration_hours,
    )
    
    return CreatePolicyResponse(policy_id=policy_id)


async def approve_provider_handler(provider_id: int, status: str, admin_discord_id: int, source: str = "discord") -> ApproveProviderResponse:
    """Approve or reject an insurance provider."""
    from utils import get_database
    db = get_database()
    insurance_repo = InsuranceRepository(db.pool)
    
    success = await insurance_repo.resolve_provider_application(
        application_id=provider_id,
        decision="approve" if status == "approved" else "reject",
        reviewer_discord_id=admin_discord_id,
        reason=None if status == "approved" else "Rejected by admin"
    )
    
    if not success:
        raise ValueError(f"Provider {provider_id} not found or already processed")
    
    return ApproveProviderResponse(provider_id, status)


async def approve_claim_handler(claim_id: int, admin_discord_id: int, source: str = "discord") -> ApproveClaimResponse:
    """Approve an insurance claim."""
    from utils import get_database
    db = get_database()
    insurance_repo = InsuranceRepository(db.pool)
    
    # Update claim status to approved
    async with db.pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE insurance_claims 
            SET status = 'approved',
                resolved_by = $1,
                resolved_at = NOW()
            WHERE claim_id = $2
            AND status = 'pending'
            """,
            admin_discord_id,
            claim_id
        )
        if 'UPDATE 0' in result:
            raise ValueError(f"Claim {claim_id} not found or not in pending status")
    
    return ApproveClaimResponse(claim_id)


async def reject_claim_handler(claim_id: int, admin_discord_id: int, notes: Optional[str] = None, source: str = "discord") -> RejectClaimResponse:
    """Reject an insurance claim."""
    from utils import get_database
    db = get_database()
    
    async with db.pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE insurance_claims 
            SET status = 'rejected',
                resolved_by = $1,
                resolved_at = NOW(),
                notes = COALESCE($3, notes)
            WHERE claim_id = $2
            AND status = 'pending'
            """,
            admin_discord_id,
            claim_id,
            notes
        )
        if 'UPDATE 0' in result:
            raise ValueError(f"Claim {claim_id} not found or not in pending status")
    
    return RejectClaimResponse(claim_id)
