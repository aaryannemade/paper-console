"""Pytest suite for PC-1 core behaviors."""

import types
import importlib
from datetime import datetime
from unittest.mock import MagicMock

import app.main as main_module
importlib.import_module("app.modules")  # registers every module type
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


def test_settings_drop_removed_host_admin_fields():
    settings = Settings(time_sync_mode="automatic", release_channel="beta")
    dumped = settings.model_dump()
    assert "time_sync_mode" not in dumped
    assert "release_channel" not in dumped


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


def test_wifi_qr_payload_escapes_reserved_characters():
    payload = generate_wifi_qr_payload(ssid='Guest;WiFi:AB"CD', password='pa:ss;wo\\rd,1"')
    assert payload == r'WIFI:S:Guest\;WiFi\:AB\"CD;T:WPA;P:pa\:ss\;wo\\rd\,1\";;'


def test_wifi_qr_payload_marks_hidden_networks_only_when_needed():
    assert generate_wifi_qr_payload("Guest", "pass1234") == "WIFI:S:Guest;T:WPA;P:pass1234;;"
    assert generate_wifi_qr_payload("Guest", "pass1234", hidden=True) == "WIFI:S:Guest;T:WPA;P:pass1234;H:true;;"


def test_open_wifi_qr_does_not_include_password():
    assert generate_wifi_qr_payload("Guest", "unused", security="NOPASS", hidden=True) == "WIFI:S:Guest;T:nopass;H:true;;"


def test_user_wifi_qr_module_still_prints_credentials():
    printer = MagicMock()
    format_qrcode_receipt(printer, {
        "qr_type": "wifi", "wifi_ssid": "Guest", "wifi_password": "pass1234",
    })
    assert printer.print_qr.call_args.kwargs["data"] == "WIFI:S:Guest;T:WPA;P:pass1234;;"
    printer.print_bold.assert_any_call("Guest")


def test_execute_module_passes_instance_id_to_modules_that_accept_it(monkeypatch):
    from app.config import ModuleInstance

    captured = {}

    def fake_execute(printer, config, module_name, module_id=None, scheduled=False):  # noqa: ARG001
        captured["module_id"] = module_id
        captured["module_name"] = module_name
        captured["config"] = config
        captured["scheduled"] = scheduled

    monkeypatch.setattr(
        main_module,
        "get_module_def",
        lambda module_type: types.SimpleNamespace(execute_fn=fake_execute),
    )

    module = ModuleInstance(
        id="adventure-one",
        type="adventure",
        name="Cave Run",
        config={"reset_game": False},
    )

    assert main_module.execute_module(module, scheduled=True) is True
    assert captured == {
        "module_id": "adventure-one",
        "module_name": "Cave Run",
        "config": {"reset_game": False},
        "scheduled": True,
    }
