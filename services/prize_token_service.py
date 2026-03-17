from __future__ import annotations

from repositories.prize_tokens import PrizeTokensRepository


class PrizeTokenService:
    def __init__(self, repo: PrizeTokensRepository):
        self.repo = repo

    async def grant_level_up_token(self, guild_id: int, user_id: int, level: int) -> bool:
        tx = await self.repo.apply_transaction(
            guild_id=guild_id,
            user_id=user_id,
            transaction_type="level_up_grant",
            amount=1,
            source_type="level",
            source_id=str(level),
            dedupe_key=f"levelup_token:{guild_id}:{user_id}:{level}",
            metadata={"level": level},
        )
        return tx is not None

    async def admin_grant(self, guild_id: int, user_id: int, amount: int, admin_user_id: int, reason: str) -> bool:
        tx = await self.repo.apply_transaction(
            guild_id=guild_id,
            user_id=user_id,
            transaction_type="admin_grant" if amount > 0 else "admin_remove",
            amount=amount,
            source_type="admin",
            source_id=str(admin_user_id),
            dedupe_key=f"admin_token_adjust:{guild_id}:{user_id}:{admin_user_id}:{amount}:{reason}",
            metadata={"reason": reason},
        )
        return tx is not None


    async def spend_auto_entry_token(self, guild_id: int, user_id: int, giveaway_id: int) -> bool:
        tx = await self.repo.apply_transaction(
            guild_id=guild_id,
            user_id=user_id,
            transaction_type="auto_entry_spend",
            amount=-1,
            source_type="giveaway",
            source_id=str(giveaway_id),
            dedupe_key=f"giveaway_auto_entry:{giveaway_id}:{user_id}",
            metadata={"giveaway_id": giveaway_id},
        )
        return tx is not None
