"""Compatibility shim for database access.

Phase 1 routes SQL through repository modules under ``repositories/``.
"""

from __future__ import annotations

import logging

from repositories.legacy_database import DatabaseManager, get_database as _get_database, init_database as _init_database

log = logging.getLogger("happy_jumper.database")
_warned = False


async def init_database() -> DatabaseManager:
    global _warned
    if not _warned:
        log.warning("utils.database is a compatibility shim; migrate callers to repositories/* modules")
        _warned = True
    return await _init_database()


def get_database() -> DatabaseManager:
    return _get_database()


__all__ = ["DatabaseManager", "init_database", "get_database"]
