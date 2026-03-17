from __future__ import annotations

import logging

from repositories.engagement import EngagementRepository
from repositories.free_raffle_repo import FreeRaffleRepository
from repositories.jumps import JumpsRepository
from repositories.pools_repository import PoolsRepository
from repositories.prize_tokens import PrizeTokensRepository
from repositories.raffles import RafflesRepository
from repositories.store import StoreRepository
from repositories.user_torn_identity_cache import UserTornIdentityCacheRepository

log = logging.getLogger("happy_jumper.member_cleanup")


class MemberCleanupService:
    def __init__(self, pool):
        self.engagement_repo = EngagementRepository(pool)
        self.prize_tokens_repo = PrizeTokensRepository(pool)
        self.store_repo = StoreRepository(pool)
        self.raffles_repo = RafflesRepository(pool)
        self.free_raffle_repo = FreeRaffleRepository(pool)
        self.pools_repo = PoolsRepository(pool)
        self.jumps_repo = JumpsRepository(pool)
        self.identity_repo = UserTornIdentityCacheRepository(pool)

    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        steps = [
            ("engagement", self.engagement_repo.cleanup_departed_member),
            ("prize_tokens", self.prize_tokens_repo.cleanup_departed_member),
            ("store", self.store_repo.cleanup_departed_member),
            ("raffles", self.raffles_repo.cleanup_departed_member),
            ("free_raffle", self.free_raffle_repo.cleanup_departed_member),
            ("pools", self.pools_repo.cleanup_departed_member),
            ("jumps", self.jumps_repo.cleanup_departed_member),
        ]
        totals: dict[str, int] = {}
        for step_name, step in steps:
            try:
                result = await step(int(guild_id), int(user_id))
                for key, value in result.items():
                    totals[key] = int(totals.get(key, 0)) + int(value)
            except Exception:
                log.warning(
                    "Departed-member cleanup step failed guild_id=%s user_id=%s step=%s",
                    guild_id,
                    user_id,
                    step_name,
                    exc_info=True,
                )
        try:
            deleted = await self.identity_repo.delete_identity(int(guild_id), int(user_id))
            totals["user_torn_identity_cache"] = 1 if deleted else 0
        except Exception:
            log.warning(
                "Departed-member cleanup step failed guild_id=%s user_id=%s step=identity_cache",
                guild_id,
                user_id,
                exc_info=True,
            )
        return totals

    async def list_known_guild_user_ids(self, guild_id: int) -> set[int]:
        users: set[int] = set()
        readers = [
            self.engagement_repo.list_guild_user_ids,
            self.prize_tokens_repo.list_guild_user_ids,
            self.store_repo.list_guild_user_ids,
            self.raffles_repo.list_guild_participant_user_ids,
            self.free_raffle_repo.list_guild_participant_user_ids,
            self.pools_repo.list_guild_participant_user_ids,
            self.jumps_repo.list_guild_participant_user_ids,
            self.identity_repo.list_guild_discord_ids,
        ]
        for reader in readers:
            try:
                users.update(await reader(int(guild_id)))
            except Exception:
                log.warning("Failed listing cleanup candidates guild_id=%s reader=%s", guild_id, reader.__name__, exc_info=True)
        return {uid for uid in users if int(uid) > 0}
