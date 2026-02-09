"""
Bot action schemas used by Discord commands.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime
import config

# ============================================================================
# SESSION SCHEMAS
# ============================================================================

class CreateSessionRequest(BaseModel):
    """Request to create a 99k jump session."""
    guild_id: int
    channel_id: int
    payment_type: Literal["xanax", "erotic_dvd"]
    payment_amount: int = Field(ge=1, description="Amount per spot")
    spots: int = Field(ge=config.MIN_JUMP_SPOTS, le=config.MAX_JUMP_SPOTS)
    xanax_stack: Literal["1_xanax", "2_xanax", "3_xanax", "4_xanax", "full_stack"]
    start_delay_hours: int = Field(ge=0, le=config.MAX_START_DELAY_HOURS)


class SessionResponse(BaseModel):
    """Session data response."""
    id: int
    guild_id: int
    host_discord_id: int
    host_torn_id: int
    max_spots: int
    xanax_stack: str
    payment_type: str
    payment_amount: int
    status: str
    announcement_message_id: Optional[int] = None
    message_url: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class SessionListResponse(BaseModel):
    """List of sessions with pagination."""
    sessions: List[SessionResponse]
    total: int
    page: int
    per_page: int


# ============================================================================
# RAFFLE SCHEMAS
# ============================================================================

class CreateRaffleRequest(BaseModel):
    """Request to create a raffle."""
    guild_id: int
    prize: str = Field(min_length=1, max_length=500, description="Freeform prize description")
    ticket_payment_type: Literal["xanax", "erotic_dvd"]
    ticket_price: int = Field(ge=config.MIN_TICKET_PRICE, le=config.MAX_TICKET_PRICE)
    tickets_available: int = Field(ge=config.MIN_RAFFLE_TICKETS, le=config.MAX_RAFFLE_TICKETS)
    max_tickets_per_user: int = Field(ge=0, description="0 = unlimited")
    duration_hours: int = Field(ge=config.MIN_RAFFLE_DURATION_HOURS, le=config.MAX_RAFFLE_DURATION_HOURS)


class RaffleResponse(BaseModel):
    """Raffle data response."""
    raffle_id: int
    guild_id: int
    creator_discord_id: int
    prize: str
    ticket_payment_type: str
    ticket_price: int
    tickets_available: int
    max_tickets_per_user: int
    status: str
    winner_discord_id: Optional[int]
    end_time: datetime
    announcement_message_id: Optional[int] = None
    message_url: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class RaffleListResponse(BaseModel):
    """List of raffles with pagination."""
    raffles: List[RaffleResponse]
    total: int
    page: int
    per_page: int


# ============================================================================
# INSURANCE SCHEMAS
# ============================================================================

class CreatePolicyRequest(BaseModel):
    """Request to create an insurance policy (provider)."""
    guild_id: int
    provider_discord_id: int  # Will be current user for provider flow
    policy_name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, description="Coverage instructions")
    cost_type: Literal["xanax", "erotic_dvd"]
    cost_amount: int = Field(ge=1, description="Premium cost")
    coverage_type: Literal["xanax_stack", "ecstasy_after_stack", "all_drugs"]
    payout_description: str = Field(min_length=1, description="Freeform payout terms")
    duration_hours: int = Field(ge=config.MIN_INSURANCE_DURATION_HOURS, le=config.MAX_INSURANCE_DURATION_HOURS)


class PolicyResponse(BaseModel):
    """Insurance policy data response."""
    policy_id: int
    provider_id: int
    name: str
    description: str
    cost_type: str
    cost_amount: int
    coverage_type: str
    payout_description: str
    duration_hours: int
    active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class ProviderResponse(BaseModel):
    """Insurance provider data response."""
    provider_id: int
    discord_id: int
    torn_user_id: int
    company_name: Optional[str] = None
    approval_status: str
    verified: bool
    active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class ApproveProviderRequest(BaseModel):
    """Request to approve/reject a provider."""
    provider_id: int
    status: Literal["approved", "rejected", "disabled"]


# ============================================================================
# GUILDS & MEMBERS
# ============================================================================

class GuildInfoResponse(BaseModel):
    """Guild info response."""
    id: int
    name: str
    icon: Optional[str] = None
    member_count: Optional[int] = None


class MemberSummary(BaseModel):
    """Member summary for bot administration."""
    discord_id: int
    username: Optional[str] = None
    display_name: Optional[str] = None
    avatar: Optional[str] = None
    torn_user_id: Optional[int] = None
    has_api_key: bool
    is_host: bool
    is_insurer: bool
    sessions_joined: int = 0
    sessions_hosted: int = 0
    created_at: Optional[datetime] = None


class MemberListResponse(BaseModel):
    """List of members with pagination."""
    members: List[MemberSummary]
    total: int
    page: int
    per_page: int


# ============================================================================
# BLACKLIST
# ============================================================================

class AddBlacklistRequest(BaseModel):
    """Request to add a user to the blacklist."""
    guild_id: int
    discord_id: int
    reason: Optional[str] = None
    expires_at: Optional[datetime] = None


class BlacklistEntry(BaseModel):
    """Blacklist entry."""
    guild_id: int
    discord_id: int
    username: Optional[str] = None
    torn_user_id: Optional[int] = None
    reason: Optional[str] = None
    banned_by: Optional[int] = None
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class BlacklistListResponse(BaseModel):
    """List of blacklist entries."""
    entries: List[BlacklistEntry]


# ============================================================================
# SETTINGS SCHEMAS
# ============================================================================

class UpdateSettingsRequest(BaseModel):
    """Request to update guild settings."""
    guild_id: int
    host99k_role_id: Optional[int] = None
    insurer_role_id: Optional[int] = None
    admin_role_id: Optional[int] = None
    jump_99k_channel_id: Optional[int] = None
    insurance_channel_id: Optional[int] = None
    raffle_channel_id: Optional[int] = None
    welcome_channel_id: Optional[int] = None
    welcome_message_template: Optional[str] = Field(None, min_length=1, max_length=1500)
    welcome_enabled: Optional[bool] = None
    reservation_timeout_minutes: Optional[int] = Field(None, ge=1, le=60)
    auto_complete_enabled: Optional[bool] = None


class SettingsResponse(BaseModel):
    """Guild settings response."""
    guild_id: int
    host99k_role_id: Optional[int] = None
    insurer_role_id: Optional[int] = None
    admin_role_id: Optional[int] = None
    jump_99k_channel_id: Optional[int] = None
    insurance_channel_id: Optional[int] = None
    raffle_channel_id: Optional[int] = None
    welcome_channel_id: Optional[int] = None
    welcome_message_template: Optional[str] = None
    welcome_enabled: bool
    reservation_timeout_minutes: int
    auto_complete_enabled: bool
    updated_at: datetime
    
    class Config:
        from_attributes = True


# ============================================================================
# AUDIT SCHEMAS
# ============================================================================

class AuditLogEntry(BaseModel):
    """Audit log entry."""
    id: int
    actor_discord_id: Optional[int] = None
    action: str
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    payload: dict
    source: Optional[str] = None
    created_at: datetime
    
    class Config:
        from_attributes = True


class AuditLogResponse(BaseModel):
    """List of audit log entries."""
    entries: List[AuditLogEntry]
    total: int
    page: int
    per_page: int


# ============================================================================
# COMMON SCHEMAS
# ============================================================================

class SuccessResponse(BaseModel):
    """Generic success response."""
    success: bool = True
    message: str
    data: Optional[dict] = None


class ErrorResponse(BaseModel):
    """Generic error response."""
    success: bool = False
    error: str
    detail: Optional[str] = None
