from __future__ import annotations

import asyncpg


def wallet_lock_pair(guild_id: int, wallet_id: int) -> tuple[int, int]:
    max_int4 = 2_147_483_647
    a = int(guild_id % max_int4)
    b = int(wallet_id % max_int4)
    return a, b


async def advisory_lock_for_wallet(conn: asyncpg.Connection, *, guild_id: int, wallet_id: int) -> None:
    a, b = wallet_lock_pair(guild_id, wallet_id)
    await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", a, b)
