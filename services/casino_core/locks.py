from __future__ import annotations

import asyncpg


def wallet_lock_key(guild_id: int, wallet_id: int) -> int:
    return (int(guild_id) << 32) ^ (int(wallet_id) & 0xFFFFFFFF)


async def advisory_lock_for_wallet(conn: asyncpg.Connection, *, guild_id: int, wallet_id: int) -> None:
    await conn.execute("SELECT pg_advisory_xact_lock($1)", wallet_lock_key(guild_id, wallet_id))
