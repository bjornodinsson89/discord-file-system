from dataclasses import dataclass
from typing import Optional, List, Dict, Any


@dataclass
class CreateSessionRequest:
    guild_id: int
    channel_id: int
    payment_type: str
    payment_amount: int
    spots: int
    xanax_count: int


@dataclass
class CreateSessionResponse:
    id: int
    message_url: Optional[str] = None


@dataclass
class CreateRaffleRequest:
    guild_id: int
    prize: str
    ticket_payment_type: str
    ticket_price: int
    tickets_available: int
    max_tickets_per_user: int
    duration_hours: Optional[int] = None  # For time-based
    end_trigger: str = "time"  # "time" or "tickets_sold"
    hours_after_sold_out: Optional[int] = None  # For sell-out based


@dataclass
class CreateRaffleResponse:
    raffle_id: int
    message_url: Optional[str] = None


@dataclass
class CreatePolicyRequest:
    guild_id: int
    provider_discord_id: int
    policy_name: str
    description: str
    cost_type: str
    cost_amount: int
    coverage_type: str
    payout_description: str
    payout_items: List[Dict[str, Any]]
    duration_hours: int


@dataclass
class CreatePolicyResponse:
    policy_id: int


@dataclass
class UpdateSettingsRequest:
    host99k_role_id: Optional[int] = None
    insurer_role_id: Optional[int] = None
    admin_role_ids: Optional[List[int]] = None
    jump_99k_channel_id: Optional[int] = None
    insurance_channel_id: Optional[int] = None
    raffle_channel_id: Optional[int] = None
    raffle_announcement_channel_id: Optional[int] = None
    raffle_purchase_channel_id: Optional[int] = None
    raffle_announce_enabled: Optional[bool] = None
    reservation_timeout_minutes: Optional[int] = None
