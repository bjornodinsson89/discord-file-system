from __future__ import annotations

import json


def fmt_tokens(v: int) -> str:
    return f"{int(v):,}"


def trunc_json(data: dict, max_len: int = 500) -> str:
    raw = json.dumps(data or {}, ensure_ascii=False)
    return raw if len(raw) <= max_len else raw[: max_len - 3] + "..."


def ledger_line(entry: dict) -> str:
    return (
        f"`#{entry.get('id')}` <@{entry.get('discord_id')}> "
        f"**{entry.get('entry_type')}** {entry.get('amount_tokens')} → {entry.get('balance_after')}"
    )
