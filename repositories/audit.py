from __future__ import annotations

import json
from datetime import datetime
from typing import Optional, Any

from .base import RepositoryBase


class AuditRepository(RepositoryBase):
    async def log_audit(
        self,
        actor_discord_id: Optional[int],
        action: str,
        target_type: Optional[str],
        target_id: Optional[int],
        payload: Optional[dict] = None,
        guild_id: Optional[int] = None,
        source: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        actor_torn_id: Optional[int] = None,
    ) -> int:
        """Create audit log entry and return audit ID."""
        payload_json = json.dumps(payload or {}, separators=(",", ":"), ensure_ascii=False)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO audit_log (
                    guild_id, actor_discord_id, actor_torn_id, action,
                    target_type, target_id, payload, ip_address,
                    user_agent, created_at, source
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, NOW(), $10
                )
                RETURNING id
                """,
                guild_id,
                actor_discord_id,
                actor_torn_id,
                action,
                target_type,
                target_id,
                payload_json,
                ip_address,
                user_agent,
                source,
            )
            return int(row["id"])

    async def get_audit_logs(
        self,
        guild_id: Optional[int] = None,
        actor_discord_id: Optional[int] = None,
        action: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get audit logs with optional filters."""
        async with self.pool.acquire() as conn:
            conditions = []
            values = []
            param_idx = 0

            if guild_id is not None:
                param_idx += 1
                conditions.append(f"guild_id = ${param_idx}")
                values.append(guild_id)
            
            if actor_discord_id is not None:
                param_idx += 1
                conditions.append(f"actor_discord_id = ${param_idx}")
                values.append(actor_discord_id)
            
            if action is not None:
                param_idx += 1
                conditions.append(f"action = ${param_idx}")
                values.append(action)
            
            if target_type is not None:
                param_idx += 1
                conditions.append(f"target_type = ${param_idx}")
                values.append(target_type)
            
            if target_id is not None:
                param_idx += 1
                conditions.append(f"target_id = ${param_idx}")
                values.append(target_id)

            where_clause = ""
            if conditions:
                where_clause = "WHERE " + " AND ".join(conditions)

            param_idx += 1
            values.append(limit)
            limit_param = f"${param_idx}"

            param_idx += 1
            values.append(offset)
            offset_param = f"${param_idx}"

            query = f"""
                SELECT id, guild_id, actor_discord_id, actor_torn_id, action,
                       target_type, target_id, payload, ip_address,
                       user_agent, created_at, source
                FROM audit_log
                {where_clause}
                ORDER BY created_at DESC
                LIMIT {limit_param} OFFSET {offset_param}
            """

            rows = await conn.fetch(query, *values)
            return [dict(row) for row in rows]

    async def get_audit_log_by_id(self, audit_id: int) -> Optional[dict[str, Any]]:
        """Get specific audit log entry by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, guild_id, actor_discord_id, actor_torn_id, action,
                       target_type, target_id, payload, ip_address,
                       user_agent, created_at, source
                FROM audit_log
                WHERE id = $1
                """,
                audit_id
            )
            return dict(row) if row else None

    async def get_recent_actions(
        self,
        actor_discord_id: int,
        action: str,
        minutes: int = 60,
    ) -> list[dict[str, Any]]:
        """Get recent actions by an actor within time window."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, guild_id, action, target_type, target_id, 
                       payload, created_at, source
                FROM audit_log
                WHERE actor_discord_id = $1
                AND action = $2
                AND created_at > NOW() - make_interval(mins => $3)
                ORDER BY created_at DESC
                """,
                actor_discord_id,
                action,
                minutes,
            )
            return [dict(row) for row in rows]

    async def cleanup_old_audit_logs(self, days: int = 90) -> int:
        """Delete audit logs older than specified days."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM audit_log 
                WHERE created_at < NOW() - make_interval(days => $1)
                """,
                days,
            )
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0
