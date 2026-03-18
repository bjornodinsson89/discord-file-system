from __future__ import annotations

import asyncpg

from repositories.happy_jump_dollars import HappyJumpDollarRepository


class HappyJumpDollarService:
    def __init__(self, repo: HappyJumpDollarRepository):
        self.repo = repo

    async def grant_level_up_hjd(self, guild_id: int, user_id: int, level: int) -> bool:
        tx = await self.repo.apply_transaction(
            guild_id=guild_id,
            user_id=user_id,
            transaction_type="level_up_grant",
            amount=100,
            source_type="level",
            source_id=str(level),
            dedupe_key=f"levelup_hjd:{guild_id}:{user_id}:{level}",
            metadata={"level": level, "amount": 100},
        )
        return tx is not None

    async def spend_store_hjd(
        self,
        *,
        guild_id: int,
        user_id: int,
        amount: int,
        source_id: str,
        dedupe_key: str,
        metadata: dict | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        tx = await self.repo.apply_transaction(
            guild_id=guild_id,
            user_id=user_id,
            transaction_type="store_spend",
            amount=-abs(int(amount)),
            source_type="store",
            source_id=source_id,
            dedupe_key=dedupe_key,
            metadata=metadata or {},
            conn=conn,
        )
        return tx is not None

    async def refund_store_hjd(
        self,
        *,
        guild_id: int,
        user_id: int,
        amount: int,
        source_id: str,
        dedupe_key: str,
        metadata: dict | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> bool:
        tx = await self.repo.apply_transaction(
            guild_id=guild_id,
            user_id=user_id,
            transaction_type="store_refund",
            amount=abs(int(amount)),
            source_type="store",
            source_id=source_id,
            dedupe_key=dedupe_key,
            metadata=metadata or {},
            conn=conn,
        )
        return tx is not None
