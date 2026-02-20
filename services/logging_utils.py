from __future__ import annotations

import logging


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    payload = {
        "event": event,
        "guild_id": fields.pop("guild_id", None),
        "session_id": fields.pop("session_id", None),
        "user_id": fields.pop("user_id", None),
        "channel_id": fields.pop("channel_id", None),
        "message_id": fields.pop("message_id", None),
        "action": fields.pop("action", None),
        "result": fields.pop("result", None),
        "error_type": fields.pop("error_type", None),
        **fields,
    }
    logger.log(level, event, extra={"structured": payload})
