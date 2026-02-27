from __future__ import annotations

import json
import hashlib
import secrets
from datetime import datetime
from typing import Any

import asyncpg

from repositories.base import RepositoryBase
from services.casino_core.locks import advisory_lock_for_wallet


class CasinoCoreRepository(RepositoryBase):
    async def get_or_create_slots_server_seed(
        self,
        conn: asyncpg.Connection,
        guild_id: int,
        *,
        for_update: bool = False,
    ) -> dict:
        lock_clause = " FOR UPDATE" if for_update else ""
        row = await conn.fetchrow(
            f"""
            SELECT *
            FROM casino_slots_server_seeds
            WHERE guild_id = $1
            {lock_clause}
            """,
            int(guild_id),
        )
        if row:
            return dict(row)

        server_seed = secrets.token_hex(32)
        server_seed_hash = hashlib.sha256(server_seed.encode("utf-8")).hexdigest()
        await conn.execute(
            """
            INSERT INTO casino_slots_server_seeds (guild_id, server_seed, server_seed_hash)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO NOTHING
            """,
            int(guild_id),
            server_seed,
            server_seed_hash,
        )
        row = await conn.fetchrow(
            """
            SELECT *
            FROM casino_slots_server_seeds
            WHERE guild_id = $1
            """ + (" FOR UPDATE" if for_update else ""),
            int(guild_id),
        )
        return dict(row)

    async def rotate_slots_server_seed(self, conn: asyncpg.Connection, guild_id: int) -> dict:
        await self.get_or_create_slots_server_seed(conn, int(guild_id), for_update=True)
        new_server_seed = secrets.token_hex(32)
        new_server_seed_hash = hashlib.sha256(new_server_seed.encode("utf-8")).hexdigest()

        row = await conn.fetchrow(
            """
            UPDATE casino_slots_server_seeds
            SET previous_server_seed = server_seed,
                previous_server_seed_hash = server_seed_hash,
                previous_rotated_at = rotated_at,
                server_seed = $2,
                server_seed_hash = $3,
                rotated_at = NOW()
            WHERE guild_id = $1
            RETURNING *
            """,
            int(guild_id),
            new_server_seed,
            new_server_seed_hash,
        )
        return dict(row)

    async def get_or_create_slots_player_state(
        self,
        conn: asyncpg.Connection,
        guild_id: int,
        discord_id: int,
        *,
        for_update: bool = False,
    ) -> dict:
        lock_clause = " FOR UPDATE" if for_update else ""
        row = await conn.fetchrow(
            f"""
            SELECT *
            FROM casino_slots_player_state
            WHERE guild_id = $1 AND discord_id = $2
            {lock_clause}
            """,
            int(guild_id),
            int(discord_id),
        )
        if row:
            return dict(row)

        client_seed = f"{int(discord_id)}-{secrets.token_hex(12)}"
        await conn.execute(
            """
            INSERT INTO casino_slots_player_state (guild_id, discord_id, client_seed, nonce)
            VALUES ($1, $2, $3, 0)
            ON CONFLICT (guild_id, discord_id) DO NOTHING
            """,
            int(guild_id),
            int(discord_id),
            client_seed,
        )
        row = await conn.fetchrow(
            """
            SELECT *
            FROM casino_slots_player_state
            WHERE guild_id = $1 AND discord_id = $2
            """ + (" FOR UPDATE" if for_update else ""),
            int(guild_id),
            int(discord_id),
        )
        return dict(row)

    async def get_or_create_retention_state(
        self,
        guild_id: int,
        discord_id: int,
        game: str,
        *,
        for_update: bool = False,
        conn: asyncpg.Connection | None = None,
    ) -> dict:
        async def _run(db: asyncpg.Connection) -> dict:
            lock_clause = " FOR UPDATE" if for_update else ""
            row = await db.fetchrow(
                f"""
                SELECT *
                FROM casino_player_retention
                WHERE guild_id = $1 AND discord_id = $2 AND game = $3
                {lock_clause}
                """,
                int(guild_id),
                int(discord_id),
                str(game),
            )
            if row:
                return dict(row)

            row = await db.fetchrow(
                """
                INSERT INTO casino_player_retention (guild_id, discord_id, game, plays, loss_streak)
                VALUES ($1, $2, $3, 0, 0)
                ON CONFLICT (guild_id, discord_id, game)
                DO UPDATE SET updated_at = NOW()
                RETURNING *
                """,
                int(guild_id),
                int(discord_id),
                str(game),
            )
            if for_update:
                row = await db.fetchrow(
                    """
                    SELECT *
                    FROM casino_player_retention
                    WHERE guild_id = $1 AND discord_id = $2 AND game = $3
                    FOR UPDATE
                    """,
                    int(guild_id),
                    int(discord_id),
                    str(game),
                )
            return dict(row)

        if conn is not None:
            return await _run(conn)
        async with self.acquire() as db:
            return await _run(db)

    async def update_retention_state(
        self,
        guild_id: int,
        discord_id: int,
        game: str,
        plays: int,
        loss_streak: int,
        *,
        conn: asyncpg.Connection | None = None,
    ) -> None:
        async def _run(db: asyncpg.Connection) -> None:
            await db.execute(
                """
                UPDATE casino_player_retention
                SET plays = $4,
                    loss_streak = $5,
                    updated_at = NOW()
                WHERE guild_id = $1 AND discord_id = $2 AND game = $3
                """,
                int(guild_id),
                int(discord_id),
                str(game),
                int(plays),
                int(loss_streak),
            )

        if conn is not None:
            await _run(conn)
            return
        async with self.acquire() as db:
            await _run(db)

    async def create_round(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        wallet_id: int,
        game_key: str,
        bet_tokens: int,
        result_json: dict | None = None,
    ) -> int:
        round_id = await conn.fetchval(
            """
            INSERT INTO casino_game_rounds (guild_id, wallet_id, game_key, bet_tokens, payout_tokens, result)
            VALUES ($1, $2, $3, $4, 0, $5::jsonb)
            RETURNING id
            """,
            int(guild_id),
            int(wallet_id),
            str(game_key),
            int(bet_tokens),
            json.dumps(result_json or {}),
        )
        return int(round_id)

    async def update_round(self, conn: asyncpg.Connection, *, round_id: int, payout_tokens: int, result_json: dict) -> None:
        await conn.execute(
            """
            UPDATE casino_game_rounds
            SET payout_tokens = $2,
                result = $3::jsonb
            WHERE id = $1
            """,
            int(round_id),
            int(payout_tokens),
            json.dumps(result_json or {}),
        )

    async def get_or_create_wallet(
        self, guild_id: int, discord_id: int, torn_user_id: int, torn_name: str | None
    ) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO casino_wallets (guild_id, discord_id, torn_user_id, torn_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, discord_id)
                DO UPDATE SET
                    torn_user_id = CASE WHEN EXCLUDED.torn_user_id > 0 THEN EXCLUDED.torn_user_id ELSE casino_wallets.torn_user_id END,
                    torn_name = COALESCE(NULLIF(EXCLUDED.torn_name, ''), casino_wallets.torn_name),
                    updated_at = NOW()
                RETURNING *
                """,
                int(guild_id),
                int(discord_id),
                int(torn_user_id or 0),
                torn_name,
            )
            return dict(row)

    async def get_wallet(self, guild_id: int, discord_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM casino_wallets WHERE guild_id = $1 AND discord_id = $2",
                int(guild_id),
                int(discord_id),
            )
            return dict(row) if row else None

    async def get_wallet_by_id(self, wallet_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM casino_wallets WHERE id = $1", int(wallet_id))
            return dict(row) if row else None

    async def get_wallet_by_id_for_guild(self, guild_id: int, wallet_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM casino_wallets WHERE id = $1 AND guild_id = $2",
                int(wallet_id),
                int(guild_id),
            )
            return dict(row) if row else None

    async def apply_ledger_entry_atomic(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        wallet_id: int,
        entry_type: str,
        amount_tokens: int,
        idempotency_key: str,
        ref_type: str | None,
        ref_id: int | None,
        metadata: dict,
    ) -> dict:
        await advisory_lock_for_wallet(conn, guild_id=guild_id, wallet_id=wallet_id)
        current = await conn.fetchrow(
            "SELECT * FROM casino_wallets WHERE id = $1 AND guild_id = $2 FOR UPDATE",
            int(wallet_id),
            int(guild_id),
        )
        if not current:
            raise ValueError("Wallet not found")

        next_balance = int(current["balance_tokens"] or 0) + int(amount_tokens)
        if next_balance < 0:
            raise ValueError("Insufficient wallet balance")

        try:
            updated_wallet = await conn.fetchrow(
                """
                UPDATE casino_wallets
                SET balance_tokens = $1,
                    updated_at = NOW()
                WHERE id = $2
                  AND guild_id = $3
                RETURNING *
                """,
                next_balance,
                int(wallet_id),
                int(guild_id),
            )
            if not updated_wallet:
                raise ValueError("Wallet update failed (guild mismatch)")
            await conn.execute(
                """
                INSERT INTO casino_ledger (
                    guild_id, wallet_id, entry_type, amount_tokens, balance_after,
                    idempotency_key, ref_type, ref_id, metadata
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                """,
                int(guild_id),
                int(wallet_id),
                str(entry_type),
                int(amount_tokens),
                next_balance,
                str(idempotency_key),
                ref_type,
                ref_id,
                json.dumps(metadata or {}),
            )
            return dict(updated_wallet)
        except asyncpg.UniqueViolationError:
            unchanged = await conn.fetchrow(
                "SELECT * FROM casino_wallets WHERE id = $1 AND guild_id = $2",
                int(wallet_id),
                int(guild_id),
            )
            return dict(unchanged)

    async def insert_deposit_if_new(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        wallet_id: int,
        torn_log_id: str,
        torn_log_ts: int,
        qty_xanax: int,
        raw_log: dict,
    ) -> int | None:
        row = await conn.fetchrow(
            """
            INSERT INTO casino_deposits (guild_id, wallet_id, torn_log_id, torn_log_ts, qty_xanax, raw_log)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (guild_id, torn_log_id) DO NOTHING
            RETURNING id
            """,
            int(guild_id),
            int(wallet_id),
            str(torn_log_id),
            int(torn_log_ts),
            int(qty_xanax),
            json.dumps(raw_log or {}),
        )
        return int(row["id"]) if row else None

    async def create_cashout_request(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        wallet_id: int,
        qty_tokens: int,
        note: str | None,
    ) -> dict:
        row = await conn.fetchrow(
            """
            INSERT INTO casino_cashouts (guild_id, wallet_id, qty_tokens, status, note)
            VALUES ($1, $2, $3, 'requested', $4)
            RETURNING *
            """,
            int(guild_id),
            int(wallet_id),
            int(qty_tokens),
            note,
        )
        return dict(row)

    async def fetch_cashout(self, conn: asyncpg.Connection, *, guild_id: int, cashout_id: int) -> dict | None:
        row = await conn.fetchrow(
            "SELECT * FROM casino_cashouts WHERE guild_id = $1 AND id = $2",
            int(guild_id),
            int(cashout_id),
        )
        return dict(row) if row else None

    async def mark_cashout_denied(
        self, conn: asyncpg.Connection, *, guild_id: int, cashout_id: int, denied_by: int, reason: str
    ) -> dict:
        row = await conn.fetchrow(
            """
            UPDATE casino_cashouts
            SET status = 'denied',
                note = COALESCE(note, '') || CASE WHEN COALESCE(note, '') = '' THEN '' ELSE E'\n' END || $3,
                verified_at = NOW(),
                verified_by_discord_id = $4
            WHERE guild_id = $1 AND id = $2
            RETURNING *
            """,
            int(guild_id),
            int(cashout_id),
            f"Denied: {reason}",
            int(denied_by),
        )
        return dict(row)

    async def mark_cashout_verified_sent(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        cashout_id: int,
        verified_by: int,
        payout_torn_log_id: str,
        payout_raw_log: dict,
        payouts_channel_message_id: int | None,
    ) -> dict:
        row = await conn.fetchrow(
            """
            UPDATE casino_cashouts
            SET status = 'verified_sent',
                verified_at = NOW(),
                verified_by_discord_id = $3,
                payout_torn_log_id = $4,
                payout_raw_log = $5::jsonb,
                payouts_channel_message_id = $6
            WHERE guild_id = $1 AND id = $2
            RETURNING *
            """,
            int(guild_id),
            int(cashout_id),
            int(verified_by),
            str(payout_torn_log_id),
            json.dumps(payout_raw_log or {}),
            int(payouts_channel_message_id) if payouts_channel_message_id else None,
        )
        return dict(row)

    async def append_house_ledger(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        entry_type: str,
        amount_tokens: int,
        ref_type: str | None,
        ref_id: int | None,
        metadata: dict,
    ) -> dict:
        last_total = await conn.fetchval(
            "SELECT total_after FROM casino_house_ledger WHERE guild_id = $1 ORDER BY id DESC LIMIT 1",
            int(guild_id),
        )
        total_after = int(last_total or 0) + int(amount_tokens)
        row = await conn.fetchrow(
            """
            INSERT INTO casino_house_ledger (guild_id, entry_type, amount_tokens, total_after, ref_type, ref_id, metadata)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            RETURNING *
            """,
            int(guild_id),
            str(entry_type),
            int(amount_tokens),
            int(total_after),
            ref_type,
            ref_id,
            json.dumps(metadata or {}),
        )
        return dict(row)

    async def get_house_total(self, guild_id: int) -> int:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                "SELECT total_after FROM casino_house_ledger WHERE guild_id = $1 ORDER BY id DESC LIMIT 1",
                int(guild_id),
            )
            return int(value or 0)

    async def get_or_create_pool(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        pool_key: str,
        seed_tokens: int = 0,
        seed_millis: int = 0,
    ) -> dict:
        row = await conn.fetchrow(
            """
            INSERT INTO casino_pools (guild_id, pool_key, tokens, millis)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, pool_key) DO UPDATE
            SET updated_at = NOW()
            RETURNING *
            """,
            int(guild_id),
            pool_key,
            int(seed_tokens),
            int(seed_millis),
        )
        return dict(row)

    async def add_to_pool(self, conn: asyncpg.Connection, *, guild_id: int, pool_key: str, add_tokens: int, add_millis: int) -> dict:
        base_tokens = int(add_tokens) + (int(add_millis) // 1000)
        base_millis = int(add_millis) % 1000
        row = await conn.fetchrow(
            """
            INSERT INTO casino_pools (guild_id, pool_key, tokens, millis)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, pool_key)
            DO UPDATE SET
                tokens = casino_pools.tokens + EXCLUDED.tokens + ((casino_pools.millis + EXCLUDED.millis) / 1000),
                millis = MOD(casino_pools.millis + EXCLUDED.millis, 1000),
                updated_at = NOW()
            RETURNING *
            """,
            int(guild_id),
            pool_key,
            int(base_tokens),
            int(base_millis),
        )
        return dict(row)

    async def claim_pool(
        self,
        conn: asyncpg.Connection,
        *,
        guild_id: int,
        pool_key: str,
        reset_seed_tokens: int,
        reset_seed_millis: int,
    ) -> tuple[int, int, int, int]:
        await self.get_or_create_pool(
            conn,
            guild_id=int(guild_id),
            pool_key=pool_key,
            seed_tokens=reset_seed_tokens,
            seed_millis=reset_seed_millis,
        )
        current = await conn.fetchrow(
            "SELECT tokens, millis FROM casino_pools WHERE guild_id = $1 AND pool_key = $2 FOR UPDATE",
            int(guild_id),
            pool_key,
        )
        claim_tokens = int(current["tokens"])
        claim_millis = int(current["millis"])
        await conn.execute(
            """
            UPDATE casino_pools
            SET tokens = $3, millis = $4, updated_at = NOW()
            WHERE guild_id = $1 AND pool_key = $2
            """,
            int(guild_id),
            pool_key,
            int(reset_seed_tokens),
            int(reset_seed_millis),
        )
        return claim_tokens, claim_millis, int(reset_seed_tokens), int(reset_seed_millis)

    async def get_cooldown(self, conn: asyncpg.Connection, *, guild_id: int, discord_id: int, game_key: str) -> datetime | None:
        return await conn.fetchval(
            "SELECT available_at FROM casino_cooldowns WHERE guild_id=$1 AND discord_id=$2 AND game_key=$3",
            int(guild_id),
            int(discord_id),
            game_key,
        )

    async def set_cooldown(self, conn: asyncpg.Connection, *, guild_id: int, discord_id: int, game_key: str, available_at: datetime) -> None:
        await conn.execute(
            """
            INSERT INTO casino_cooldowns (guild_id, discord_id, game_key, available_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id, discord_id, game_key)
            DO UPDATE SET available_at = EXCLUDED.available_at
            """,
            int(guild_id),
            int(discord_id),
            game_key,
            available_at,
        )

    async def fetch_ledger_page(
        self,
        guild_id: int,
        *,
        limit: int,
        offset: int,
        wallet_id: int | None = None,
        entry_type: str | None = None,
    ) -> list[dict]:
        clauses = ["l.guild_id = $1"]
        args: list[Any] = [int(guild_id)]
        n = 2
        if wallet_id is not None:
            clauses.append(f"l.wallet_id = ${n}")
            args.append(int(wallet_id))
            n += 1
        if entry_type:
            clauses.append(f"l.entry_type = ${n}")
            args.append(entry_type)
            n += 1
        args.extend([int(limit), int(offset)])
        rows = []
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT l.*, w.discord_id, w.torn_user_id, w.torn_name
                FROM casino_ledger l
                JOIN casino_wallets w ON w.id = l.wallet_id AND w.guild_id = l.guild_id
                WHERE {' AND '.join(clauses)}
                ORDER BY l.created_at DESC
                LIMIT ${n} OFFSET ${n+1}
                """,
                *args,
            )
        return [dict(r) for r in rows]

    async def fetch_house_ledger_page(self, guild_id: int, *, limit: int, offset: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM casino_house_ledger
                WHERE guild_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                int(guild_id),
                int(limit),
                int(offset),
            )
            return [dict(r) for r in rows]

    async def compute_admin_totals(self, guild_id: int) -> dict:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  COALESCE((SELECT SUM(amount_tokens) FROM casino_ledger WHERE guild_id = $1 AND entry_type = 'deposit_credit'), 0) AS total_deposits_tokens,
                  COALESCE((SELECT SUM(-amount_tokens) FROM casino_house_ledger WHERE guild_id = $1 AND entry_type = 'payout_out'), 0) AS total_payouts_verified_tokens,
                  COALESCE((SELECT SUM(qty_tokens) FROM casino_cashouts WHERE guild_id = $1 AND status = 'requested'), 0) AS escrow_outstanding_tokens,
                  COALESCE((SELECT SUM(balance_tokens) FROM casino_wallets WHERE guild_id = $1), 0) AS tokens_in_circulation,
                  COALESCE((SELECT total_after FROM casino_house_ledger WHERE guild_id = $1 ORDER BY id DESC LIMIT 1), 0) AS house_net_tokens
                """,
                int(guild_id),
            )
        return dict(row or {})
