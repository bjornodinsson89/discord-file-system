from __future__ import annotations

import asyncio
from types import SimpleNamespace

from utils.embeds import create_api_key_guide_embed
from utils.torn_api import TornAPIError, TornAPIPermissionError
from views import components


class _FakeResponse:
    def __init__(self):
        self.deferred = []

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred.append({"ephemeral": ephemeral, "thinking": thinking})


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.messages.append(
            {"content": content, "embed": embed, "view": view, "ephemeral": ephemeral}
        )


class _FakeUsersRepo:
    instances: list["_FakeUsersRepo"] = []

    def __init__(self, _pool):
        self.upserts = []
        type(self).instances.append(self)

    async def upsert_user_api_key(self, **kwargs):
        self.upserts.append(kwargs)

    async def get_user_api_key(self, _discord_id):
        return {"timezone_name": "UTC"}


class _FakeAuditRepo:
    instances: list["_FakeAuditRepo"] = []

    def __init__(self, _pool):
        self.audit_rows = []
        type(self).instances.append(self)

    async def log_audit(self, **kwargs):
        self.audit_rows.append(kwargs)


class _FakeSecurity:
    def encrypt(self, value):
        return f"enc:{value}"


class _FakeTornAPI:
    def __init__(self, *, validate_result=None, permission_error=False, api_error=None):
        self.validate_result = validate_result or (12345, 777, "Tester", set())
        self.permission_error = permission_error
        self.api_error = api_error
        self.calls = []

    async def validate_api_key(self, api_key, **kwargs):
        self.calls.append(("validate", api_key, kwargs))
        if self.api_error:
            raise self.api_error
        return self.validate_result

    async def get_item_send_receive_logs(self, api_key, **kwargs):
        self.calls.append(("item_logs", api_key, kwargs))
        if self.permission_error:
            raise TornAPIPermissionError("missing item logs")
        if self.api_error:
            raise self.api_error
        return []


def _build_interaction(user_id=12345):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=999,
        response=_FakeResponse(),
        followup=_FakeFollowup(),
    )


def _build_modal(value="A" * 16):
    modal = components.ApiKeyModal()
    modal.api_key._value = value
    return modal


class _FakeModalResponse:
    def __init__(self):
        self.modal = None

    async def send_modal(self, modal):
        self.modal = modal


def test_enter_api_key_button_opens_api_key_modal_successfully():
    async def _run():
        view = components.ApiKeyIntroView()
        button = next(
            child for child in view.children if getattr(child, "label", None) == "Enter API Key"
        )
        interaction = SimpleNamespace(response=_FakeModalResponse())

        await button.callback(interaction)

        assert isinstance(interaction.response.modal, components.ApiKeyModal)

    asyncio.run(_run())


def test_api_key_modal_placeholder_matches_expected_text_exactly():
    async def _run():
        modal = components.ApiKeyModal()

        assert modal.api_key.placeholder == "CUSTOM API KEY OR FULL ACCESS KEY ONLY"

    asyncio.run(_run())


def test_api_key_modal_placeholders_respect_discord_limits():
    async def _run():
        modal = components.ApiKeyModal()
        placeholders = [getattr(child, "placeholder", None) for child in modal.children]

        assert all(isinstance(placeholder, str) for placeholder in placeholders)
        assert all(len(placeholder) <= 100 for placeholder in placeholders)

    asyncio.run(_run())


def test_api_key_guide_embed_has_loud_public_key_warning():
    embed = create_api_key_guide_embed()

    assert "DO NOT USE A PUBLIC API KEY" in embed.description
    assert "USE THE BUTTON BELOW TO CREATE A CUSTOM SCOPED KEY" in embed.description
    assert "NO OTHER KEY WILL WORK" in embed.description
    assert "item log permissions (cat=85)" in embed.description


async def _run_modal_success(monkeypatch, *, torn_api):
    _FakeUsersRepo.instances.clear()
    _FakeAuditRepo.instances.clear()
    monkeypatch.setattr(components, "get_torn_api", lambda: torn_api)
    monkeypatch.setattr(components, "get_security_manager", lambda: _FakeSecurity())
    monkeypatch.setattr(components, "get_database", lambda: SimpleNamespace(pool=object()))
    monkeypatch.setattr(components, "UsersRepository", _FakeUsersRepo)
    monkeypatch.setattr(components, "AuditRepository", _FakeAuditRepo)

    interaction = _build_interaction()
    modal = _build_modal()
    await modal.on_submit(interaction)
    return interaction, torn_api


def test_key_missing_required_log_permissions_is_rejected_and_not_persisted(monkeypatch):
    async def _run():
        torn_api = _FakeTornAPI(permission_error=True)
        interaction, torn_api = await _run_modal_success(monkeypatch, torn_api=torn_api)

        assert interaction.response.deferred == [{"ephemeral": True, "thinking": False}]
        assert len(interaction.followup.messages) == 1
        message = interaction.followup.messages[0]
        assert message["embed"].title.endswith("Insufficient Permissions")
        assert (
            message["embed"].description
            == components.ApiKeyModal._missing_log_permissions_message()
        )
        assert isinstance(message["view"], components.ApiKeyIntroView)
        assert message["ephemeral"] is True
        assert [call[0] for call in torn_api.calls] == ["validate", "item_logs"]
        assert _FakeUsersRepo.instances == []

    asyncio.run(_run())


def test_public_key_is_rejected_via_same_permission_check_path(monkeypatch):
    async def _run():
        torn_api = _FakeTornAPI(permission_error=True)
        interaction, torn_api = await _run_modal_success(monkeypatch, torn_api=torn_api)

        assert [call[0] for call in torn_api.calls] == ["validate", "item_logs"]
        assert "required log permissions" in interaction.followup.messages[0]["embed"].description
        assert _FakeUsersRepo.instances == []

    asyncio.run(_run())


def test_valid_full_access_key_with_required_log_permissions_is_accepted(monkeypatch):
    async def _run():
        torn_api = _FakeTornAPI(validate_result=(12345, 888, "FullAccess", set()))
        interaction, torn_api = await _run_modal_success(monkeypatch, torn_api=torn_api)

        assert [call[0] for call in torn_api.calls] == ["validate", "item_logs"]
        assert _FakeUsersRepo.instances[0].upserts == [
            {
                "discord_id": 12345,
                "torn_user_id": 888,
                "torn_name": "FullAccess",
                "encrypted_key": f"enc:{'A' * 16}",
                "timezone_name": None,
            }
        ]
        assert interaction.followup.messages[0]["embed"].title.endswith("API Key Registered")

    asyncio.run(_run())


def test_valid_custom_scoped_key_with_required_log_permissions_is_accepted(monkeypatch):
    async def _run():
        torn_api = _FakeTornAPI(validate_result=(12345, 999, "ScopedKey", set()))
        interaction, _ = await _run_modal_success(monkeypatch, torn_api=torn_api)

        assert _FakeUsersRepo.instances[0].upserts[0]["torn_user_id"] == 999
        assert _FakeUsersRepo.instances[0].upserts[0]["torn_name"] == "ScopedKey"
        assert interaction.followup.messages[0]["embed"].title.endswith("API Key Registered")

    asyncio.run(_run())


def test_invalid_key_is_not_persisted(monkeypatch):
    async def _run():
        torn_api = _FakeTornAPI(api_error=TornAPIError("Invalid API key"))
        interaction, torn_api = await _run_modal_success(monkeypatch, torn_api=torn_api)

        assert [call[0] for call in torn_api.calls] == ["validate"]
        assert _FakeUsersRepo.instances == []
        assert interaction.followup.messages[0]["embed"].title.endswith("Validation Failed")

    asyncio.run(_run())


def test_valid_key_continues_normal_success_flow(monkeypatch):
    async def _run():
        torn_api = _FakeTornAPI(validate_result=(12345, 4321, "NormalFlow", set()))
        interaction, _ = await _run_modal_success(monkeypatch, torn_api=torn_api)

        assert len(_FakeAuditRepo.instances[0].audit_rows) == 1
        assert _FakeAuditRepo.instances[0].audit_rows[0]["action"] == "api_key_registered"
        assert interaction.followup.messages[0]["embed"].description == "Torn ID: `4321`"

    asyncio.run(_run())
