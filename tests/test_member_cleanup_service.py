from __future__ import annotations

import asyncio

from services.member_cleanup import MemberCleanupService


class StubRepo:
    def __init__(self):
        self.rows = {(1, 10): 2, (2, 10): 3}

    async def cleanup_departed_member(self, guild_id: int, user_id: int):
        key = (guild_id, user_id)
        deleted = self.rows.get(key, 0)
        self.rows[key] = 0
        return {"rows": deleted}

    async def list_guild_user_ids(self, guild_id: int):
        return {uid for (gid, uid), count in self.rows.items() if gid == guild_id and count > 0}

    async def list_guild_participant_user_ids(self, guild_id: int):
        return await self.list_guild_user_ids(guild_id)


class StubIdentityRepo:
    def __init__(self):
        self.deleted = set()

    async def delete_identity(self, guild_id: int, user_id: int):
        key = (guild_id, user_id)
        if key in self.deleted:
            return False
        self.deleted.add(key)
        return True

    async def list_guild_discord_ids(self, guild_id: int):
        return set()


def test_cleanup_is_idempotent_and_guild_scoped():
    async def _run():
        service = MemberCleanupService(pool=None)
        stub = StubRepo()
        identity = StubIdentityRepo()
        service.engagement_repo = stub
        service.prize_tokens_repo = stub
        service.store_repo = stub
        service.raffles_repo = stub
        service.free_raffle_repo = stub
        service.pools_repo = stub
        service.jumps_repo = stub
        service.identity_repo = identity

        first = await service.cleanup_departed_member(1, 10)
        second = await service.cleanup_departed_member(1, 10)
        other_guild = await service.cleanup_departed_member(2, 10)

        assert sum(first.values()) > 0
        assert sum(second.values()) == 0
        assert sum(other_guild.values()) > 0

    asyncio.run(_run())


def test_reconciliation_candidate_listing_unions_sources():
    async def _run():
        service = MemberCleanupService(pool=None)
        stub = StubRepo()
        identity = StubIdentityRepo()
        service.engagement_repo = stub
        service.prize_tokens_repo = stub
        service.store_repo = stub
        service.raffles_repo = stub
        service.free_raffle_repo = stub
        service.pools_repo = stub
        service.jumps_repo = stub
        service.identity_repo = identity

        users = await service.list_known_guild_user_ids(1)
        assert users == {10}

    asyncio.run(_run())
