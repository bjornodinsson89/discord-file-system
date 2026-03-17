from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import discord

log = logging.getLogger("happy_jumper.panel_edit_safety")


def _embed_state(embed: discord.Embed | None) -> dict[str, Any] | None:
    if embed is None:
        return None
    data = embed.to_dict()
    return {
        "title": data.get("title"),
        "description": data.get("description"),
        "fields": data.get("fields") or [],
        "footer": (data.get("footer") or {}).get("text"),
        "thumbnail": (data.get("thumbnail") or {}).get("url"),
        "image": (data.get("image") or {}).get("url"),
        "url": data.get("url"),
    }


def _component_state(component: Any) -> dict[str, Any]:
    children = getattr(component, "children", None)
    if children is not None:
        return {"type": "row", "children": [_component_state(c) for c in children]}

    options_state: list[dict[str, Any]] = []
    for option in list(getattr(component, "options", []) or []):
        options_state.append(
            {
                "label": getattr(option, "label", None),
                "value": getattr(option, "value", None),
                "description": getattr(option, "description", None),
                "default": getattr(option, "default", None),
                "emoji": str(getattr(option, "emoji", None) or "") or None,
            }
        )

    return {
        "type": getattr(component, "type", None).name
        if getattr(component, "type", None)
        else type(component).__name__,
        "custom_id": getattr(component, "custom_id", None),
        "label": getattr(component, "label", None),
        "disabled": bool(getattr(component, "disabled", False)),
        "placeholder": getattr(component, "placeholder", None),
        "options": options_state,
    }


def _view_state(
    view: discord.ui.View | None = None, *, components: Any = None
) -> list[dict[str, Any]]:
    if view is not None:
        return [_component_state(child) for child in view.children]
    if components is not None:
        return [_component_state(row) for row in components]
    return []


def _fingerprint(
    content: str | None, embed: discord.Embed | None, view: discord.ui.View | None
) -> str:
    payload = {
        "content": content,
        "embed": _embed_state(embed),
        "view": _view_state(view),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _message_fingerprint(message: discord.Message) -> str:
    embeds = list(getattr(message, "embeds", []) or [])
    return json.dumps(
        {
            "content": getattr(message, "content", None),
            "embed": _embed_state(embeds[0] if embeds else None),
            "view": _view_state(None, components=getattr(message, "components", None)),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


@dataclass
class _PendingEdit:
    message: discord.Message
    content: str | None
    embed: discord.Embed | None
    view: discord.ui.View | None
    force: bool = False


@dataclass
class _EditState:
    last_edit_at: datetime | None = None
    last_fingerprint: str | None = None
    pending: _PendingEdit | None = None
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PanelEditSafety:
    def __init__(self) -> None:
        self._states: dict[int, _EditState] = {}

    async def request_edit(
        self,
        message: discord.Message,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        min_interval_seconds: float,
        force: bool = False,
        not_found_cb=None,
    ) -> bool:
        message_id = int(getattr(message, "id", id(message)))
        state = self._states.setdefault(message_id, _EditState())
        next_fp = _fingerprint(content, embed, view)

        async with state.lock:
            if state.last_fingerprint is None:
                state.last_fingerprint = _message_fingerprint(message)

            if state.last_fingerprint == next_fp:
                state.pending = None
                return False

            now = datetime.now(timezone.utc)
            last_edit_at = state.last_edit_at
            due = (
                force
                or last_edit_at is None
                or (now - last_edit_at).total_seconds() >= float(min_interval_seconds)
            )
            if due:
                return await self._perform_edit(
                    state, _PendingEdit(message, content, embed, view, force), next_fp, not_found_cb
                )

            state.pending = _PendingEdit(message, content, embed, view, force)
            if state.task is None or state.task.done():
                delay = max(0.1, float(min_interval_seconds) - (now - last_edit_at).total_seconds())
                state.task = asyncio.create_task(
                    self._flush_later(message_id, delay, float(min_interval_seconds), not_found_cb)
                )
            return False

    async def _flush_later(
        self, message_id: int, delay: float, min_interval_seconds: float, not_found_cb
    ) -> None:
        await asyncio.sleep(delay)
        state = self._states.get(int(message_id))
        if state is None:
            return
        async with state.lock:
            pending = state.pending
            if pending is None:
                return
            next_fp = _fingerprint(pending.content, pending.embed, pending.view)
            if state.last_fingerprint == next_fp:
                state.pending = None
                return
            await self._perform_edit(state, pending, next_fp, not_found_cb)
            state.pending = None

    async def _perform_edit(
        self, state: _EditState, pending: _PendingEdit, fingerprint: str, not_found_cb
    ) -> bool:
        try:
            await pending.message.edit(
                content=pending.content, embed=pending.embed, view=pending.view
            )
            state.last_edit_at = datetime.now(timezone.utc)
            state.last_fingerprint = fingerprint
            return True
        except discord.NotFound:
            if callable(not_found_cb):
                await not_found_cb()
            log.warning(
                "Panel edit skipped; message not found message_id=%s",
                getattr(pending.message, "id", "unknown"),
            )
        except discord.Forbidden:
            log.warning(
                "Panel edit skipped; forbidden message_id=%s",
                getattr(pending.message, "id", "unknown"),
            )
        except discord.HTTPException as exc:
            log.warning(
                "Panel edit failed message_id=%s status=%s",
                getattr(pending.message, "id", "unknown"),
                getattr(exc, "status", None),
            )
        except Exception:
            log.exception(
                "Panel edit failed message_id=%s", getattr(pending.message, "id", "unknown")
            )
        return False


PANEL_EDIT_SAFETY = PanelEditSafety()
