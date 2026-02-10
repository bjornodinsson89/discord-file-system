from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(slots=True)
class JumpSession:
    id: int
    guild_id: int
    host_discord_id: int
    status: str


@dataclass(slots=True)
class JumpSignup:
    id: int
    session_id: int
    discord_id: int
    torn_user_id: int
    status: str
    reserved_until: Optional[datetime]


@dataclass(slots=True)
class WaitlistEntry:
    session_id: int
    discord_id: int
    torn_user_id: int
    position: int


@dataclass(slots=True)
class Raffle:
    raffle_id: int
    guild_id: int
    status: str


@dataclass(slots=True)
class RaffleEntry:
    raffle_id: int
    discord_id: int
    torn_user_id: int
    num_tickets: int
    payment_verified: bool
    reserved_until: Optional[datetime]


@dataclass(slots=True)
class InsuranceProvider:
    provider_id: int
    discord_id: int
    approval_status: str
    active: bool


@dataclass(slots=True)
class InsurancePolicy:
    policy_id: int
    provider_id: int
    coverage_type: str
    active: bool


@dataclass(slots=True)
class InsuranceCoverage:
    coverage_id: int
    user_discord_id: int
    policy_id: int
    status: str


@dataclass(slots=True)
class InsuranceClaim:
    claim_id: int
    coverage_id: int
    claimant_discord_id: int
    status: str


@dataclass(slots=True)
class GuildSettings:
    guild_id: int
    announce_channel_id: Optional[int]
    raffle_channel_id: Optional[int]
    insurance_channel_id: Optional[int]


@dataclass(slots=True)
class UserApiKey:
    discord_id: int
    torn_user_id: Optional[int]
    guild_id: Optional[int]
    api_key: str
