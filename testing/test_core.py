"""Pytest suite for PC-1 core behaviors."""

from datetime import datetime
from unittest.mock import MagicMock

import app.device_password as device_password
import app.modules  # noqa: F401 - triggers module auto-registration
from app.config import Settings, format_print_datetime
from app.module_registry import get_all_modules, validate_module_config
from app.modules.qrcode_print import format_qrcode_receipt
from app.qr_payloads import generate_wifi_qr_payload


def test_default_settings_have_eight_channels():
    settings = Settings()
    assert sorted(settings.channels.keys()) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_default_settings_exclude_removed_module_types():
    settings = Settings()
    assert all(
        module.type not in {"ai", "settings_menu"}
        for module in settings.modules.values()
    )


def test_default_settings_use_stable_release_channel():
    settings = Settings()
    assert settings.release_channel == "stable"


def test_print_datetime_includes_configured_time_format():
    dt = datetime(2026, 4, 21, 16, 5)
    assert format_print_datetime(dt, time_format="12h") == "Tuesday, April 21, 2026 4:05 PM"
    assert format_print_datetime(dt, time_format="24h") == "Tuesday, April 21, 2026 16:05"


def test_default_module_configs_are_registered_and_valid():
    settings = Settings()
    registry = get_all_modules()
    for module in settings.modules.values():
        assert module.type in registry
        validate_module_config(module.type, module.config or {})


def test_registry_excludes_removed_modules():
    registry = get_all_modules()
    assert "settings_menu" not in registry
    assert "ai" not in registry


def test_password_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PC1_DEVICE_PASSWORD_FILE", str(tmp_path / "device_password"))
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "my-strong-password")
    assert device_password.get_device_password() == "my-strong-password"


def test_password_fallback_uses_device_hash(monkeypatch, tmp_path):
    monkeypatch.delenv("PC1_DEVICE_PASSWORD", raising=False)
    monkeypatch.setenv("PC1_DEVICE_PASSWORD_FILE", str(tmp_path / "device_password"))
    monkeypatch.setattr(device_password, "get_device_password_seed", lambda: "seed-value")
    expected = device_password.derive_device_password_from_seed("seed-value")
    assert device_password.get_device_password() == expected


def test_wifi_qr_payload_escapes_reserved_characters():
    payload = generate_wifi_qr_payload(ssid='Guest;WiFi:AB"CD', password='pa:ss;wo\\rd,1"')
    assert payload == r'WIFI:S:Guest\;WiFi\:AB\"CD;T:WPA;P:pa\:ss\;wo\\rd\,1\";;'


def test_wifi_qr_payload_marks_hidden_networks_only_when_needed():
    assert generate_wifi_qr_payload("Guest", "pass1234") == "WIFI:S:Guest;T:WPA;P:pass1234;;"
    assert generate_wifi_qr_payload("Guest", "pass1234", hidden=True) == "WIFI:S:Guest;T:WPA;P:pass1234;H:true;;"


def test_open_wifi_qr_does_not_include_password():
    assert generate_wifi_qr_payload("Guest", "unused", security="NOPASS", hidden=True) == "WIFI:S:Guest;T:nopass;H:true;;"


def test_primary_ip_ignores_loopback(monkeypatch):
    import app.utils as utils

    class FakeSocket:
        def __init__(self, address):
            self.address = address

        def connect(self, _target):
            return None

        def getsockname(self):
            return (self.address, 0)

        def close(self):
            return None

    monkeypatch.setattr(utils.socket, "socket", lambda *a: FakeSocket("127.0.1.1"))
    assert utils.get_primary_ip() is None
    monkeypatch.setattr(utils.socket, "socket", lambda *a: FakeSocket("192.168.1.20"))
    assert utils.get_primary_ip() == "192.168.1.20"


def test_user_wifi_qr_module_still_prints_credentials():
    printer = MagicMock()
    format_qrcode_receipt(printer, {
        "qr_type": "wifi", "wifi_ssid": "Guest", "wifi_password": "pass1234",
    })
    assert printer.print_qr.call_args.kwargs["data"] == "WIFI:S:Guest;T:WPA;P:pass1234;;"
    printer.print_bold.assert_any_call("Guest")
