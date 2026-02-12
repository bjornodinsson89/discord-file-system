from __future__ import annotations

from repositories.torn_items import TornItemsRepository, norm_name as repo_norm_name


def norm_name(s: str) -> str:
    return repo_norm_name(s)


class ItemResolver:
    def __init__(self, db_pool):
        self.pool = db_pool
        self.repo = TornItemsRepository(db_pool)
        self._id_cache: dict[str, int] = {}
        self._item_cache: dict[str, dict | None] = {}

    async def resolve_item_id(self, user_input: str) -> int:
        key = norm_name(user_input)
        if not key:
            return 0
        if key in self._id_cache:
            return self._id_cache[key]

        item = await self.resolve_item(user_input)
        item_id = int(item.get("item_id") or 0) if item else 0
        self._id_cache[key] = item_id
        return item_id

    async def resolve_item(self, user_input: str) -> dict | None:
        key = norm_name(user_input)
        if not key:
            return None
        if key in self._item_cache:
            return self._item_cache[key]

        meta = await self.repo.get_item_by_name(user_input)
        item = None
        if meta:
            item = {
                "item_id": int(meta.get("item_id") or 0),
                "name": meta.get("name"),
                "norm_name": norm_name(meta.get("name") or ""),
                "image_url": meta.get("image_url"),
            }

        self._item_cache[key] = item
        self._id_cache[key] = int(item.get("item_id") or 0) if item else 0
        return item
