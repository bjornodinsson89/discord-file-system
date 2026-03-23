from __future__ import annotations

import types
from pathlib import Path

import asyncio

import pytest

from cogs import bank, jewelry_alert
from repositories.users import UsersRepository
from services.admin_key_pool import AdminKeyPoolService
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError


class _FakeRole:
    def __init__(self, role_id: int, *, mentionable: bool = True, name: str | None = None):
        self.id = role_id
        self.mentionable = mentionable
        self.name = name or f"role-{role_id}"
        self.mention = f"<@&{role_id}>"


class _FakePerms:
    def __init__(
        self,
        *,
        administrator: bool = False,
        manage_guild: bool = False,
        mention_everyone: bool = True,
    ):
        self.administrator = administrator
        self.manage_guild = manage_guild
        self.mention_everyone = mention_everyone


class _FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        administrator: bool = False,
        manage_guild: bool = False,
        roles: list[_FakeRole] | None = None,
    ):
        self.id = member_id
        self.roles = roles or []
        self.guild_permissions = _FakePerms(administrator=administrator, manage_guild=manage_guild)


class _FakeGuild:
    def __init__(
        self,
        guild_id: int,
        members: list[_FakeMember],
        *,
        owner_id: int,
        roles: list[_FakeRole] | None = None,
    ):
        self.id = guild_id
        self.members = members
        self.owner_id = owner_id
        self._roles = {role.id: role for role in (roles or [])}
        self.me = _FakeMember(999999)
        self.channels: dict[int, object] = {}

    def get_member(self, member_id: int):
        for member in self.members:
            if member.id == member_id:
                return member
        return None

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        return self.channels.get(channel_id)


class _FakeSecurity:
    def decrypt_api_key(self, encrypted: str) -> str:
        if encrypted == "bad-encrypted":
            raise ValueError("cannot decrypt")
        return encrypted.replace("enc-", "")


class _FakeUsersRepo:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.invalid_calls: list[int] = []
        self.reset_calls: list[int] = []
        self.invalid_results: dict[int, tuple[int, bool]] = {}

    async def list_user_api_keys_by_discord_ids(self, discord_ids: list[int]) -> list[dict]:
        wanted = set(discord_ids)
        return [row for row in self.rows if int(row["discord_id"]) in wanted]

    async def record_invalid_key_failure(self, discord_id: int) -> tuple[int, bool]:
        self.invalid_calls.append(discord_id)
        return self.invalid_results.get(discord_id, (1, False))

    async def reset_invalid_key_failures(self, discord_id: int) -> None:
        self.reset_calls.append(discord_id)


class _FakeSettingsRepo:
    def __init__(self, admin_role_ids: list[int] | None = None, settings: dict | None = None):
        self.admin_role_ids = admin_role_ids or []
        self.settings = settings or {}
        self.upserts: list[tuple[int, dict]] = []

    async def get_or_create(self, guild_id: int) -> dict:
        data = {"admin_role_ids": list(self.admin_role_ids)}
        data.update(self.settings)
        return data

    async def upsert_settings(self, guild_id: int, **updates):
        self.upserts.append((guild_id, updates))
        self.settings.update(updates)
        return self.settings


class _FakeTornAPI:
    def __init__(self, *, bank_plan=None, shop_plan=None):
        self.bank_plan = list(bank_plan or [])
        self.shop_plan = list(shop_plan or [])
        self.bank_keys: list[str] = []
        self.shop_keys: list[str] = []

    async def get_bank_rates(self, api_key: str):
        self.bank_keys.append(api_key)
        result = self.bank_plan.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def get_shoplifting(self, api_key: str):
        self.shop_keys.append(api_key)
        result = self.shop_plan.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeResponse:
    def __init__(self):
        self.messages: list[dict] = []

    async def send_message(self, content=None, embed=None, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "embed": embed, "ephemeral": ephemeral, **kwargs})


class _FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.response = _FakeResponse()


class _FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.mention = f"<#${channel_id}>".replace("$", "")
        self.sent: list[dict] = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(id=444)


def _make_service(*, admin_role_ids=None, rows=None, torn_api=None):
    service = AdminKeyPoolService.__new__(AdminKeyPoolService)
    service._settings_repo = _FakeSettingsRepo(admin_role_ids=admin_role_ids or [])
    service._users_repo = _FakeUsersRepo(rows=rows or [])
    service._locks = {}
    service._next_index_by_guild = {}
    return service, service._users_repo, service._settings_repo


def test_bank_calc_uses_pooled_admin_keys_without_guild_key(monkeypatch):
    guild = _FakeGuild(10, [_FakeMember(101, administrator=True)], owner_id=500)
    cog = bank.BankCog.__new__(bank.BankCog)
    cog.bot = object()
    cog._repo = _FakeSettingsRepo(settings={"bank_rates_api_key_encrypted": None})
    cog._admin_key_pool = types.SimpleNamespace(
        get_bank_rates_for_guild=lambda _guild: _fake_async(
            {"1w": 10, "2w": 11, "1m": 12, "2m": 13, "3m": 14}
        )
    )
    cog._cache = {}
    cog._cache_locks = {}

    interaction = _FakeInteraction(guild)
    asyncio.run(
        bank.BankCog.bank_calc.callback(cog, interaction, amount="100m", merits=0, tci=None)
    )

    assert interaction.response.messages
    embed = interaction.response.messages[0]["embed"]
    assert "Bank Investment Calculator" in embed.title


def test_jewelry_alert_poll_uses_pooled_admin_keys_without_guild_key(monkeypatch):
    guild = _FakeGuild(20, [_FakeMember(201, administrator=True)], owner_id=201)
    channel = _FakeChannel(333)
    guild.channels[333] = channel

    cog = jewelry_alert.JewelryAlertCog.__new__(jewelry_alert.JewelryAlertCog)
    cog.bot = types.SimpleNamespace(user=types.SimpleNamespace(id=999999))
    cog._db = object()
    cog._repo = _FakeSettingsRepo(
        settings={
            "jewelry_alert_channel_id": 333,
            "bank_rates_api_key_encrypted": None,
            "jewelry_alert_role_ids": [],
        }
    )
    cog._admin_key_pool = types.SimpleNamespace(
        get_shoplifting_for_guild=lambda _guild: _fake_async(
            {"jewelry_store": [{"disabled": True}, {"disabled": True}]}
        )
    )
    cog._log_throttle_until = {}
    cog._meme_png_cache = b"png"

    async def _send_announcement(**kwargs):
        return 555

    cog._send_announcement = _send_announcement
    cog._delete_announcement = lambda **kwargs: _fake_async(None)

    asyncio.run(jewelry_alert.JewelryAlertCog._poll_guild(cog, guild))

    assert cog._repo.upserts
    assert cog._repo.upserts[0][1]["jewelry_alert_last_announcement_message_id"] == 555


def test_invalid_key_failure_increments_and_resets_after_success(monkeypatch):
    guild = _FakeGuild(
        30,
        [_FakeMember(301, administrator=True), _FakeMember(302, manage_guild=True)],
        owner_id=999,
    )
    rows = [
        {"discord_id": 301, "encrypted_key": "enc-key-a"},
        {"discord_id": 302, "encrypted_key": "enc-key-b"},
    ]
    service, users_repo, _ = _make_service(rows=rows)
    torn = _FakeTornAPI(
        bank_plan=[
            TornAPIError("Incorrect key"),
            {"1w": 1, "2w": 2, "1m": 3, "2m": 4, "3m": 5},
            {"1w": 9, "2w": 8, "1m": 7, "2m": 6, "3m": 5},
        ],
    )
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    first = asyncio.run(service.get_bank_rates_for_guild(guild))
    second = asyncio.run(service.get_bank_rates_for_guild(guild))

    assert first["1w"] == 1
    assert second["1w"] == 9
    assert users_repo.invalid_calls == [301]
    assert users_repo.reset_calls == [302, 301]
    assert torn.bank_keys[:3] == ["key-a", "key-b", "key-a"]


def test_invalid_key_deleted_after_three_failures(monkeypatch):
    guild = _FakeGuild(40, [_FakeMember(401, administrator=True)], owner_id=401)
    service, users_repo, _ = _make_service(rows=[{"discord_id": 401, "encrypted_key": "enc-key-a"}])
    users_repo.invalid_results[401] = (3, True)
    torn = _FakeTornAPI(bank_plan=[TornAPIError("Invalid key")])
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    with pytest.raises(TornAPIError):
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert users_repo.invalid_calls == [401]


def test_permission_failure_does_not_delete_key(monkeypatch):
    guild = _FakeGuild(50, [_FakeMember(501, administrator=True)], owner_id=501)
    service, users_repo, _ = _make_service(rows=[{"discord_id": 501, "encrypted_key": "enc-key-a"}])
    torn = _FakeTornAPI(bank_plan=[TornAPIPermissionError("missing access")])
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    with pytest.raises(TornAPIPermissionError):
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert users_repo.invalid_calls == []


def test_rate_limit_failure_does_not_delete_key(monkeypatch):
    guild = _FakeGuild(60, [_FakeMember(601, administrator=True)], owner_id=601)
    service, users_repo, _ = _make_service(rows=[{"discord_id": 601, "encrypted_key": "enc-key-a"}])
    torn = _FakeTornAPI(bank_plan=[TornAPIRateLimitError("rate limit")])
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    with pytest.raises(TornAPIRateLimitError):
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert users_repo.invalid_calls == []


def test_users_repository_invalid_key_tracking_queries():
    conn = _RepoConn(fail_count=2, delete_on_third=True)
    repo = UsersRepository.__new__(UsersRepository)
    repo.pool = object()
    repo.acquire = lambda: conn

    new_count, deleted = asyncio.run(repo.record_invalid_key_failure(700))
    asyncio.run(repo.reset_invalid_key_failures(700))

    assert new_count == 3
    assert deleted is True
    assert any("invalid_key_fail_count = 0" in query for query, _args in conn.executed)


def test_payment_verification_paths_do_not_use_admin_key_pool():
    for relative_path in ("cogs/raffles.py", "cogs/events.py"):
        text = Path(relative_path).read_text()
        assert "AdminKeyPoolService" not in text


class _RepoTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RepoConn:
    def __init__(self, *, fail_count: int, delete_on_third: bool):
        self.fail_count = fail_count
        self.delete_on_third = delete_on_third
        self.executed: list[tuple[str, tuple]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def transaction(self):
        return _RepoTransaction()

    async def fetchrow(self, query, *args):
        self.executed.append((query, args))
        if "UPDATE public.user_api_keys" in query:
            self.fail_count += 1
            return {"invalid_key_fail_count": self.fail_count}
        if "DELETE FROM public.user_api_keys" in query:
            return {"discord_id": args[0]} if self.delete_on_third else None
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return None


async def _fake_async(result):
    return result


def test_single_mode_uses_only_selected_admin_key(monkeypatch):
    guild = _FakeGuild(70, [_FakeMember(701, administrator=True), _FakeMember(702, administrator=True)], owner_id=701)
    rows = [
        {"discord_id": 701, "encrypted_key": "enc-key-a"},
        {"discord_id": 702, "encrypted_key": "enc-key-b"},
    ]
    service, users_repo, settings_repo = _make_service(rows=rows)
    settings_repo.settings.update({"admin_key_strategy": "single", "admin_key_single_discord_id": 702})
    torn = _FakeTornAPI(bank_plan=[{"1w": 4, "2w": 5, "1m": 6, "2m": 7, "3m": 8}])
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    result = asyncio.run(service.get_bank_rates_for_guild(guild))

    assert result["1w"] == 4
    assert torn.bank_keys == ["key-b"]
    assert users_repo.reset_calls == [702]


def test_single_mode_does_not_fall_back_to_other_admin_keys(monkeypatch):
    guild = _FakeGuild(71, [_FakeMember(711, administrator=True), _FakeMember(712, administrator=True)], owner_id=711)
    rows = [
        {"discord_id": 711, "encrypted_key": "enc-key-a"},
        {"discord_id": 712, "encrypted_key": "enc-key-b"},
    ]
    service, users_repo, settings_repo = _make_service(rows=rows)
    settings_repo.settings.update({"admin_key_strategy": "single", "admin_key_single_discord_id": 711})
    torn = _FakeTornAPI(bank_plan=[TornAPIPermissionError("missing access")])
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    with pytest.raises(TornAPIPermissionError):
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert torn.bank_keys == ["key-a"]
    assert users_repo.invalid_calls == []


def test_single_mode_invalid_key_deleted_after_three_failures(monkeypatch):
    guild = _FakeGuild(72, [_FakeMember(721, administrator=True)], owner_id=721)
    service, users_repo, settings_repo = _make_service(rows=[{"discord_id": 721, "encrypted_key": "enc-key-a"}])
    settings_repo.settings.update({"admin_key_strategy": "single", "admin_key_single_discord_id": 721})
    users_repo.invalid_results[721] = (3, True)
    torn = _FakeTornAPI(bank_plan=[TornAPIError("Invalid key")])
    monkeypatch.setattr("services.admin_key_pool.get_torn_api", lambda: torn)
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    with pytest.raises(TornAPIError) as excinfo:
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert "selected admin key is not working" in str(excinfo.value).lower()
    assert users_repo.invalid_calls == [721]


def test_single_mode_missing_selected_key_returns_clear_failure(monkeypatch):
    guild = _FakeGuild(73, [_FakeMember(731, administrator=True)], owner_id=731)
    service, _users_repo, settings_repo = _make_service(rows=[])
    settings_repo.settings.update({"admin_key_strategy": "single", "admin_key_single_discord_id": 731})
    monkeypatch.setattr("services.admin_key_pool.get_security_manager", lambda: _FakeSecurity())

    with pytest.raises(TornAPIError) as excinfo:
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert "selected admin has no stored torn api key" in str(excinfo.value).lower()


def test_single_mode_ineligible_selected_admin_returns_clear_failure(monkeypatch):
    guild = _FakeGuild(74, [_FakeMember(741), _FakeMember(742, administrator=True)], owner_id=742)
    service, _users_repo, settings_repo = _make_service(rows=[{"discord_id": 741, "encrypted_key": "enc-key-a"}])
    settings_repo.settings.update({"admin_key_strategy": "single", "admin_key_single_discord_id": 741})

    with pytest.raises(TornAPIError) as excinfo:
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert "no longer eligible" in str(excinfo.value).lower()


def test_single_mode_missing_member_returns_clear_failure(monkeypatch):
    guild = _FakeGuild(75, [_FakeMember(752, administrator=True)], owner_id=752)
    service, _users_repo, settings_repo = _make_service(rows=[{"discord_id": 751, "encrypted_key": "enc-key-a"}])
    settings_repo.settings.update({"admin_key_strategy": "single", "admin_key_single_discord_id": 751})

    with pytest.raises(TornAPIError) as excinfo:
        asyncio.run(service.get_bank_rates_for_guild(guild))

    assert "no longer in this server" in str(excinfo.value).lower()


def test_bank_calc_single_mode_maps_clear_errors():
    guild = _FakeGuild(80, [_FakeMember(801, administrator=True)], owner_id=801)
    cog = bank.BankCog.__new__(bank.BankCog)
    cog.bot = object()
    cog._repo = _FakeSettingsRepo(settings={"admin_key_strategy": "single"})
    cog._admin_key_pool = types.SimpleNamespace(
        get_bank_rates_for_guild=lambda _guild: _fake_raise(TornAPIError("No single admin key is configured for this server."))
    )
    cog._cache = {}
    cog._cache_locks = {}

    interaction = _FakeInteraction(guild)
    asyncio.run(bank.BankCog.bank_calc.callback(cog, interaction, amount="100m", merits=0, tci=None))

    embed = interaction.response.messages[0]["embed"]
    assert "no single admin key is configured" in embed.description.lower()


def test_jewelry_alert_single_mode_skips_cleanly_for_missing_single_admin():
    guild = _FakeGuild(81, [_FakeMember(811, administrator=True)], owner_id=811)
    channel = _FakeChannel(333)
    guild.channels[333] = channel

    cog = jewelry_alert.JewelryAlertCog.__new__(jewelry_alert.JewelryAlertCog)
    cog.bot = types.SimpleNamespace(user=types.SimpleNamespace(id=999999))
    cog._db = object()
    cog._repo = _FakeSettingsRepo(settings={"jewelry_alert_channel_id": 333, "admin_key_strategy": "single", "jewelry_alert_role_ids": []})
    cog._admin_key_pool = types.SimpleNamespace(
        get_shoplifting_for_guild=lambda _guild: _fake_raise(TornAPIError("No single admin key is configured for this server."))
    )
    cog._log_throttle_until = {}
    cog._meme_png_cache = b"png"
    messages: list[str] = []
    cog._log_throttled = lambda guild_id, error_type, message, *args: messages.append(message % args)

    asyncio.run(jewelry_alert.JewelryAlertCog._poll_guild(cog, guild))

    assert any("no single admin key configured" in msg.lower() for msg in messages)


async def _fake_raise(exc):
    raise exc


def test_jump_99k_paths_do_not_use_admin_key_pool():
    for relative_path in ("cogs/events.py", "services/jump_service.py", "services/jump_monitor.py"):
        text = Path(relative_path).read_text()
        assert "AdminKeyPoolService" not in text
