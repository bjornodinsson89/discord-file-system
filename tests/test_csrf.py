import asyncio

import pytest
from fastapi import HTTPException

from web.csrf import enforce_csrf, generate_csrf_token


class _DummyRequest:
    def __init__(self, method: str, path: str, *, session=None, headers=None, cookies=None):
        self.method = method
        self.url = type("URL", (), {"path": path})()
        self.session = session or {}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.app = type("DummyApp", (), {"state": type("State", (), {"csrf_hmac_secret": "test-secret"})()})()


def test_unauthenticated_unsafe_api_request_is_not_csrf_blocked():
    request = _DummyRequest("POST", "/api/sessions/create", session={})
    asyncio.run(enforce_csrf(request))


def test_authenticated_with_valid_cookie_and_header_passes():
    token = generate_csrf_token("test-secret")
    request = _DummyRequest(
        "POST",
        "/api/sessions/create",
        session={"user": {"id": "123"}},
        headers={"x-csrf-token": token},
        cookies={"csrf_token": token},
    )
    asyncio.run(enforce_csrf(request))


def test_missing_header_fails_with_reason():
    token = generate_csrf_token("test-secret")
    request = _DummyRequest(
        "POST",
        "/api/sessions/create",
        session={"user": {"id": "123"}},
        cookies={"csrf_token": token},
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(enforce_csrf(request))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["reason"] == "missing_header"


def test_mismatch_fails_with_reason():
    token = generate_csrf_token("test-secret")
    request = _DummyRequest(
        "POST",
        "/api/sessions/create",
        session={"user": {"id": "123"}},
        headers={"x-csrf-token": token},
        cookies={"csrf_token": generate_csrf_token("test-secret")},
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(enforce_csrf(request))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["reason"] == "mismatch"


def test_bad_signature_fails_with_reason():
    token = generate_csrf_token("wrong-secret")
    request = _DummyRequest(
        "POST",
        "/api/sessions/create",
        session={"user": {"id": "123"}},
        headers={"x-csrf-token": token},
        cookies={"csrf_token": token},
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(enforce_csrf(request))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["reason"] == "bad_signature"
