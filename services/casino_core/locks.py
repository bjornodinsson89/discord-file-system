from __future__ import annotations

import asyncpg


def wallet_lock_pair(guild_id: int, wallet_id: int) -> tuple[int, int]:
    cap = 2_147_483_647
    a = int(guild_id) % cap
    b = int(wallet_id) % cap
    return a, b


async def advisory_lock_for_wallet(conn: asyncpg.Connection, *, guild_id: int, wallet_id: int) -> None:
    a, b = wallet_lock_pair(guild_id, wallet_id)
    await conn.execute("SELECT pg_advisory_xact_lock($1, $2)", a, b)
