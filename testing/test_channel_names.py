"""Channel display names: model normalization, API, persistence and receipt."""

import asyncio
import copy

import pytest
from fastapi import BackgroundTasks, HTTPException

import app.config as config
import app.main as main
from app.config import (
    CHANNEL_NAME_MAX_LENGTH,
    ChannelConfig,
    ChannelModuleAssignment,
    ModuleInstance,
    Settings,
    load_config,
    normalize_channel_name,
    save_config,
)
from testing.route_utils import route_dependencies


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("PC1_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main, "settings", copy.deepcopy(main.settings))
    return main.settings


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ""),
        ("", ""),
        ("  Work   update  ", "Work update"),
        ("Line\nbreak\ttab", "Line break tab"),
        ("x" * 40, "x" * CHANNEL_NAME_MAX_LENGTH),
        ("Café ☕", "Café ☕"),
    ],
)
def test_normalize_channel_name(raw, expected):
    assert normalize_channel_name(raw) == expected


def test_old_configs_load_unnamed_and_names_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("PC1_DATA_DIR", str(tmp_path))
    assert ChannelConfig(modules=[]).name == ""
    assert ChannelConfig.model_validate({"modules": [], "name": "  Morning  "}).name == "Morning"
    with pytest.raises(ValueError):
        ChannelConfig.model_validate({"name": 5})

    settings = Settings()
    settings.channels[1].name = "Work update"
    save_config(settings)
    assert load_config().channels[1].name == "Work update"


def test_rename_endpoint_sets_clears_and_persists(isolated_settings):
    tasks = BackgroundTasks()
    result = asyncio.run(
        main.update_channel_name(2, main.ChannelNameUpdate(name="  Work\nupdate "), tasks)
    )
    assert result == {"message": "Channel name updated", "position": 2, "name": "Work update"}
    assert main.settings.channels[2].name == "Work update"
    asyncio.run(tasks())  # background save
    assert config.load_config().channels[2].name == "Work update"

    asyncio.run(main.update_channel_name(2, main.ChannelNameUpdate(name=""), BackgroundTasks()))
    assert main.settings.channels[2].name == ""


def test_rename_creates_missing_channel_and_rejects_bad_position(isolated_settings):
    main.settings.channels.pop(7, None)
    asyncio.run(main.update_channel_name(7, main.ChannelNameUpdate(name="Night"), BackgroundTasks()))
    assert main.settings.channels[7].name == "Night"
    assert main.settings.channels[7].modules == []
    for position in (0, 9):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                main.update_channel_name(position, main.ChannelNameUpdate(name="x"), BackgroundTasks())
            )
        assert exc.value.status_code == 400


def test_full_settings_save_keeps_names(isolated_settings):
    payload = main.settings.model_dump()
    payload["channels"][1]["name"] = "Swapped in"
    response = asyncio.run(main.update_settings(Settings(**payload), BackgroundTasks()))
    assert response["config"].channels[1].name == "Swapped in"


def test_rename_route_requires_admin():
    deps = route_dependencies(main.app, "/api/channels/{position}/name")
    assert main.require_admin_access in deps


def test_device_channels_hash_tracks_name_changes(isolated_settings, monkeypatch):
    import app.relay_link as relay_link

    monkeypatch.setattr(config, "settings", main.settings)
    first = relay_link.channel_names_hash()
    body = relay_link.get_device_channels()
    assert body["hash"] == first
    assert [c["position"] for c in body["channels"]] == list(range(1, 9))

    main.settings.channels[3].name = "Work update"
    second = relay_link.channel_names_hash()
    assert second != first and len(second) == 16
    assert relay_link.get_device_channels()["channels"][2] == {"position": 3, "name": "Work update"}
    # Module/schedule changes do not affect the hash; only names do.
    main.settings.channels[3].schedule_rules = []
    main.settings.channels[3].modules = []
    assert relay_link.channel_names_hash() == second
    # A missing channel counts as unnamed.
    main.settings.channels.pop(8, None)
    assert relay_link.channel_names()[7] == ""


def test_device_channel_routes_use_relay_token():
    import app.relay_auth as relay_auth

    for path in ("/api/device/channels", "/api/device/heartbeat"):
        assert relay_auth.require_relay_token in route_dependencies(main.app, path)


def test_channel_overview_shows_names(isolated_settings, monkeypatch):
    lines = []

    class Recorder:
        def print_header(self, text, **kwargs):
            lines.append(("header", text))

        def print_body(self, text, **kwargs):
            lines.append(("body", text))

        def print_caption(self, text, **kwargs):
            lines.append(("caption", text))

    monkeypatch.setattr(main, "printer", Recorder())
    main.settings.modules = {"m1": ModuleInstance(id="m1", type="text", name="Todo")}
    main.settings.channels = {
        1: ChannelConfig(name="Work update", modules=[ChannelModuleAssignment(module_id="m1")]),
        2: ChannelConfig(modules=[ChannelModuleAssignment(module_id="m1")]),
        3: ChannelConfig(name="Spare"),
    }
    main._write_channel_overview_compact()

    assert ("body", "1 Work update") in lines
    assert ("caption", "  Todo") in lines
    assert ("body", "2: Todo") in lines
    assert ("body", "3 Spare") in lines
    assert ("caption", "  (empty)") in lines
    assert ("caption", "4: (empty)") in lines


def test_heartbeat_clock_follows_timezone_and_format(isolated_settings, monkeypatch):
    import app.relay_link as relay_link

    monkeypatch.setattr(config, "settings", main.settings)
    main.settings.timezone = "Asia/Kolkata"
    main.settings.time_format = "24h"
    assert relay_link.device_clock() == {"utc_offset_s": 19800, "time_format": "24h"}
    main.settings.timezone = "UTC"
    main.settings.time_format = "12h"
    assert relay_link.device_clock() == {"utc_offset_s": 0, "time_format": "12h"}
    # Invalid zone (bypassing validation) falls back to UTC instead of failing.
    object.__setattr__(main.settings, "timezone", "Not/AZone")
    assert relay_link.device_clock()["utc_offset_s"] == 0
