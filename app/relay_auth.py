"""Shared bearer token between the container and the ESP32 relay.

One device, one token, used in both directions: the ESP32 sends it on input
events (and, from Step 3c, heartbeats and job status), and the container sends it
on print jobs. It is separate from the settings password.

Resolution: ``PC1_RELAY_TOKEN``, else the file named by ``PC1_RELAY_TOKEN_FILE``
(e.g. a Docker secret). If neither is set, the device endpoints are disabled.
A configured token that is too short or unreadable fails startup.
"""

import hmac
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

TOKEN_ENV = "PC1_RELAY_TOKEN"
TOKEN_FILE_ENV = "PC1_RELAY_TOKEN_FILE"
MIN_TOKEN_LENGTH = 16


class RelayTokenError(RuntimeError):
    """The relay token is configured but unusable."""


def _validate(token: str, source: str) -> str:
    if len(token) < MIN_TOKEN_LENGTH:
        raise RelayTokenError(
            f"{source} must be at least {MIN_TOKEN_LENGTH} characters"
        )
    if any(ch.isspace() for ch in token):
        raise RelayTokenError(f"{source} must not contain whitespace")
    return token


def get_relay_token() -> Optional[str]:
    """Return the configured token, or None when the relay is not configured."""
    value = os.environ.get(TOKEN_ENV, "").strip()
    if value:
        return _validate(value, TOKEN_ENV)

    path = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if not path:
        return None
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RelayTokenError(f"{TOKEN_FILE_ENV} could not be read: {exc}") from exc
    if not value:
        raise RelayTokenError(f"{TOKEN_FILE_ENV} is empty")
    return _validate(value, TOKEN_FILE_ENV)


def relay_token_source() -> str:
    if os.environ.get(TOKEN_ENV, "").strip():
        return "env"
    if os.environ.get(TOKEN_FILE_ENV, "").strip():
        return "file"
    return "unset"


def token_matches(presented: Optional[str], expected: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def require_relay_token(request: Request) -> None:
    """FastAPI dependency for endpoints called by the ESP32."""
    try:
        expected = get_relay_token()
    except RelayTokenError:
        logger.error("Relay token misconfigured", exc_info=True)
        raise HTTPException(status_code=503, detail="Device relay token is misconfigured")
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail=f"Device relay is not configured (set {TOKEN_ENV})",
        )
    if not token_matches(bearer_token(request), expected):
        raise HTTPException(
            status_code=401,
            detail="Device token required or invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )
