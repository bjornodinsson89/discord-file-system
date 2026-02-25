from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from .base import RepositoryBase


log = logging.getLogger("happy_jumper.repositories.api_audit")
_KEY_PARAM_RE = re.compile(r"key=[^&\s]+")


@dataclass
class ApiAuditRow:
    id: int
    discord_id: int
    torn_id: Optional[int]
    context: str
    endpoint: str
    selections: Optional[str]
    query_meta: dict[str, Any]
    status: Literal["ok", "error"]
    http_status: Optional[int]
    duration_ms: Optional[int]
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: datetime


class ApiAuditRepository(RepositoryBase):
    @staticmethod
    def _sanitize_error_message(error_message: Optional[str]) -> Optional[str]:
        if error_message is None:
            return None
        redacted = _KEY_PARAM_RE.sub("key=[REDACTED]", str(error_message))
        return redacted[:300]

    @staticmethod
    def _coerce_query_meta(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            try:
                parsed = json.loads(stripped)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    async def insert_event(
        self,
        *,
        discord_id: int,
        torn_id: Optional[int],
        context: str,
        endpoint: str,
        selections: Optional[str],
        query_meta: dict[str, Any],
        status: Literal["ok", "error"],
        http_status: Optional[int],
        duration_ms: Optional[int],
        error_code: Optional[str],
        error_message: Optional[str],
    ) -> None:
        try:
            async with self.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO api_audit_log (
                        discord_id,
                        torn_id,
                        context,
                        endpoint,
                        selections,
                        query_meta,
                        status,
                        http_status,
                        duration_ms,
                        error_code,
                        error_message
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)
                    """,
                    int(discord_id),
                    int(torn_id) if torn_id is not None else None,
                    str(context),
                    str(endpoint),
                    str(selections) if selections is not None else None,
                    json.dumps(query_meta or {}, separators=(",", ":"), ensure_ascii=False),
                    status,
                    int(http_status) if http_status is not None else None,
                    int(duration_ms) if duration_ms is not None else None,
                    str(error_code) if error_code is not None else None,
                    self._sanitize_error_message(error_message),
                )
        except Exception:
            log.debug(
                "Failed to insert API audit event discord_id=%s context=%s",
                discord_id,
                context,
                exc_info=True,
            )

    async def list_recent(self, discord_id: int, limit: int) -> list[ApiAuditRow]:
        safe_limit = max(1, min(int(limit), 100))
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    discord_id,
                    torn_id,
                    context,
                    endpoint,
                    selections,
                    query_meta,
                    status,
                    http_status,
                    duration_ms,
                    error_code,
                    error_message,
                    created_at
                FROM api_audit_log
                WHERE discord_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                int(discord_id),
                safe_limit,
            )

        return [
            ApiAuditRow(
                id=int(row["id"]),
                discord_id=int(row["discord_id"]),
                torn_id=int(row["torn_id"]) if row["torn_id"] is not None else None,
                context=str(row["context"]),
                endpoint=str(row["endpoint"]),
                selections=str(row["selections"]) if row["selections"] is not None else None,
                query_meta=self._coerce_query_meta(row["query_meta"]),
                status=str(row["status"]),
                http_status=int(row["http_status"]) if row["http_status"] is not None else None,
                duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
                error_code=str(row["error_code"]) if row["error_code"] is not None else None,
                error_message=str(row["error_message"]) if row["error_message"] is not None else None,
                created_at=row["created_at"],
            )
            for row in rows
        ]
