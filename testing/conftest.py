"""Test-wide isolation, applied before any app module is imported.

Every test gets an empty PC1_DATA_DIR so nothing reads or writes the
developer's config.json, generated password or Adventure saves (and nothing
touches /data when the suite runs inside the container).
"""

import os
import tempfile

import pytest

# Server settings that a developer shell, .env or the container may export.
# Tests that need one set it explicitly with monkeypatch.
_ISOLATED_ENV = (
    "PC1_DATA_DIR",
    "PC1_DEVICE_PASSWORD",
    "PC1_DEVICE_PASSWORD_FILE",
    "PC1_GENERATED_PASSWORD_FILE",
    "PC1_SESSION_SECRET",
    "PC1_CORS_ORIGINS",
    "PC1_RELAY_TOKEN",
    "PC1_RELAY_TOKEN_FILE",
    "PC1_RELAY_URL",
    "PC1_PRINT_CAPTURE_DIR",
    "PC1_PRINTER_PORT",
    "PC1_PRINTER_BAUDRATE",
    "PC1_PRINTER_USB_VENDOR_ID",
    "PC1_PRINTER_USB_PRODUCT_ID",
)
for _name in _ISOLATED_ENV:
    os.environ.pop(_name, None)

# The production default is the ESP32 relay, which is unreachable in tests.
# Tests that exercise other outputs construct them explicitly.
os.environ["PC1_PRINTER_OUTPUT"] = "console"
# Settings are loaded at import time; give that load an empty directory too.
_startup_data = tempfile.TemporaryDirectory(prefix="pc1-test-data-")
os.environ["PC1_DATA_DIR"] = _startup_data.name


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """A fresh, empty application data directory for each test."""
    data = tmp_path / "pc1-data"
    data.mkdir()
    monkeypatch.setenv("PC1_DATA_DIR", str(data))
    return data


@pytest.fixture(autouse=True)
def reset_selection_mode():
    """Never leak an open menu from one test into the next."""
    from app import selection_mode

    selection_mode.exit_selection_mode()
    yield
    selection_mode.exit_selection_mode()


@pytest.fixture(autouse=True)
def reset_process_local_state(monkeypatch):
    """No test inherits a previous test's print reservation or relay session."""
    from app import hardware, relay_link, device_input

    monkeypatch.setattr(hardware, "print_in_progress", False)
    monkeypatch.setattr(hardware, "hold_action_in_progress", False)
    monkeypatch.setattr(hardware, "hold_action_started_at", 0.0)
    monkeypatch.setattr(hardware, "last_print_time", 0.0)
    relay_link.link.reset()
    device_input.processor.reset()
    yield
    relay_link.link.reset()
    device_input.processor.reset()
