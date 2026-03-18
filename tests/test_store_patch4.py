from __future__ import annotations

import asyncio
from pathlib import Path

from services.store_service import StoreService
from repositories.torn_items import TornItemLookupError


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Conn:
    def transaction(self):
        return _Tx()


class _Acquire:
    async def __aenter__(self):
        return _Conn()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Role:
    def __init__(self, rid: int):
        self.id = rid


class _Member:
    def __init__(self, uid: int, roles=None, fail_add: bool = False, on_add=None):
        self.id = uid
        self.roles = list(roles or [])
        self._fail_add = fail_add
        self._on_add = on_add

    async def add_roles(self, role, reason=None):
        if self._on_add is not None:
            self._on_add()
        if self._fail_add:
            raise RuntimeError("no perms")
        if role not in self.roles:
            self.roles.append(role)


class _Guild:
    def __init__(self, gid: int, roles=None):
        self.id = gid
        self._roles = {r.id: r for r in (roles or [])}

    def get_role(self, rid: int):
        return self._roles.get(rid)

    def get_channel(self, _cid: int):
        return None


class _FakeStoreRepo:
    def __init__(self, *, item: dict | None = None):
        self.item = item
        self.redemptions = []
        self.stock_delta = 0
        self.torn_description = None

    def acquire(self):
        return _Acquire()

    async def upsert_guild_settings_with_conn(self, _conn, _guild_id: int, **_changes):
        return {
            "enabled": True,
            "torn_item_store_enabled": True,
            "discord_perk_store_enabled": True,
            "fulfillment_channel_id": None,
        }

    async def get_item(self, _guild_id: int, _item_id: int, *, for_update=False, conn=None):
        return dict(self.item) if self.item else None

    async def count_user_redemptions_for_item(self, *_args, **_kwargs):
        return 0

    async def create_redemption(self, **payload):
        data = {
            "id": len(self.redemptions) + 1,
            "created_at": payload.get("fulfilled_at"),
            **payload,
        }
        self.redemptions.append(data)
        return data

    async def adjust_stock(self, _guild_id: int, _item_id: int, delta: int, *, conn=None):
        self.stock_delta += delta

    async def get_redemption(
        self, guild_id: int, redemption_id: int, *, conn=None, for_update=False
    ):
        for r in self.redemptions:
            if r["guild_id"] == guild_id and r["id"] == redemption_id:
                return dict(r)
        return None

    async def update_redemption(self, guild_id: int, redemption_id: int, *, conn=None, **changes):
        for r in self.redemptions:
            if r["guild_id"] == guild_id and r["id"] == redemption_id:
                r.update(changes)
                return dict(r)
        return None

    async def lookup_torn_thumbnail(self, **_kwargs):
        return "https://img.example/x.png"

    async def lookup_torn_description(self, **_kwargs):
        return self.torn_description


class _FakeTornItemsRepo:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.lookups = []

    async def resolve_store_item_match_by_name(self, raw_name: str):
        self.lookups.append(raw_name)
        if self.error is not None:
            raise self.error
        return self.result


class _FakeCreateItemRepo(_FakeStoreRepo):
    def __init__(self):
        super().__init__()
        self.created_payload = None

    async def create_item(self, **payload):
        self.created_payload = dict(payload)
        return {"id": 55, **payload}


class _Response:
    def __init__(self):
        self.kwargs = None

    async def send_message(self, *args, **kwargs):
        self.kwargs = {"args": args, **kwargs}


class _Interaction:
    def __init__(self, guild_id: int = 1, user_id: int = 2):
        self.guild_id = guild_id
        self.user = type("User", (), {"id": user_id})()
        self.response = _Response()


class _FakeTokenSvc:
    def __init__(self, *, allow_spend: bool = True, events=None):
        self.allow_spend = allow_spend
        self.spent = 0
        self.refunded = 0
        self.events = events if events is not None else []

    async def spend_store_hjd(self, *, amount: int, **_kwargs):
        self.events.append("spend")
        if not self.allow_spend:
            raise ValueError("insufficient HJD balance")
        self.spent += amount
        return True

    async def refund_store_hjd(self, *, amount: int, **_kwargs):
        self.events.append("refund")
        self.refunded += amount
        return True


def test_store_migration_contains_required_new_tables_and_no_torn_items_repurpose():
    src = Path("migrations/2026_03_19_add_reward_store_tables.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.reward_store_items" in src
    assert "CREATE TABLE IF NOT EXISTS public.reward_redemptions" in src
    assert "CREATE TABLE IF NOT EXISTS public.store_guild_settings" in src
    assert "status IN ('pending', 'fulfilled', 'cancelled', 'refunded')" in src
    assert "fulfillment_type IN ('admin_manual', 'discord_role', 'discord_action')" in src


def test_store_setup_panel_and_commands_exist():
    setup_src = Path("setup_panel.py").read_text(encoding="utf-8")
    assert "class StoreSetupView" in setup_src
    assert "Toggle store enabled" in setup_src
    assert "Toggle Torn item store" in setup_src
    assert "Toggle Discord perk store" in setup_src

    cog_src = Path("cogs/store.py").read_text(encoding="utf-8")
    assert '@app_commands.command(name="store"' not in cog_src
    assert '@app_commands.command(name="store_admin"' not in cog_src
    assert "class AdminStorefrontView" in cog_src


def test_store_thumbnail_lookup_can_use_torn_items_data_source():
    src = Path("repositories/store.py").read_text(encoding="utf-8")
    assert "SELECT image_url FROM torn_items" in src


def test_redeem_torn_item_creates_pending_and_spends_and_decrements_stock():
    async def _run():
        repo = _FakeStoreRepo(
            item={
                "id": 12,
                "name": "Xanax",
                "category": "torn_item",
                "token_cost": 5,
                "stock": 2,
                "is_active": True,
                "fulfillment_type": "admin_manual",
            }
        )
        token = _FakeTokenSvc()
        service = StoreService(repo, token)
        guild = _Guild(5)
        user = _Member(8)
        redemption, err = await service.redeem_item(guild=guild, user=user, item_id=12)
        assert err is None
        assert redemption is not None
        assert redemption["status"] == "pending"
        assert token.spent == 5
        assert repo.stock_delta == -1

    asyncio.run(_run())


def test_redeem_blocks_on_insufficient_tokens():
    async def _run():
        repo = _FakeStoreRepo(
            item={
                "id": 12,
                "name": "Xanax",
                "category": "torn_item",
                "token_cost": 5,
                "stock": 2,
                "is_active": True,
                "fulfillment_type": "admin_manual",
            }
        )
        service = StoreService(repo, _FakeTokenSvc(allow_spend=False))
        guild = _Guild(5)
        user = _Member(8)
        try:
            await service.redeem_item(guild=guild, user=user, item_id=12)
            raise AssertionError("expected insufficient balance error")
        except ValueError as exc:
            assert "insufficient" in str(exc)

    asyncio.run(_run())


def test_redeem_blocks_on_out_of_stock():
    async def _run():
        repo = _FakeStoreRepo(
            item={
                "id": 12,
                "name": "Xanax",
                "category": "torn_item",
                "token_cost": 5,
                "stock": 0,
                "is_active": True,
                "fulfillment_type": "admin_manual",
            }
        )
        service = StoreService(repo, _FakeTokenSvc())
        redemption, err = await service.redeem_item(guild=_Guild(1), user=_Member(2), item_id=12)
        assert redemption is None
        assert "out of stock" in str(err).lower()

    asyncio.run(_run())


def test_discord_role_redemption_fulfills_immediately_and_blocks_if_member_already_has_role():
    async def _run():
        role = _Role(77)
        guild = _Guild(1, roles=[role])
        item = {
            "id": 1,
            "name": "VIP",
            "category": "discord_perk",
            "token_cost": 3,
            "stock": None,
            "is_active": True,
            "fulfillment_type": "discord_role",
            "discord_role_id": 77,
        }
        repo = _FakeStoreRepo(item=item)
        service = StoreService(repo, _FakeTokenSvc())
        fresh = _Member(10)
        redemption, err = await service.redeem_item(guild=guild, user=fresh, item_id=1)
        assert err is None
        assert redemption["status"] == "fulfilled"

        already = _Member(11, roles=[role])
        redemption2, err2 = await service.redeem_item(guild=guild, user=already, item_id=1)
        assert redemption2 is None
        assert "already have" in str(err2).lower()

    asyncio.run(_run())


def test_refund_restores_tokens_and_stock_atomically():
    async def _run():
        repo = _FakeStoreRepo(
            item={
                "id": 1,
                "name": "Item",
                "category": "torn_item",
                "token_cost": 4,
                "stock": 5,
                "is_active": True,
                "fulfillment_type": "admin_manual",
            }
        )
        token = _FakeTokenSvc()
        service = StoreService(repo, token)
        repo.redemptions.append(
            {
                "id": 1,
                "guild_id": 1,
                "user_id": 22,
                "store_item_id": 1,
                "status": "pending",
                "token_cost": 4,
            }
        )
        updated, err = await service.refund_redemption(
            guild_id=1, redemption_id=1, admin_user_id=999
        )
        assert err is None
        assert updated["status"] == "refunded"
        assert token.refunded == 4
        assert repo.stock_delta == 1

    asyncio.run(_run())


def test_store_repository_has_item_crud_and_pending_queue_methods():
    src = Path("repositories/store.py").read_text(encoding="utf-8")
    for snippet in [
        "async def create_item",
        "async def update_item",
        "async def adjust_stock",
        "async def list_pending_redemptions",
    ]:
        assert snippet in src


def test_torn_items_repository_semantics_unchanged_and_store_uses_read_only_lookup():
    torn_src = Path("repositories/torn_items.py").read_text(encoding="utf-8")
    assert "class TornItemsRepository" in torn_src
    assert "upsert_items" in torn_src
    store_src = Path("repositories/store.py").read_text(encoding="utf-8")
    assert "lookup_torn_thumbnail" in store_src
    assert "lookup_torn_description" in store_src
    assert "SELECT image_url FROM torn_items" in store_src
    assert "SELECT description FROM torn_items" in store_src


def test_torn_items_description_migration_exists():
    src = Path("migrations/2026_03_18_add_torn_item_descriptions.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE public.torn_items" in src
    assert "ADD COLUMN IF NOT EXISTS description TEXT NULL" in src


def test_torn_item_sync_and_repository_persist_descriptions_in_source():
    repo_src = Path("repositories/torn_items.py").read_text(encoding="utf-8")
    events_src = Path("cogs/events.py").read_text(encoding="utf-8")
    assert "INSERT INTO torn_items(item_id, name, norm_name, image_url, description)" in repo_src
    assert "description = EXCLUDED.description" in repo_src
    assert 'description = item.get("description")' in events_src
    assert "rows.append((item_id, name, normalized, image_url, description))" in events_src


def test_setup_store_persistence_path_uses_store_guild_settings():
    src = Path("setup_panel.py").read_text(encoding="utf-8")
    assert "save_store_changes" in src
    assert "upsert_guild_settings" in src
    assert "store_settings" in src


def test_discord_role_redemption_charges_before_role_grant_and_refunds_on_grant_failure():
    async def _run():
        role = _Role(77)
        guild = _Guild(1, roles=[role])
        item = {
            "id": 1,
            "name": "VIP",
            "category": "discord_perk",
            "token_cost": 3,
            "stock": 2,
            "is_active": True,
            "fulfillment_type": "discord_role",
            "discord_role_id": 77,
        }

        events = []
        token = _FakeTokenSvc(events=events)

        def _on_add():
            events.append("add_role")

        repo = _FakeStoreRepo(item=item)
        service = StoreService(repo, token)
        member = _Member(10, fail_add=True, on_add=_on_add)

        redemption, err = await service.redeem_item(guild=guild, user=member, item_id=1)
        assert redemption is None
        assert "no hjd were charged" in str(err).lower()
        assert events[0] == "spend"
        assert events[1] == "add_role"
        assert events[2] == "refund"
        assert token.spent == 3
        assert token.refunded == 3

    asyncio.run(_run())


def test_create_torn_item_resolves_thumbnail_and_persists_metadata():
    async def _run():
        repo = _FakeCreateItemRepo()
        torn_repo = _FakeTornItemsRepo(
            result={
                "item_id": 123,
                "name": "Xanax",
                "image_url": "https://img.example/xanax.png",
                "description": "A potent painkiller.",
            }
        )
        service = StoreService(repo, _FakeTokenSvc(), torn_repo)
        item, note = await service.create_store_item(
            guild_id=1,
            name="Xanax",
            description="heal",
            category="torn_item",
            token_cost=5,
            stock=3,
            fulfillment_type="admin_manual",
            created_by=7,
        )
        assert note is None
        assert torn_repo.lookups == ["Xanax"]
        assert repo.created_payload["torn_item_id"] == 123
        assert repo.created_payload["torn_item_name"] == "Xanax"
        assert repo.created_payload["thumbnail_url"] == "https://img.example/xanax.png"
        assert repo.created_payload["description"] == "heal"
        assert item["thumbnail_url"] == "https://img.example/xanax.png"

    asyncio.run(_run())


def test_create_torn_item_matches_case_insensitively():
    async def _run():
        repo = _FakeCreateItemRepo()
        torn_repo = _FakeTornItemsRepo(
            result={
                "item_id": 123,
                "name": "Xanax",
                "image_url": "https://img.example/xanax.png",
                "description": "A potent painkiller.",
            }
        )
        service = StoreService(repo, _FakeTokenSvc(), torn_repo)
        item, _note = await service.create_store_item(
            guild_id=1,
            name="  xAnAx  ",
            description=None,
            category="torn_item",
            token_cost=5,
            stock=None,
            fulfillment_type="admin_manual",
            created_by=7,
        )
        assert torn_repo.lookups == ["xAnAx"]
        assert item["torn_item_name"] == "Xanax"
        assert item["description"] == "A potent painkiller."

    asyncio.run(_run())


def test_create_torn_item_ambiguous_name_fails_cleanly():
    async def _run():
        repo = _FakeCreateItemRepo()
        torn_repo = _FakeTornItemsRepo(
            error=TornItemLookupError(
                "Multiple Torn items match 'Xan'. Please enter a more specific item name."
            )
        )
        service = StoreService(repo, _FakeTokenSvc(), torn_repo)
        try:
            await service.create_store_item(
                guild_id=1,
                name="Xan",
                description=None,
                category="torn_item",
                token_cost=5,
                stock=None,
                fulfillment_type="admin_manual",
                created_by=7,
            )
            raise AssertionError("expected ambiguous-name error")
        except ValueError as exc:
            assert "more specific" in str(exc)
        assert repo.created_payload is None

    asyncio.run(_run())


def test_create_torn_item_missing_match_creates_without_thumbnail():
    async def _run():
        repo = _FakeCreateItemRepo()
        service = StoreService(repo, _FakeTokenSvc(), _FakeTornItemsRepo(result=None))
        item, note = await service.create_store_item(
            guild_id=1,
            name="Mystery Pill",
            description=None,
            category="torn_item",
            token_cost=5,
            stock=None,
            fulfillment_type="admin_manual",
            created_by=7,
        )
        assert "no Torn image match" in note
        assert item["thumbnail_url"] is None
        assert item["torn_item_name"] == "Mystery Pill"

    asyncio.run(_run())


def test_store_detail_embed_uses_thumbnail_url():
    from cogs.store import StoreCog

    class _Repo(_FakeStoreRepo):
        async def get_item(self, *_args, **_kwargs):
            return {
                "id": 9,
                "name": "Xanax",
                "description": "desc",
                "token_cost": 5,
                "stock": 3,
                "fulfillment_type": "admin_manual",
                "thumbnail_url": "https://img.example/xanax.png",
            }

    class _Svc:
        async def resolve_description(self, item):
            return item.get("description") or "No description provided."

        async def resolve_thumbnail(self, item):
            return item.get("thumbnail_url")

    async def _run():
        cog = StoreCog.__new__(StoreCog)
        cog.store_repo = _Repo()
        cog.store_service = _Svc()
        interaction = _Interaction()
        await StoreCog.send_item_detail(cog, interaction, 9)
        embed = interaction.response.kwargs["embed"]
        assert str(embed.thumbnail.url) == "https://img.example/xanax.png"

    asyncio.run(_run())


def test_blank_store_item_description_falls_back_to_torn_metadata_on_create():
    async def _run():
        repo = _FakeCreateItemRepo()
        torn_repo = _FakeTornItemsRepo(
            result={
                "item_id": 123,
                "name": "Xanax",
                "image_url": "https://img.example/xanax.png",
                "description": "Restores happiness and reduces hospital time.",
            }
        )
        service = StoreService(repo, _FakeTokenSvc(), torn_repo)
        item, _note = await service.create_store_item(
            guild_id=1,
            name="Xanax",
            description=None,
            category="torn_item",
            token_cost=5,
            stock=3,
            fulfillment_type="admin_manual",
            created_by=7,
        )
        assert repo.created_payload["description"] == "Restores happiness and reduces hospital time."
        assert item["description"] == "Restores happiness and reduces hospital time."

    asyncio.run(_run())


def test_custom_store_item_description_is_preserved():
    async def _run():
        repo = _FakeCreateItemRepo()
        torn_repo = _FakeTornItemsRepo(
            result={
                "item_id": 123,
                "name": "Xanax",
                "image_url": "https://img.example/xanax.png",
                "description": "Torn metadata description.",
            }
        )
        service = StoreService(repo, _FakeTokenSvc(), torn_repo)
        item, _note = await service.create_store_item(
            guild_id=1,
            name="Xanax",
            description="Custom storefront copy.",
            category="torn_item",
            token_cost=5,
            stock=3,
            fulfillment_type="admin_manual",
            created_by=7,
        )
        assert repo.created_payload["description"] == "Custom storefront copy."
        assert item["description"] == "Custom storefront copy."

    asyncio.run(_run())


def test_store_embed_description_fallback_order():
    async def _run():
        repo = _FakeStoreRepo()
        repo.torn_description = "Torn fallback description."
        service = StoreService(repo, _FakeTokenSvc())

        own = await service.resolve_description({"description": "Store description", "category": "torn_item"})
        torn = await service.resolve_description({"description": "", "category": "torn_item", "torn_item_id": 123})
        empty = await service.resolve_description({"description": "", "category": "discord_perk"})

        assert own == "Store description"
        assert torn == "Torn fallback description."
        assert empty == "No description provided."

    asyncio.run(_run())


def test_add_item_flow_no_longer_uses_extras_field_in_modal_source():
    src = Path("cogs/store.py").read_text(encoding="utf-8")
    assert "Extras (role_id,stock,torn_item_name)" not in src
    assert "role_id=123;stock=5;torn_item_name=Xanax" not in src
    assert 'label="Stock"' in src
    assert "Choose a fulfillment type for this Discord perk." in src
