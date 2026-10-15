"""Shared authentication helpers for privileged API endpoints."""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Dict, Optional

from fastapi import HTTPException, Request, Response

import app.device_password as device_password

AUTH_REQUIRED_HEADER = "X-PC1-Auth-Required"
TOKEN_HEADER = "X-PC1-Device-Password"
SESSION_COOKIE_NAME = "pc1_admin_session"
REMEMBER_DURATION_SECONDS = 365 * 24 * 60 * 60
SESSION_DURATION_SECONDS = 12 * 60 * 60


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _urlsafe_b64decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(f"{raw}{padding}".encode("ascii"))


def _current_admin_password() -> str:
    return device_password.get_device_password()


def _session_secret() -> bytes:
    # Key cookies with an operator secret, or the password itself. Never use host
    # identity (hostname/machine-id), which is guessable for a networked container.
    seed = os.environ.get("PC1_SESSION_SECRET", "").strip() or _current_admin_password()
    return hashlib.sha256(f"pc1-session::{seed}".encode("utf-8")).digest()


def _secrets_match(candidate: str, expected: str) -> bool:
    # compare_digest() rejects non-ASCII str, so compare UTF-8 bytes instead.
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _password_fingerprint() -> str:
    return hashlib.sha256(_current_admin_password().encode("utf-8")).hexdigest()


def _sign_session_payload(payload_segment: str) -> str:
    signature = hmac.new(
        _session_secret(),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _urlsafe_b64encode(signature)


def _build_session_cookie_value(remember: bool) -> str:
    now = int(time.time())
    lifetime = REMEMBER_DURATION_SECONDS if remember else SESSION_DURATION_SECONDS
    payload = {
        "v": 1,
        "iat": now,
        "exp": now + lifetime,
        "pwd": _password_fingerprint(),
    }
    payload_segment = _urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signature_segment = _sign_session_payload(payload_segment)
    return f"{payload_segment}.{signature_segment}"


def _read_session_payload(cookie_value: str) -> Optional[Dict[str, object]]:
    try:
        payload_segment, signature_segment = cookie_value.split(".", 1)
    except ValueError:
        return None

    expected_signature = _sign_session_payload(payload_segment)
    if not hmac.compare_digest(signature_segment, expected_signature):
        return None

    try:
        payload = json.loads(_urlsafe_b64decode(payload_segment).decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    expires_at = int(payload.get("exp", 0))
    if expires_at <= int(time.time()):
        return None

    if payload.get("pwd") != _password_fingerprint():
        return None

    return payload


def is_admin_authenticated(request: Request) -> bool:
    provided_token = request.headers.get(TOKEN_HEADER, "").strip()
    if provided_token and _secrets_match(provided_token, _current_admin_password()):
        return True

    session_cookie = request.cookies.get(SESSION_COOKIE_NAME, "").strip()
    if session_cookie and _read_session_payload(session_cookie):
        return True

    return False


def verify_admin_password(password: str) -> bool:
    candidate = (password or "").strip()
    if not candidate:
        return False
    return _secrets_match(candidate, _current_admin_password())


def set_admin_session_cookie(
    response: Response,
    remember: bool,
    *,
    secure: bool = False,
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_build_session_cookie_value(remember=remember),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
        max_age=REMEMBER_DURATION_SECONDS if remember else None,
    )


def clear_admin_session_cookie(response: Response, *, secure: bool = False) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def require_admin_access(request: Request):
    """Protect privileged endpoints with header or signed-session auth."""
    if is_admin_authenticated(request):
        return

    raise HTTPException(
        status_code=401,
        detail="Server password required or invalid.",
        headers={AUTH_REQUIRED_HEADER: "true"},
    )


def get_admin_auth_status(request: Optional[Request] = None) -> Dict[str, object]:
    """Return privileged endpoint auth mode without exposing secrets."""
    return {
        "login_required": True,
        "authenticated": is_admin_authenticated(request) if request else False,
        "auth_mode": "device_password",
        "password_label": "Server Password",
        "message": "Enter the server password to access settings.",
        "password_help": (
            "Set with PC1_DEVICE_PASSWORD or PC1_DEVICE_PASSWORD_FILE. If neither is "
            "set, a generated password is printed in the server log at startup. "
            "This browser can remember your login with a signed session cookie."
        ),
        "session_supported": True,
        "remember_supported": True,
        "device_password_source": device_password.get_device_password_source(),
    }
