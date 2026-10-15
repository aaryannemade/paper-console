"""Server password for settings login and the X-PC1-Device-Password API header.

Resolution order:
1. ``PC1_DEVICE_PASSWORD`` environment variable.
2. ``PC1_DEVICE_PASSWORD_FILE``: a file containing the password (for example a
   Docker secret mounted at ``/run/secrets/...``).
3. A random password generated on first use and stored in
   ``PC1_GENERATED_PASSWORD_FILE`` (default: ``.device_password`` next to
   ``config.json``) with owner-only permissions. It is logged once so the operator
   can log in, and it survives restarts.

The password is an application credential only. It is never derived from host
identity and never synchronized to OS accounts.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

DEVICE_PASSWORD_ENV = "PC1_DEVICE_PASSWORD"
DEVICE_PASSWORD_FILE_ENV = "PC1_DEVICE_PASSWORD_FILE"
GENERATED_PASSWORD_FILE_ENV = "PC1_GENERATED_PASSWORD_FILE"
MIN_DEVICE_PASSWORD_LENGTH = 8

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_announced_generated_password = False


def _read_password_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_generated_password_path() -> Path:
    configured = os.environ.get(GENERATED_PASSWORD_FILE_ENV, "").strip()
    return Path(configured) if configured else _PROJECT_ROOT / ".device_password"


def _configured_password() -> tuple[str, str]:
    """Return (password, source) for operator-provided passwords, or ("", "")."""
    env_password = os.environ.get(DEVICE_PASSWORD_ENV, "").strip()
    if env_password:
        if len(env_password) < MIN_DEVICE_PASSWORD_LENGTH:
            raise ValueError(
                f"{DEVICE_PASSWORD_ENV} must be at least "
                f"{MIN_DEVICE_PASSWORD_LENGTH} characters long"
            )
        return env_password, "env"

    file_setting = os.environ.get(DEVICE_PASSWORD_FILE_ENV, "").strip()
    if file_setting:
        file_password = _read_password_file(Path(file_setting))
        if len(file_password) < MIN_DEVICE_PASSWORD_LENGTH:
            raise ValueError(
                f"{DEVICE_PASSWORD_FILE_ENV} must point to a readable file containing "
                f"at least {MIN_DEVICE_PASSWORD_LENGTH} characters"
            )
        return file_password, "file"

    return "", ""


def _load_or_create_generated_password() -> str:
    global _announced_generated_password
    path = get_generated_password_path()

    password = _read_password_file(path)
    if len(password) < MIN_DEVICE_PASSWORD_LENGTH:
        candidate = secrets.token_urlsafe(18)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL: if two workers race, exactly one creates the file and the
            # other reads the winner's value below.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"{candidate}\n")
        except FileExistsError:
            pass
        password = _read_password_file(path)
        if len(password) < MIN_DEVICE_PASSWORD_LENGTH:
            raise RuntimeError(f"Could not create a server password at {path}")

    if not _announced_generated_password:
        _announced_generated_password = True
        logger.warning(
            "No %s or %s set. Using the generated settings password stored in %s: %s",
            DEVICE_PASSWORD_ENV,
            DEVICE_PASSWORD_FILE_ENV,
            path,
            password,
        )
    return password


def get_device_password() -> str:
    password, _source = _configured_password()
    return password or _load_or_create_generated_password()


def get_device_password_source() -> str:
    """Return "env", "file" or "generated" (never the password itself)."""
    _password, source = _configured_password()
    return source or "generated"
