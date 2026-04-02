from __future__ import annotations

from datetime import datetime
import json
from typing import Optional

from .base import RepositoryBase


class RafflesRepository(RepositoryBase):
    async def _get_column_names(self, table_name: str) -> set[str]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = $1
                """,
                table_name,
            )
            return {str(row["column_name"]) for row in rows}

    async def create_raffle(
        self,
        guild_id: int,
        creator_discord_id: int,
        prize: str,
        ticket_payment_type: str,
        ticket_price: int,
        tickets_available: int,
        max_tickets_per_user: int,
        end_time: Optional[datetime],
        end_trigger: str,
        hours_after_sold_out: Optional[float],
        admin_comments: Optional[str] = None,
        is_bundle: bool = False,
        bundle_text: Optional[str] = None,
        allow_prize_token_purchase: bool = False,
        prize_token_cost_per_ticket: Optional[int] = None,
    ) -> int:
        """Create a new raffle."""
        async with self.acquire() as conn:
            creator = await conn.fetchrow(
                "SELECT torn_user_id FROM user_api_keys WHERE discord_id = $1",
                creator_discord_id
            )
            creator_torn_id = creator["torn_user_id"] if creator else None
            
            row = await conn.fetchrow(
                """
                INSERT INTO raffles (
                    guild_id, creator_discord_id, creator_torn_id, prize, ticket_payment_type,
                    ticket_price, tickets_available, max_tickets_per_user,
                    end_time, end_trigger, hours_after_sold_out, status, is_free,
                    tickets_sold, is_bundle, bundle_text, admin_comments,
                    allow_prize_token_purchase, prize_token_cost_per_ticket, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active', $12, 0, $13, $14, $15, $16, $17, NOW())
                RETURNING raffle_id
                """,
                guild_id,
                creator_discord_id,
                creator_torn_id,
                prize,
                ticket_payment_type,
                0 if ticket_payment_type == "free" else ticket_price,
                tickets_available,
                max_tickets_per_user,
                end_time,
                end_trigger,
                hours_after_sold_out,
                ticket_payment_type == "free",
                is_bundle,
                bundle_text,
                admin_comments,
                bool(allow_prize_token_purchase),
                int(prize_token_cost_per_ticket) if prize_token_cost_per_ticket is not None else None,
            )
            return int(row["raffle_id"])

    async def get_raffles_for_admin_controls(self, guild_id: int) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    r.*,
                    COALESCE(
                        (SELECT SUM(num_tickets)
                         FROM raffle_entries
                         WHERE raffle_id = r.raffle_id AND payment_verified = TRUE), 0
                    ) AS tickets_sold
                FROM raffles r
                WHERE r.guild_id = $1
                  AND r.status IN ('active', 'cancelled', 'completed')
                ORDER BY r.created_at DESC
                LIMIT 25
                """,
                guild_id,
            )
            return [dict(row) for row in rows]

    async def reserve_entry(self, raffle_id: int, discord_id: int, torn_user_id: int, num_tickets: int, reserved_until: datetime):
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO raffle_entries (
                    raffle_id,
                    discord_id,
                    torn_user_id,
                    num_tickets,
                    reserved_until,
                    payment_verified,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5, FALSE, NOW())
                ON CONFLICT (raffle_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    num_tickets = EXCLUDED.num_tickets,
                    reserved_until = EXCLUDED.reserved_until
                RETURNING *
                """,
                raffle_id,
                discord_id,
                torn_user_id,
                num_tickets,
                reserved_until,
            )
            return dict(row) if row else None

    async def reserve_free_entry(self, raffle_id: int, discord_id: int, torn_user_id: int, num_tickets: int) -> Optional[dict]:
        """Reserve entry for free raffle - instantly confirmed, no payment needed."""
        async with self.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO raffle_entries 
                        (raffle_id, discord_id, torn_user_id, num_tickets, 
                         reserved_until, payment_verified, payment_verified_at, created_at)
                    VALUES ($1, $2, $3, $4, NULL, TRUE, NOW(), NOW())
                    ON CONFLICT (raffle_id, discord_id) DO UPDATE
                    SET torn_user_id = EXCLUDED.torn_user_id,
                        num_tickets = raffle_entries.num_tickets + EXCLUDED.num_tickets,
                        payment_verified = TRUE,
                        payment_verified_at = NOW()
                    RETURNING *
                    """,
                    raffle_id, discord_id, torn_user_id, num_tickets
                )
                
                if row:
                    # Update tickets_sold count
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET tickets_sold = (
                            SELECT COALESCE(SUM(num_tickets), 0)
                            FROM raffle_entries
                            WHERE raffle_id = $1 AND payment_verified = TRUE
                        )
                        WHERE raffle_id = $1
                        """,
                        raffle_id
                    )
                    
                    # Check if sold out and move to drawing once
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET tickets_fully_sold_at = NOW()
                        WHERE raffle_id = $1 
                        AND end_trigger = 'tickets_sold'
                        AND tickets_available > 0
                        AND tickets_fully_sold_at IS NULL
                        AND (
                            SELECT COALESCE(SUM(num_tickets), 0)
                            FROM raffle_entries
                            WHERE raffle_id = $1 AND payment_verified = TRUE
                        ) >= tickets_available
                        """,
                        raffle_id
                    )
                
                return dict(row) if row else None


    async def cancel_active_raffle(self, raffle_id: int) -> bool:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE raffles SET status = 'cancelled' WHERE raffle_id = $1 AND status = 'active' RETURNING raffle_id",
                raffle_id,
            )
            return row is not None
    async def get_raffle(self, raffle_id: int) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 
                    r.*,
                    COALESCE(
                        (SELECT SUM(num_tickets) FROM raffle_entries 
                         WHERE raffle_id = r.raffle_id AND payment_verified = TRUE), 0
                    ) as tickets_sold
                FROM raffles r
                WHERE r.raffle_id = $1
                """,
                raffle_id
            )
            return dict(row) if row else None

    async def get_active_raffles(self, guild_id: int) -> list:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    r.*,
                    COALESCE(
                        (SELECT SUM(num_tickets) FROM raffle_entries 
                         WHERE raffle_id = r.raffle_id AND payment_verified = TRUE), 0
                    ) as tickets_sold
                FROM raffles r
                WHERE r.guild_id = $1 AND r.status = 'active'
                ORDER BY r.created_at DESC
                """,
                guild_id
            )
            return [dict(row) for row in rows]


    async def get_all_active_raffle_ids(self) -> list[int]:
        """Get raffle IDs for all active raffles."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id
                FROM raffles
                WHERE status = 'active'
                """
            )
            return [int(row["raffle_id"]) for row in rows]

    async def get_active_raffles_with_panels(self) -> list:
        """Get active raffles that already have purchase panel message references."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id, purchase_panel_channel_id, purchase_panel_message_id, allow_prize_token_purchase
                FROM raffles
                WHERE status = 'active'
                  AND purchase_panel_channel_id IS NOT NULL
                  AND purchase_panel_message_id IS NOT NULL
                """
            )
            return [dict(row) for row in rows]

    async def get_stale_raffles_for_cleanup(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM raffles
                WHERE status IN ('completed', 'cancelled', 'expired', 'closed')
                  AND cleaned_at IS NULL
                """
            )
            return [dict(row) for row in rows]

    async def mark_cleaned(self, raffle_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE raffles SET cleaned_at = NOW() WHERE raffle_id = $1",
                raffle_id,
            )

    async def set_announcement_ref(self, raffle_id: int, channel_id: int, message_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET announcement_channel_id = $2,
                    announcement_message_id = $3
                WHERE raffle_id = $1
                """,
                raffle_id,
                channel_id,
                message_id,
            )

    async def set_prize_confirm_dm_ref(self, raffle_id: int, channel_id: int, message_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET prize_confirm_dm_channel_id = $2,
                    prize_confirm_dm_message_id = $3
                WHERE raffle_id = $1
                """,
                raffle_id,
                channel_id,
                message_id,
            )

    async def set_prize_sent(self, raffle_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                "UPDATE raffles SET prize_sent_at = NOW() WHERE raffle_id = $1",
                raffle_id,
            )

    async def update_prize_verification(
        self,
        raffle_id: int,
        *,
        status: str,
        checked_at: Optional[datetime] = None,
        verified_at: Optional[datetime] = None,
        verified_by_discord_id: Optional[int] = None,
        verification_log_id: Optional[str] = None,
    ) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET prize_verification_status = $2,
                    prize_verification_checked_at = COALESCE($3, NOW()),
                    prize_verified_at = $4,
                    prize_verified_by_discord_id = $5,
                    prize_verification_log_id = $6
                WHERE raffle_id = $1
                """,
                raffle_id,
                status,
                checked_at,
                verified_at,
                str(verified_by_discord_id) if verified_by_discord_id else None,
                verification_log_id,
            )

    async def get_pending_prize_verification_rows(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id, announcement_message_id
                FROM raffles
                WHERE status = 'completed'
                  AND COALESCE(is_free, FALSE) = FALSE
                  AND winner_discord_id IS NOT NULL
                  AND announcement_message_id IS NOT NULL
                  AND COALESCE(prize_verification_status, '') <> 'VERIFIED'
                """
            )
            return [dict(row) for row in rows]

    async def get_pending_prize_confirm_dm_rows(self) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id, creator_discord_id, prize_confirm_dm_message_id
                FROM raffles
                WHERE prize_confirm_dm_message_id IS NOT NULL
                  AND status = 'completed'
                  AND prize_sent_at IS NULL
                """
            )
            return [dict(row) for row in rows]

    async def get_prize_items_payload(self, raffle: dict) -> list[dict]:
        if raffle.get("prize_item_id") and raffle.get("prize_quantity"):
            return [{"item_id": int(raffle["prize_item_id"]), "qty": int(raffle["prize_quantity"])}]

        raw = raffle.get("prize_items")
        if not raw:
            return []

        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return []
        else:
            parsed = raw

        if not isinstance(parsed, list):
            return []

        items: list[dict] = []
        for row in parsed:
            if not isinstance(row, dict):
                continue
            item_id = row.get("item_id") or row.get("id")
            qty = row.get("qty") or row.get("quantity")
            if not item_id or not qty:
                continue
            items.append({"item_id": int(item_id), "qty": int(qty)})
        return items

    async def get_raffle_entries(self, raffle_id: int) -> list:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM raffle_entries WHERE raffle_id = $1",
                raffle_id
            )
            return [dict(row) for row in rows]



    async def get_entry_by_raffle_and_discord(self, raffle_id: int, discord_id: int) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM raffle_entries WHERE raffle_id = $1 AND discord_id = $2",
                raffle_id,
                discord_id,
            )
            return dict(row) if row else None

    async def get_reserved_tickets_count(self, raffle_id: int) -> int:
        async with self.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(SUM(num_tickets), 0)
                FROM raffle_entries
                WHERE raffle_id = $1
                  AND payment_verified = FALSE
                  AND reserved_until > NOW()
                """,
                raffle_id,
            )
            return int(value or 0)

    async def draw_winner(self, raffle_id: int) -> Optional[dict]:
        return await self.draw_raffle_winner(raffle_id)
    async def get_pending_verifications(self) -> list:
        """Get entries that need auto-verification."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM raffle_entries 
                WHERE payment_verified = FALSE 
                AND reserved_until > NOW()
                AND created_at <= NOW() - INTERVAL '4 minutes 30 seconds'
                """
            )
            return [dict(row) for row in rows]

    async def get_entry_with_raffle(self, entry_id: int) -> Optional[dict]:
        """Fetch reservation and raffle details used for payment verification."""
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    e.entry_id,
                    e.raffle_id,
                    e.discord_id,
                    e.torn_user_id,
                    e.num_tickets,
                    e.reserved_until,
                    e.payment_verified,
                    e.created_at,
                    r.guild_id,
                    r.creator_discord_id,
                    r.creator_torn_id,
                    COALESCE(r.creator_torn_id, u.torn_user_id) AS effective_creator_torn_id,
                    r.ticket_payment_type,
                    r.ticket_price,
                    r.tickets_available,
                    r.end_trigger,
                    r.tickets_fully_sold_at,
                    r.status
                FROM raffle_entries e
                JOIN raffles r ON r.raffle_id = e.raffle_id
                LEFT JOIN user_api_keys u ON u.discord_id = r.creator_discord_id
                WHERE e.entry_id = $1
                """,
                entry_id,
            )
            return dict(row) if row else None

    async def get_entry_for_verification(self, entry_id: int) -> Optional[dict]:
        """Backward-compatible alias for entry+raffle verification lookup."""
        return await self.get_entry_with_raffle(entry_id)

    async def mark_entry_verified(self, entry_id: int) -> bool:
        """Mark a reservation as paid after external verification succeeds."""
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE raffle_entries
                SET payment_verified = TRUE,
                    payment_verified_at = NOW()
                WHERE entry_id = $1
                  AND payment_verified = FALSE
                """,
                entry_id,
            )
            return result == "UPDATE 1"

    async def recompute_tickets_sold_and_maybe_set_sold_out(self, raffle_id: int) -> Optional[int]:
        """Recompute verified ticket totals and set sold-out timestamp when threshold is reached."""
        async with self.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE raffles
                    SET tickets_sold = (
                        SELECT COALESCE(SUM(num_tickets), 0)
                        FROM raffle_entries
                        WHERE raffle_id = $1
                          AND payment_verified = TRUE
                    )
                    WHERE raffle_id = $1
                    """,
                    raffle_id,
                )

                row = await conn.fetchrow(
                    """
                    SELECT raffle_id, tickets_available, tickets_sold, end_trigger, tickets_fully_sold_at
                    FROM raffles
                    WHERE raffle_id = $1
                    """,
                    raffle_id,
                )
                if not row:
                    return None

                if (
                    row["end_trigger"] == "tickets_sold"
                    and int(row["tickets_available"] or 0) > 0
                    and row["tickets_fully_sold_at"] is None
                    and int(row["tickets_sold"] or 0) >= int(row["tickets_available"] or 0)
                ):
                    result = await conn.execute(
                        """
                        UPDATE raffles
                        SET tickets_fully_sold_at = NOW()
                        WHERE raffle_id = $1
                          AND tickets_fully_sold_at IS NULL
                        """,
                        raffle_id,
                    )
                    if result == "UPDATE 1":
                        return raffle_id

                return None

    async def recompute_tickets_sold_and_set_sold_out(self, raffle_id: int) -> Optional[int]:
        """Backward-compatible alias for sold-out recompute logic."""
        return await self.recompute_tickets_sold_and_maybe_set_sold_out(raffle_id)

    async def set_purchase_panel_ref(self, raffle_id: int, channel_id: int, message_id: int) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET purchase_panel_channel_id = $2,
                    purchase_panel_message_id = $3,
                    purchase_channel_id = $2,
                    purchase_message_id = $3
                WHERE raffle_id = $1
                """,
                raffle_id,
                channel_id,
                message_id,
            )

    async def get_purchase_panel_ref(self, raffle_id: int) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT raffle_id, purchase_panel_channel_id, purchase_panel_message_id, allow_prize_token_purchase
                FROM raffles
                WHERE raffle_id = $1
                """,
                raffle_id,
            )
            return dict(row) if row else None

    async def set_prize_image_url(self, raffle_id: int, url: str) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET prize_image_url = $2
                WHERE raffle_id = $1
                """,
                raffle_id,
                url,
            )

    async def update_raffle_image(self, raffle_id: int, image_url: str) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET prize_image_url = $2
                WHERE raffle_id = $1
                """,
                raffle_id,
                image_url,
            )

    async def update_raffle_comment(self, raffle_id: int, comment: Optional[str]) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET admin_comments = $2
                WHERE raffle_id = $1
                """,
                raffle_id,
                comment,
            )

    async def cancel_expired_reservation(self, entry_id: int):
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM raffle_entries WHERE entry_id = $1 AND payment_verified = FALSE",
                entry_id
            )

    async def cleanup_expired_raffle_entries(self) -> int:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM raffle_entries 
                WHERE payment_verified = FALSE 
                AND reserved_until < NOW()
                """
            )
            return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def _draw_raffle_winner_atomic(
        self,
        raffle_id: int,
        *,
        require_ready: bool,
        cancel_if_empty: bool,
    ) -> dict:
        """Atomically draw raffle winner while guarding against concurrent draws."""
        async with self.acquire() as conn:
            async with conn.transaction():
                raffle = await conn.fetchrow(
                    """
                    SELECT raffle_id, status, end_trigger, end_time, tickets_available, winner_discord_id, winner_torn_id,
                           COALESCE((
                               SELECT SUM(num_tickets)
                               FROM raffle_entries
                               WHERE raffle_id = $1 AND payment_verified = TRUE
                           ), 0) AS verified_total
                    FROM raffles
                    WHERE raffle_id = $1
                    FOR UPDATE
                    """,
                    raffle_id,
                )
                if not raffle:
                    return {"state": "not_found", "winner": None}

                status = str(raffle["status"] or "").lower()
                if status in {"completed", "cancelled"}:
                    winner = None
                    if raffle.get("winner_discord_id") is not None:
                        winner = {
                            "discord_id": int(raffle["winner_discord_id"]),
                            "torn_user_id": int(raffle["winner_torn_id"]) if raffle.get("winner_torn_id") is not None else None,
                        }
                    return {"state": "already_drawn", "winner": winner}

                if status != "active":
                    return {"state": "not_drawable", "winner": None}

                if require_ready:
                    verified_total = int(raffle["verified_total"] or 0)
                    tickets_available = int(raffle["tickets_available"] or 0)
                    end_trigger = str(raffle.get("end_trigger") or "").lower()
                    if end_trigger == "tickets_sold" and tickets_available > 0 and verified_total < tickets_available:
                        return {"state": "not_ready", "winner": None}
                    end_time = raffle.get("end_time")
                    if end_trigger == "time" and end_time is not None:
                        now = datetime.now(end_time.tzinfo) if getattr(end_time, "tzinfo", None) else datetime.now()
                        if end_time > now:
                            return {"state": "not_ready", "winner": None}

                entries = await conn.fetch(
                    """
                    SELECT discord_id, torn_user_id, num_tickets
                    FROM raffle_entries
                    WHERE raffle_id = $1 AND payment_verified = TRUE
                    """,
                    raffle_id,
                )

                if not entries:
                    if cancel_if_empty:
                        await conn.execute(
                            "UPDATE raffles SET status = 'cancelled' WHERE raffle_id = $1",
                            raffle_id,
                        )
                        return {"state": "cancelled", "winner": None}
                    return {"state": "no_entries", "winner": None}

                import random

                pool = []
                for entry in entries:
                    pool.extend([entry] * int(entry["num_tickets"] or 0))

                winner = random.choice(pool)

                await conn.execute(
                    """
                    UPDATE raffles
                    SET status = 'completed',
                        winner_discord_id = $1,
                        winner_torn_id = $2,
                        drawn_at = NOW()
                    WHERE raffle_id = $3
                    """,
                    winner["discord_id"],
                    winner["torn_user_id"],
                    raffle_id,
                )

                return {
                    "state": "drawn",
                    "winner": {
                        "discord_id": winner["discord_id"],
                        "torn_user_id": winner["torn_user_id"],
                        "total_entries": len(entries),
                        "total_tickets": sum(int(e["num_tickets"] or 0) for e in entries),
                    },
                }

    async def draw_raffle_winner_atomic(self, raffle_id: int) -> dict:
        return await self._draw_raffle_winner_atomic(raffle_id, require_ready=True, cancel_if_empty=True)

    async def force_draw_raffle_winner_atomic(self, raffle_id: int) -> dict:
        return await self._draw_raffle_winner_atomic(raffle_id, require_ready=False, cancel_if_empty=False)

    async def draw_raffle_winner(self, raffle_id: int) -> Optional[dict]:
        result = await self.draw_raffle_winner_atomic(raffle_id)
        if result.get("state") == "drawn":
            return result.get("winner")
        return None

    async def force_draw_raffle_winner(self, raffle_id: int) -> Optional[dict]:
        result = await self.force_draw_raffle_winner_atomic(raffle_id)
        if result.get("state") == "drawn":
            return result.get("winner")
        return None

    async def get_raffles_to_draw(self) -> list:
        """Get raffles that are ready to be drawn."""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM raffles
                WHERE status = 'active'
                  AND (
                        (
                            end_trigger = 'tickets_sold'
                            AND tickets_fully_sold_at IS NOT NULL
                            AND NOW() >= tickets_fully_sold_at + INTERVAL '30 seconds'
                        )
                        OR (
                            end_trigger = 'time'
                            AND end_time IS NOT NULL
                            AND NOW() >= end_time
                        )
                  )
                """
            )
            return [dict(row) for row in rows]

    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM raffle_entries e
                USING raffles r
                WHERE e.raffle_id = r.raffle_id
                  AND r.guild_id = $1
                  AND e.discord_id = $2
                """,
                guild_id,
                user_id,
            )
            return {"raffle_entries": int(str(result).split()[-1])}

    async def list_guild_participant_user_ids(self, guild_id: int) -> set[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT e.discord_id
                FROM raffle_entries e
                JOIN raffles r ON r.raffle_id = e.raffle_id
                WHERE r.guild_id = $1
                """,
                guild_id,
            )
            return {int(r["discord_id"]) for r in rows if int(r["discord_id"] or 0) > 0}

    async def get_recovery_preview(self, raffle_id: int) -> dict | None:
        raffle = await self.get_raffle(raffle_id)
        if not raffle:
            return None
        if str(raffle.get("status") or "").lower() != "cancelled":
            return {"blocked_reason": "Raffle is not cancelled.", "raffle": raffle}
        if raffle.get("superseded_by_raffle_id"):
            return {"blocked_reason": "This cancelled raffle already has a replacement.", "raffle": raffle}

        entry_columns = await self._get_column_names("raffle_entries")
        filters = ["raffle_id = $1", "payment_verified = TRUE"]
        if "recreated_from_entry_id" in entry_columns:
            filters.append("recreated_from_entry_id IS NULL")
        if "refunded_at" in entry_columns:
            filters.append("refunded_at IS NULL")
        if "is_refunded" in entry_columns:
            filters.append("COALESCE(is_refunded, FALSE) = FALSE")
        if "status" in entry_columns:
            filters.append("COALESCE(status, 'verified') NOT IN ('refunded', 'cancelled', 'invalidated', 'failed', 'pending')")
        if "is_cancelled" in entry_columns:
            filters.append("COALESCE(is_cancelled, FALSE) = FALSE")
        if "is_invalidated" in entry_columns:
            filters.append("COALESCE(is_invalidated, FALSE) = FALSE")

        async with self.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT COUNT(*) AS restored_entry_count,
                       COALESCE(SUM(num_tickets), 0) AS restored_ticket_count
                FROM raffle_entries
                WHERE {' AND '.join(filters)}
                """,
                raffle_id,
            )
            return {
                "raffle": raffle,
                "restored_entry_count": int(row["restored_entry_count"] or 0),
                "restored_ticket_count": int(row["restored_ticket_count"] or 0),
            }

    async def recreate_cancelled_raffle(self, raffle_id: int) -> dict:
        raffle_columns = await self._get_column_names("raffles")
        entry_columns = await self._get_column_names("raffle_entries")
        preview = await self.get_recovery_preview(raffle_id)
        if not preview:
            raise ValueError("Raffle not found.")
        if preview.get("blocked_reason"):
            raise ValueError(str(preview["blocked_reason"]))

        source = dict(preview["raffle"])
        restore_filters = ["raffle_id = $1", "payment_verified = TRUE"]
        if "recreated_from_entry_id" in entry_columns:
            restore_filters.append("recreated_from_entry_id IS NULL")
        if "refunded_at" in entry_columns:
            restore_filters.append("refunded_at IS NULL")
        if "is_refunded" in entry_columns:
            restore_filters.append("COALESCE(is_refunded, FALSE) = FALSE")
        if "status" in entry_columns:
            restore_filters.append("COALESCE(status, 'verified') NOT IN ('refunded', 'cancelled', 'invalidated', 'failed', 'pending')")
        if "is_cancelled" in entry_columns:
            restore_filters.append("COALESCE(is_cancelled, FALSE) = FALSE")
        if "is_invalidated" in entry_columns:
            restore_filters.append("COALESCE(is_invalidated, FALSE) = FALSE")

        async with self.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow("SELECT * FROM raffles WHERE raffle_id = $1 FOR UPDATE", raffle_id)
                if current is None:
                    raise ValueError("Raffle not found.")
                if str(current.get("status") or "").lower() != "cancelled":
                    raise ValueError("Raffle is not cancelled.")
                if current.get("superseded_by_raffle_id") is not None:
                    raise ValueError("This cancelled raffle already has a replacement.")

                creator_torn_id = current.get("creator_torn_id")
                if creator_torn_id is None and current.get("creator_discord_id") is not None:
                    creator = await conn.fetchrow(
                        "SELECT torn_user_id FROM user_api_keys WHERE discord_id = $1",
                        int(current["creator_discord_id"]),
                    )
                    creator_torn_id = creator["torn_user_id"] if creator else None

                insert_map = {
                    "guild_id": int(current["guild_id"]),
                    "creator_discord_id": int(current["creator_discord_id"]),
                    "creator_torn_id": creator_torn_id,
                    "prize": current.get("prize"),
                    "ticket_payment_type": current.get("ticket_payment_type"),
                    "ticket_price": int(current.get("ticket_price") or 0),
                    "tickets_available": int(current.get("tickets_available") or 0),
                    "max_tickets_per_user": int(current.get("max_tickets_per_user") or 0),
                    "end_time": current.get("end_time"),
                    "end_trigger": current.get("end_trigger"),
                    "hours_after_sold_out": current.get("hours_after_sold_out"),
                    "status": "active",
                    "is_free": bool(current.get("is_free")),
                    "tickets_sold": 0,
                    "is_bundle": bool(current.get("is_bundle")),
                    "bundle_text": current.get("bundle_text"),
                    "admin_comments": current.get("admin_comments"),
                    "allow_prize_token_purchase": bool(current.get("allow_prize_token_purchase")),
                    "prize_token_cost_per_ticket": current.get("prize_token_cost_per_ticket"),
                    "created_at": datetime.now(),
                    "recreated_from_raffle_id": raffle_id,
                }
                for optional_column in ("prize_item_id", "prize_quantity", "prize_image_url"):
                    if optional_column in raffle_columns:
                        insert_map[optional_column] = current.get(optional_column)
                columns = [column for column in insert_map if column in raffle_columns]
                values = [insert_map[column] for column in columns]
                placeholders = ", ".join(f"${idx}" for idx in range(1, len(values) + 1))
                new_row = await conn.fetchrow(
                    f"INSERT INTO raffles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING raffle_id",
                    *values,
                )
                new_raffle_id = int(new_row["raffle_id"])

                source_entries = await conn.fetch(
                    f"SELECT * FROM raffle_entries WHERE {' AND '.join(restore_filters)} ORDER BY entry_id ASC",
                    raffle_id,
                )
                restored_entries = 0
                restored_tickets = 0
                for entry in source_entries:
                    columns = [
                        "raffle_id", "discord_id", "torn_user_id", "num_tickets",
                        "reserved_until", "payment_verified", "payment_verified_at", "created_at",
                    ]
                    values = [
                        new_raffle_id,
                        entry.get("discord_id"),
                        entry.get("torn_user_id"),
                        int(entry.get("num_tickets") or 0),
                        None,
                        True,
                        entry.get("payment_verified_at") or datetime.now(),
                        datetime.now(),
                    ]
                    if "recreated_from_entry_id" in entry_columns:
                        columns.append("recreated_from_entry_id")
                        values.append(int(entry["entry_id"]))
                    if "status" in entry_columns:
                        columns.append("status")
                        values.append("verified")
                    placeholders = ", ".join(f"${idx}" for idx in range(1, len(values) + 1))
                    await conn.execute(
                        f"INSERT INTO raffle_entries ({', '.join(columns)}) VALUES ({placeholders})",
                        *values,
                    )
                    restored_entries += 1
                    restored_tickets += int(entry.get("num_tickets") or 0)

                update_old = ["tickets_sold = $2", "superseded_by_raffle_id = $3"]
                if "updated_at" in raffle_columns:
                    update_old.append("updated_at = NOW()")
                await conn.execute(
                    f"UPDATE raffles SET {', '.join(update_old)} WHERE raffle_id = $1",
                    raffle_id,
                    int(current.get("tickets_sold") or 0),
                    new_raffle_id,
                )
                update_new = ["tickets_sold = $2"]
                args = [new_raffle_id, restored_tickets]
                if "purchase_panel_channel_id" in raffle_columns:
                    update_new.append("purchase_panel_channel_id = NULL")
                if "purchase_panel_message_id" in raffle_columns:
                    update_new.append("purchase_panel_message_id = NULL")
                if "purchase_channel_id" in raffle_columns:
                    update_new.append("purchase_channel_id = NULL")
                if "purchase_message_id" in raffle_columns:
                    update_new.append("purchase_message_id = NULL")
                await conn.execute(
                    f"UPDATE raffles SET {', '.join(update_new)} WHERE raffle_id = $1",
                    *args,
                )
                return {
                    "old_raffle_id": raffle_id,
                    "new_raffle_id": new_raffle_id,
                    "restored_entry_count": restored_entries,
                    "restored_ticket_count": restored_tickets,
                    "raffle": source,
                }
