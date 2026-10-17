"""Device input events from the ESP32 relay (migration Step 3b)."""

import asyncio
import time

import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError

import app.device_input as device_input
import app.hardware as hardware
import app.main as main
import app.relay_auth as relay_auth
import app.selection_mode as selection_mode
from app.device_input import ChannelSelector, DeviceInputEvent, InputProcessor
from app.drivers.esp32_relay_output import Esp32RelayOutput
from app.drivers.printer_raster import RasterPrinter
from app.relay_link import DeviceLink
from testing.route_utils import find_route, route_dependencies

TOKEN = "relay-token-0123456789"


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/api/device/events", "headers": raw})


def _button(gesture, press_id=1, event_id=None, position=None):
    return DeviceInputEvent(
        event_id=event_id or f"{gesture}-{press_id}-{time.monotonic_ns()}",
        type="button",
        gesture=gesture,
        press_id=press_id,
        position=position,
    )


# --- relay token ---------------------------------------------------------------


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv(relay_auth.TOKEN_ENV, raising=False)
    monkeypatch.delenv(relay_auth.TOKEN_FILE_ENV, raising=False)


def test_device_api_is_disabled_without_token(no_token):
    assert relay_auth.get_relay_token() is None
    with pytest.raises(HTTPException) as exc:
        relay_auth.require_relay_token(_request({"Authorization": f"Bearer {TOKEN}"}))
    assert exc.value.status_code == 503


def test_correct_bearer_token_is_accepted(no_token, monkeypatch):
    monkeypatch.setenv(relay_auth.TOKEN_ENV, TOKEN)
    relay_auth.require_relay_token(_request({"Authorization": f"Bearer {TOKEN}"}))
    relay_auth.require_relay_token(_request({"Authorization": f"bearer {TOKEN}"}))


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-0123456789"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
        # The settings password header is not a device credential.
        {"X-PC1-Device-Password": TOKEN},
    ],
)
def test_missing_or_wrong_token_is_rejected(no_token, monkeypatch, headers):
    monkeypatch.setenv(relay_auth.TOKEN_ENV, TOKEN)
    with pytest.raises(HTTPException) as exc:
        relay_auth.require_relay_token(_request(headers))
    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


def test_settings_password_is_not_accepted_as_token(no_token, monkeypatch):
    monkeypatch.setenv(relay_auth.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "settings-password-123")
    with pytest.raises(HTTPException):
        relay_auth.require_relay_token(_request({"Authorization": "Bearer settings-password-123"}))


@pytest.mark.parametrize("bad", ["short", "has whitespace inside!"])
def test_unusable_token_fails_loudly(no_token, monkeypatch, bad):
    monkeypatch.setenv(relay_auth.TOKEN_ENV, bad)
    with pytest.raises(relay_auth.RelayTokenError):
        relay_auth.get_relay_token()


def test_token_file_is_supported(no_token, monkeypatch, tmp_path):
    secret = tmp_path / "relay_token"
    secret.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv(relay_auth.TOKEN_FILE_ENV, str(secret))
    assert relay_auth.get_relay_token() == TOKEN
    assert relay_auth.relay_token_source() == "file"

    monkeypatch.setenv(relay_auth.TOKEN_FILE_ENV, str(tmp_path / "missing"))
    with pytest.raises(relay_auth.RelayTokenError):
        relay_auth.get_relay_token()


def test_device_route_is_registered_with_token_dependency():
    route = find_route(main.app, "/api/device/events")
    assert route.methods == {"POST"}
    deps = route_dependencies(main.app, "/api/device/events")
    assert relay_auth.require_relay_token in deps
    assert main.require_admin_access not in deps


# --- event validation ----------------------------------------------------------------


def test_event_shapes_are_validated():
    assert DeviceInputEvent(event_id="a", type="position", position=3).position == 3
    event = DeviceInputEvent(event_id="b", type="button", gesture="press", press_id=7, extra="x")
    assert event.press_id == "7"

    for bad in (
        {"event_id": "c", "type": "position"},
        {"event_id": "c", "type": "position", "position": 9},
        {"event_id": "c", "type": "position", "position": 0},
        {"event_id": "c", "type": "position", "position": 2, "gesture": "press"},
        {"event_id": "c", "type": "button", "gesture": "press"},
        {"event_id": "c", "type": "button", "press_id": 1},
        {"event_id": "c", "type": "button", "gesture": "double", "press_id": 1},
        {"event_id": "c", "type": "button", "gesture": "long_hold", "press_id": 1},
        {"event_id": "", "type": "position", "position": 1},
        {"event_id": "x" * 65, "type": "position", "position": 1},
        {"event_id": "c", "type": "button", "gesture": "press", "press_id": True},
    ):
        with pytest.raises(ValidationError):
            DeviceInputEvent(**bad)


# --- processor -----------------------------------------------------------------------


@pytest.fixture
def processor():
    calls = []
    results = {"press": "printing_channel", "hold": "hold_ready",
               "hold_release": "quick_actions"}
    proc = InputProcessor()
    selector = ChannelSelector()

    def make(gesture):
        def handler():
            calls.append((gesture, selector.read_position()))
            return results[gesture]
        return handler

    proc.configure(selector, {g: make(g) for g in device_input.GESTURES})
    proc.calls, proc.results, proc.sel = calls, results, selector
    return proc


def test_position_event_updates_server_side_position(processor):
    response = processor.process(DeviceInputEvent(event_id="p1", type="position", position=6))
    assert response == {"event_id": "p1", "result": "position_set", "position": 6, "duplicate": False}
    assert processor.sel.read_position() == 6
    assert processor.calls == []


def test_button_position_is_applied_before_the_gesture(processor):
    processor.process(_button("press", position=4))
    assert processor.calls == [("press", 4)]


def test_retried_event_is_not_applied_twice(processor):
    event = _button("press", event_id="retry-1")
    first = processor.process(event)
    second = processor.process(event)
    assert processor.calls == [("press", 1)]
    assert second == {**first, "duplicate": True}


def test_recent_event_memory_is_bounded(processor):
    for i in range(InputProcessor.RECENT_EVENT_LIMIT + 10):
        processor.process(DeviceInputEvent(event_id=f"p{i}", type="position", position=1))
    assert len(processor._recent) == InputProcessor.RECENT_EVENT_LIMIT
    assert processor.process(DeviceInputEvent(event_id="p0", type="position", position=2))[
        "duplicate"
    ] is False


def test_hold_that_cancels_consumes_the_rest_of_the_press(processor):
    processor.results["hold"] = "cancel_requested"
    assert processor.process(_button("hold", press_id=5))["result"] == "cancel_requested"
    assert processor.process(_button("hold_release", press_id=5))["result"] == "ignored_consumed"
    # The next physical press works normally.
    assert processor.process(_button("hold_release", press_id=6))["result"] == "quick_actions"
    assert [c[0] for c in processor.calls] == ["hold", "hold_release"]


def test_hold_cancel_consumes_the_press_without_quick_actions(processor):
    assert processor.process(_button("hold", press_id=7))["result"] == "hold_ready"
    # No handler configured: still consumed.
    assert processor.process(_button("hold_cancel", press_id=7))["result"] == "hold_cancelled"
    assert processor.process(_button("hold_release", press_id=7))["result"] == "ignored_consumed"
    assert [c[0] for c in processor.calls] == ["hold"]


def test_hold_cancel_releases_reservation_but_not_a_print(monkeypatch, idle):
    assert main.handle_button_hold() == "hold_ready"
    assert hardware.hold_action_in_progress is True
    assert main.handle_button_hold_cancel() == "hold_cancelled"
    assert hardware.hold_action_in_progress is False
    assert hardware.try_begin_print_job() is True  # printer free immediately

    # During a print, hold_cancel leaves the print reservation alone.
    assert main.handle_button_hold_cancel() == "hold_cancelled"
    assert hardware.print_in_progress is True


def test_handler_failure_is_reported_not_raised(processor):
    def boom():
        raise RuntimeError("boom")

    processor._handlers["press"] = boom
    assert processor.process(_button("press"))["result"] == "error"


def test_unconfigured_processor_is_not_ready():
    assert InputProcessor().process(_button("press"))["result"] == "not_ready"


# --- gesture handlers in app.main ---------------------------------------------------


@pytest.fixture
def idle(monkeypatch):
    monkeypatch.setattr(hardware, "print_in_progress", False)
    monkeypatch.setattr(hardware, "hold_action_in_progress", False)
    monkeypatch.setattr(hardware, "hold_action_started_at", 0.0)
    monkeypatch.setattr(hardware, "last_print_time", 0.0)
    selection_mode.exit_selection_mode()
    yield
    selection_mode.exit_selection_mode()
    hardware.clear_print_reservation()


def _run_with_loop(monkeypatch, fn):
    """Call a gesture handler from a worker thread while the app loop runs."""

    async def scenario():
        monkeypatch.setattr(main, "global_loop", asyncio.get_running_loop())
        result = await asyncio.to_thread(fn)
        await asyncio.sleep(0.05)  # let scheduled coroutines run
        return result

    return asyncio.run(scenario())


def test_press_prints_the_reported_channel(monkeypatch, idle):
    printed = []

    async def fake_trigger_channel(position, *args, **kwargs):
        printed.append(position)
        hardware.clear_print_reservation(clear_hold=False)

    monkeypatch.setattr(main, "trigger_channel", fake_trigger_channel)
    monkeypatch.setattr(main.dial, "current_position", 1)
    proc = InputProcessor()
    proc.configure(main.dial, {
        "press": main.handle_button_press, "hold": main.handle_button_hold,
        "hold_release": main.handle_button_hold_release,
    })

    result = _run_with_loop(monkeypatch, lambda: proc.process(_button("press", position=3)))

    assert result["result"] == "printing_channel"
    assert printed == [3]
    assert hardware.print_in_progress is False


def test_press_during_a_print_is_rejected_as_busy(monkeypatch, idle):
    monkeypatch.setattr(hardware, "print_in_progress", True)
    assert _run_with_loop(monkeypatch, main.handle_button_press) == "busy"


def test_press_in_menu_selects_or_ignores(monkeypatch, idle):
    chosen = []
    selection_mode.enter_selection_mode(chosen.append, "menu", valid_positions={2, 8})
    monkeypatch.setattr(main.dial, "current_position", 2)
    assert _run_with_loop(monkeypatch, main.handle_button_press) == "selection"
    assert chosen == [2]

    selection_mode.enter_selection_mode(chosen.append, "menu", valid_positions={2, 8})
    monkeypatch.setattr(main.dial, "current_position", 5)
    monkeypatch.setattr(hardware, "last_print_time", 0.0)  # skip debounce
    assert _run_with_loop(monkeypatch, main.handle_button_press) == "selection_ignored"
    assert chosen == [2]
    assert selection_mode.is_selection_mode_active()


def test_hold_during_print_cancels(monkeypatch, idle):
    assert hardware.try_begin_print_job() is True
    assert main.handle_button_hold() == "cancel_requested"
    assert hardware.printer.is_cancel_requested() is True


def test_hold_release_opens_quick_actions(monkeypatch, idle):
    opened = []

    async def fake_menu():
        opened.append(True)
        hardware.clear_print_reservation()

    monkeypatch.setattr(main, "long_press_menu_trigger", fake_menu)
    assert main.handle_button_hold() == "hold_ready"
    assert hardware.hold_action_in_progress is True
    assert _run_with_loop(monkeypatch, main.handle_button_hold_release) == "quick_actions"
    assert opened == [True]


def test_gestures_report_unavailable_printer_without_reserving(monkeypatch, idle):
    offline = RasterPrinter(output=Esp32RelayOutput(link=DeviceLink()))
    monkeypatch.setattr(hardware, "printer", offline)
    monkeypatch.setattr(main, "printer", offline)

    for handler in (main.handle_button_press, main.handle_button_hold,
                    main.handle_button_hold_release):
        assert _run_with_loop(monkeypatch, handler) == "printer_unavailable"
    assert hardware.print_in_progress is False
    assert hardware.hold_action_in_progress is False


def test_health_reports_device_input_state(monkeypatch, no_token):
    assert asyncio.run(main.health_check())["components"]["device_input"] == "disabled"
    monkeypatch.setenv(relay_auth.TOKEN_ENV, TOKEN)
    health = asyncio.run(main.health_check())
    assert health["components"]["device_input"] == "enabled"
    assert "button" not in health["components"] and "dial" not in health["components"]


def test_hold_when_idle_arms_quick_actions_with_tactile_feed(monkeypatch):
    calls = []

    class FakePrinter:
        def feed_dots(self, dots):
            calls.append(("feed", dots))

    monkeypatch.setattr(main, "printer", FakePrinter())
    monkeypatch.setattr(main, "request_print_cancel", lambda: False)
    monkeypatch.setattr(
        main, "reserve_hold_action", lambda: calls.append("reserve") or True
    )

    assert main.handle_button_hold() == "hold_ready"
    assert calls == ["reserve", ("feed", 12)]
