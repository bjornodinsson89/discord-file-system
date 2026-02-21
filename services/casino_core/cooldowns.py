from __future__ import annotations

from repositories.casino_core import CasinoCoreRepository
from utils.database import get_pool


class CasinoCooldownsService:
    def __init__(self):
        self.repo = CasinoCoreRepository(get_pool())
