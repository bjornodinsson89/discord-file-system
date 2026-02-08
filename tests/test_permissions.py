import asyncio

import pytest
from fastapi import HTTPException

import web.permissions as permissions


def test_has_required_permission_accepts_manage_guild_bit():
    assert permissions.has_required_guild_admin_permission(str(0x20)) is True


def test_require_guild_admin_error_message(monkeypatch):
    async def _fake_get_user_guilds(user):
        return []

    monkeypatch.setattr(permissions, "get_user_guilds", _fake_get_user_guilds)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(permissions.require_guild_admin(123, {"id": "1"}))

    assert exc_info.value.status_code == 403
    assert "Owner, Administrator, or Manage Server" in exc_info.value.detail
