"""Tests for settings authentication and session handling."""

from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

import app.auth as auth
import app.device_password as device_password


def _request_with_cookie(cookie_name: str, cookie_value: str, *, origin: str | None = None):
    headers = []
    if cookie_name and cookie_value:
        headers.append((b"cookie", f"{cookie_name}={cookie_value}".encode("utf-8")))
    if origin:
        headers.append((b"origin", origin.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
        }
    )


def _extract_cookie_value(set_cookie_header: str, cookie_name: str) -> str:
    jar = SimpleCookie()
    jar.load(set_cookie_header)
    return jar[cookie_name].value


def test_verify_admin_password_uses_device_password(monkeypatch):
    monkeypatch.setattr(device_password, "get_device_password", lambda: "pc1-test-password")

    assert auth.verify_admin_password("pc1-test-password") is True
    assert auth.verify_admin_password("wrong-password") is False


def test_signed_session_cookie_authenticates_request(monkeypatch):
    monkeypatch.setattr(device_password, "get_device_password", lambda: "pc1-test-password")
    monkeypatch.delenv("PC1_SESSION_SECRET", raising=False)

    response = Response()
    auth.set_admin_session_cookie(response, remember=True, secure=False)
    cookie_value = _extract_cookie_value(
        response.headers["set-cookie"],
        auth.SESSION_COOKIE_NAME,
    )

    request = _request_with_cookie(auth.SESSION_COOKIE_NAME, cookie_value)
    assert auth.is_admin_authenticated(request) is True


def test_signed_session_cookie_invalidates_when_password_changes(monkeypatch):
    monkeypatch.setattr(device_password, "get_device_password", lambda: "pc1-test-password")
    monkeypatch.delenv("PC1_SESSION_SECRET", raising=False)

    response = Response()
    auth.set_admin_session_cookie(response, remember=True, secure=False)
    cookie_value = _extract_cookie_value(
        response.headers["set-cookie"],
        auth.SESSION_COOKIE_NAME,
    )

    monkeypatch.setattr(device_password, "get_device_password", lambda: "pc1-other-password")
    request = _request_with_cookie(auth.SESSION_COOKIE_NAME, cookie_value)
    assert auth.is_admin_authenticated(request) is False


@pytest.mark.parametrize("origin", ["http://localhost", "https://example.org"])
def test_require_admin_access_requires_login_from_any_origin(origin):
    request = _request_with_cookie("", "", origin=origin)

    with pytest.raises(HTTPException) as error:
        auth.require_admin_access(request)
    assert error.value.status_code == 401
    assert error.value.headers[auth.AUTH_REQUIRED_HEADER] == "true"


def test_auth_status_reports_password_source_without_secrets(monkeypatch):
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "status-test-password")

    status = auth.get_admin_auth_status()

    assert status["auth_mode"] == "device_password"
    assert status["login_required"] is True
    assert status["device_password_source"] == "env"
    assert "device_password_managed" not in status
    assert "status-test-password" not in repr(status)


def test_session_cookie_cannot_be_forged_from_host_identity(monkeypatch):
    """Cookies are keyed by the password (or PC1_SESSION_SECRET), not hostname."""
    import hashlib
    import hmac as hmac_module
    import json
    import time as time_module

    monkeypatch.delenv("PC1_SESSION_SECRET", raising=False)
    monkeypatch.setattr(device_password, "get_device_password", lambda: "real-password-123")
    payload = {
        "v": 1,
        "iat": int(time_module.time()),
        "exp": int(time_module.time()) + 3600,
        "pwd": hashlib.sha256(b"real-password-123").hexdigest(),
    }
    segment = auth._urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    for guessed_seed in ("localhost", "pc1", "container-hostname"):
        key = hashlib.sha256(f"pc1-session::{guessed_seed}".encode()).digest()
        signature = auth._urlsafe_b64encode(
            hmac_module.new(key, segment.encode(), hashlib.sha256).digest()
        )
        request = _request_with_cookie(auth.SESSION_COOKIE_NAME, f"{segment}.{signature}")
        assert auth.is_admin_authenticated(request) is False


def test_explicit_session_secret_keeps_cookies_valid(monkeypatch):
    monkeypatch.setenv("PC1_SESSION_SECRET", "operator-session-secret")
    monkeypatch.setattr(device_password, "get_device_password", lambda: "pc1-test-password")
    response = Response()
    auth.set_admin_session_cookie(response, remember=False)
    cookie_value = _extract_cookie_value(response.headers["set-cookie"], auth.SESSION_COOKIE_NAME)

    assert auth.is_admin_authenticated(_request_with_cookie(auth.SESSION_COOKIE_NAME, cookie_value))

    monkeypatch.setenv("PC1_SESSION_SECRET", "rotated-secret")
    assert not auth.is_admin_authenticated(_request_with_cookie(auth.SESSION_COOKIE_NAME, cookie_value))


def test_non_ascii_password_is_compared_safely(monkeypatch):
    monkeypatch.setattr(device_password, "get_device_password", lambda: "pässwörd-ünïcode")

    assert auth.verify_admin_password("pässwörd-ünïcode") is True
    assert auth.verify_admin_password("passwort-unicode") is False
    assert auth.verify_admin_password("ä") is False


def test_password_header_authenticates_api_request(monkeypatch):
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "server-test-password")
    request = _request_with_cookie("", "")
    request.scope["headers"].append(
        (auth.TOKEN_HEADER.lower().encode(), b"server-test-password")
    )

    assert auth.require_admin_access(request) is None
    status = auth.get_admin_auth_status(request)

    assert status["auth_mode"] == "device_password"
    assert status["login_required"] is True
    assert status["authenticated"] is True
