"""Regression tests for hardware/network failure paths."""

import asyncio
import json
import subprocess
import sys
import time
import imaplib
import types
from pathlib import Path
from fastapi import BackgroundTasks, HTTPException
import pytest

import app.main as main_module
import app.hardware as hardware_module
from app.config import CalendarConfig, ChannelConfig, ChannelModuleAssignment, ModuleInstance
from app.drivers.printer_mock import PrinterDriver as MockPrinterDriver
from app.modules import email_client
from app.modules import quotes as quotes_module
from app.modules import news as news_module
from app.modules import calendar as calendar_module
from app.modules import history as history_module
from datetime import date, datetime, timedelta, timezone


def test_email_polling_loop_uses_configured_interval_for_auto_print(monkeypatch):
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    module = types.SimpleNamespace(
        type="email",
        config={"auto_print_new": True, "polling_interval": 60},
    )

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module.settings, "modules", {"email-1": module})

    try:
        asyncio.run(main_module.email_polling_loop())
    except asyncio.CancelledError:
        pass

    assert sleep_calls == [60]


def test_try_begin_print_job_respects_hold_reservation(monkeypatch):
    monkeypatch.setattr(hardware_module, "print_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", True)
    monkeypatch.setattr(hardware_module, "hold_action_started_at", time.time())
    monkeypatch.setattr(hardware_module, "last_print_time", 0.0)

    assert hardware_module.try_begin_print_job(debounce=False) is False

    monkeypatch.setattr(hardware_module, "hold_action_in_progress", False)
    assert hardware_module.try_begin_print_job(debounce=False) is True

    hardware_module.clear_print_reservation()
    assert hardware_module.print_in_progress is False
    assert hardware_module.hold_action_in_progress is False
    assert hardware_module.hold_action_started_at == 0.0


def test_clear_print_reservation_updates_debounce_from_completion(monkeypatch):
    now = 1_000.0

    monkeypatch.setattr(hardware_module.time, "time", lambda: now)
    monkeypatch.setattr(hardware_module, "print_in_progress", True)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_started_at", 0.0)
    monkeypatch.setattr(hardware_module, "last_print_time", 0.0)

    hardware_module.clear_print_reservation(clear_hold=False)

    assert hardware_module.print_in_progress is False
    assert hardware_module.last_print_time == now


def test_print_cancellation_tracks_active_job_and_resets_for_next_job(monkeypatch):
    calls = []

    class FakePrinter:
        def request_cancel(self):
            calls.append("cancel")

        def clear_cancel_request(self):
            calls.append("clear")

    monkeypatch.setattr(hardware_module, "printer", FakePrinter())
    monkeypatch.setattr(hardware_module, "print_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_started_at", 0.0)
    monkeypatch.setattr(hardware_module, "last_print_time", 0.0)

    assert hardware_module.request_print_cancel() is False
    assert hardware_module.try_begin_print_job(debounce=False) is True
    assert calls == ["clear"]
    assert hardware_module.request_print_cancel() is True
    assert calls == ["clear", "cancel"]

    hardware_module.clear_print_reservation()
    assert calls == ["clear", "cancel", "clear"]


def test_long_press_threshold_cancels_print_without_reserving_menu(monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_module, "request_print_cancel", lambda: calls.append("cancel") or True
    )
    monkeypatch.setattr(
        main_module, "reserve_hold_action", lambda: calls.append("reserve") or True
    )

    assert main_module.handle_button_hold() == "cancel_requested"
    assert calls == ["cancel"]


def test_long_press_threshold_preserves_quick_actions_when_idle(monkeypatch):
    calls = []

    class FakePrinter:
        def feed_dots(self, dots):
            calls.append(("feed", dots))

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(main_module, "request_print_cancel", lambda: False)
    monkeypatch.setattr(
        main_module, "reserve_hold_action", lambda: calls.append("reserve") or True
    )

    assert main_module.handle_button_hold() == "hold_ready"
    assert calls == ["reserve", ("feed", 12)]


def test_debug_cancel_print_requests_active_job_cancellation(monkeypatch):
    monkeypatch.setattr(main_module, "request_print_cancel", lambda: True)

    result = asyncio.run(main_module.debug_cancel_print())

    assert result == {"message": "Print cancellation requested"}


def test_debug_cancel_print_rejects_when_no_job_is_active(monkeypatch):
    monkeypatch.setattr(main_module, "request_print_cancel", lambda: False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main_module.debug_cancel_print())

    assert exc_info.value.status_code == 409


def test_try_begin_print_job_clears_stale_hold_reservation(monkeypatch):
    now = 1_000.0

    monkeypatch.setattr(hardware_module.time, "time", lambda: now)
    monkeypatch.setattr(hardware_module, "print_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", True)
    monkeypatch.setattr(
        hardware_module,
        "hold_action_started_at",
        now - hardware_module.HOLD_ACTION_TIMEOUT_SECONDS - 1,
    )
    monkeypatch.setattr(hardware_module, "last_print_time", 0.0)

    assert hardware_module.try_begin_print_job(debounce=False) is True
    assert hardware_module.print_in_progress is True
    assert hardware_module.hold_action_in_progress is False
    assert hardware_module.hold_action_started_at == 0.0

    hardware_module.clear_print_reservation()


def test_long_hold_promotes_hold_reservation_to_factory_reset(monkeypatch):
    captured = {}

    class DummyLoop:
        def is_running(self):
            return True

    def fake_run_coroutine_threadsafe(coro, loop):
        captured["coro"] = coro
        captured["loop"] = loop
        return None

    monkeypatch.setattr(main_module, "global_loop", DummyLoop())
    monkeypatch.setattr(hardware_module, "print_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", True)
    monkeypatch.setattr(hardware_module, "hold_action_started_at", time.time())
    monkeypatch.setattr(main_module.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    assert main_module.handle_button_long_hold() == "factory_reset"

    assert hardware_module.print_in_progress is True
    assert hardware_module.hold_action_in_progress is False
    assert hardware_module.hold_action_started_at == 0.0
    assert captured["loop"] is main_module.global_loop
    captured["coro"].close()

    hardware_module.clear_print_reservation()


def test_long_press_menu_trigger_arms_selection_before_unlock(monkeypatch):
    order = []

    class FakePrinter:
        def clear_hardware_buffer(self):
            order.append("clear_hw")

    class FakeDial:
        def read_position(self):
            return 5

    class ImmediateExecutor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLoop:
        async def run_in_executor(self, executor, fn):
            order.append("run_in_executor")
            fn()

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(main_module, "dial", FakeDial())
    monkeypatch.setattr(main_module, "_print_long_press_menu", lambda pos: order.append(f"print_menu_{pos}"))
    monkeypatch.setattr(
        main_module,
        "clear_print_reservation",
        lambda clear_hold=False: order.append(f"clear_reservation_{clear_hold}"),
    )

    monkeypatch.setattr(main_module.asyncio, "get_event_loop", lambda: FakeLoop())
    monkeypatch.setattr(main_module, "ThreadPoolExecutor", ImmediateExecutor, raising=False)

    import app.selection_mode as selection_mode

    monkeypatch.setattr(selection_mode, "exit_selection_mode", lambda: order.append("exit_selection_mode"))
    monkeypatch.setattr(
        selection_mode,
        "enter_selection_mode",
        lambda callback, module_id, **kwargs: order.append(f"enter_selection_mode_{module_id}"),
    )

    original_executor = getattr(__import__("concurrent.futures", fromlist=["ThreadPoolExecutor"]), "ThreadPoolExecutor")
    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", ImmediateExecutor)

    asyncio.run(main_module.long_press_menu_trigger())

    assert order == [
        "exit_selection_mode",
        "run_in_executor",
        "print_menu_5",
        "enter_selection_mode_quick-actions-5",
        "clear_reservation_False",
    ]


def test_trigger_channel_does_not_hard_reset_printer_between_normal_jobs(monkeypatch):
    events = []

    class FakePrinter:
        def blip(self):
            events.append("blip")

        def clear_hardware_buffer(self):
            events.append("clear_hw")

        def reset_buffer(self, max_lines=0):
            events.append(f"reset_buffer_{max_lines}")

        def print_text(self, text):
            events.append(f"text:{text}")

        def print_line(self):
            events.append("line")

        def feed(self, count):
            events.append(f"feed:{count}")

        def flush_buffer(self):
            events.append("flush")

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(main_module, "settings", types.SimpleNamespace(max_print_lines=200, channels={1: None}))
    monkeypatch.setattr(main_module, "clear_print_reservation", lambda clear_hold=False: None)

    asyncio.run(main_module.trigger_channel(1))

    assert "clear_hw" not in events


def test_scheduler_loop_skips_trigger_when_hold_reserved(monkeypatch):
    triggered = []
    sleep_calls = {"count": 0}

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError

    async def fake_trigger_channel(position, scheduled=False):
        triggered.append((position, scheduled))

    monkeypatch.setattr(main_module, "current_datetime", lambda: datetime(2026, 4, 3, 12, 0))
    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "trigger_channel", fake_trigger_channel)
    monkeypatch.setattr(
        main_module.settings,
        "channels",
        {1: types.SimpleNamespace(schedule=["12:00"])},
    )
    monkeypatch.setattr(hardware_module, "print_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", True)
    monkeypatch.setattr(
        hardware_module,
        "hold_action_started_at",
        time.time(),
    )

    try:
        asyncio.run(main_module.scheduler_loop())
    except asyncio.CancelledError:
        pass

    assert triggered == []
    hardware_module.clear_print_reservation()


def test_scheduler_loop_uses_configured_local_time(monkeypatch):
    triggered = []
    sleep_calls = {"count": 0}

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError

    async def fake_trigger_channel(position, scheduled=False):
        triggered.append((position, scheduled))

    monkeypatch.setattr(main_module, "current_datetime", lambda: datetime(2026, 4, 3, 12, 0))
    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "trigger_channel", fake_trigger_channel)
    monkeypatch.setattr(
        main_module.settings,
        "channels",
        {1: types.SimpleNamespace(schedule=["12:00"])},
    )
    monkeypatch.setattr(main_module, "try_begin_print_job", lambda debounce=False: True)

    try:
        asyncio.run(main_module.scheduler_loop())
    except asyncio.CancelledError:
        pass

    assert triggered == [(1, True)]


def test_run_weather_prefetch_cycle_warms_upcoming_scheduled_weather(monkeypatch):
    calls = []
    main_module._weather_prefetch_state.clear()

    async def run_cycle():
        await main_module._run_weather_prefetch_cycle(datetime(2026, 4, 3, 7, 57, 30))
        await asyncio.sleep(0)

    module = types.SimpleNamespace(id="weather-one", type="weather", config={"city_name": "Worcester"})
    channel = ChannelConfig(
        modules=[ChannelModuleAssignment(module_id="weather-one", order=0)],
        schedule=["08:00"],
    )

    monkeypatch.setattr(
        main_module,
        "settings",
        types.SimpleNamespace(
            channels={1: channel},
            modules={"weather-one": module},
        ),
    )
    monkeypatch.setattr(main_module, "_weather_prefetch_lead_seconds", lambda *_args: 180)
    monkeypatch.setattr(
        main_module.weather,
        "prefetch_weather",
        lambda config, module_id=None: calls.append((module_id, dict(config or {}))) or {"ok": True},
    )

    asyncio.run(run_cycle())

    assert calls == [("weather-one", {"city_name": "Worcester"})]


def test_calendar_day_view_uses_configured_local_today_label(monkeypatch):
    lines = []
    target_date = date(2026, 5, 12)

    class FakePrinter:
        def print_subheader(self, text):
            lines.append(str(text))

        def feed(self, _count):
            return None

        def print_bold(self, text):
            lines.append(str(text))

        def print_body(self, text):
            lines.append(str(text))

    monkeypatch.setattr(calendar_module, "current_date", lambda: target_date)

    calendar_module._print_calendar_day_view(
        FakePrinter(),
        [target_date],
        {
            target_date: [
                {
                    "sort_key": "09:00",
                    "is_all_day": False,
                    "time": "9:00 AM",
                    "summary": "Breakfast",
                }
            ]
        },
    )

    assert lines[0] == "TODAY (05/12)"


def test_usb_output_handles_serial_open_failure():
    from app.drivers.usb_serial_output import UsbSerialOutput

    def refuse(port, baudrate):
        raise OSError("no port")

    output = UsbSerialOutput(port="COM99", serial_factory=refuse)
    assert output.ser is None
    assert output.is_available() is False


def test_fetch_emails_sets_auth_failed_on_imap_auth_error(monkeypatch):
    config = {
        "email_service": "Custom",
        "email_user": "user@example.com",
        "email_password": "bad-password",
        "email_host": "imap.example.com",
        "email_port": 993,
        "email_use_ssl": True,
    }

    def fake_imap4_ssl(*args, **kwargs):  # noqa: ARG001
        raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_imap4_ssl)

    messages = email_client.fetch_emails(config)
    assert messages == []
    assert email_client._LAST_FETCH_ERROR == "auth_failed"


def test_news_module_uses_configurable_country_and_page_size(monkeypatch):
    captured = {}

    class DummyResponse:
        def json(self):
            return {"status": "ok", "articles": []}

    def fake_get(url, params=None, timeout=0):  # noqa: ARG001
        captured["params"] = params or {}
        return DummyResponse()

    monkeypatch.setattr(news_module.requests, "get", fake_get)
    news_module.get_newsapi_articles(
        {
            "news_api_key": "abc123",
            "country": "ca",
            "page_size": 7,
        }
    )

    assert captured["params"]["country"] == "ca"
    assert captured["params"]["pageSize"] == 7


def _large_google_style_calendar_with_upcoming_event():
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    future = now + timedelta(days=1, hours=15)

    historical_events = []
    for i in range(15000):
        start = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=i % 365,
            minutes=i,
        )
        historical_events.extend(
            [
                "BEGIN:VEVENT",
                f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
                f"DTEND:{(start + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}",
                f"SUMMARY:Old event {i}",
                "END:VEVENT",
            ]
        )

    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
            "CALSCALE:GREGORIAN",
            *historical_events,
            "BEGIN:VEVENT",
            f"DTSTART:{future.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{(future + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}",
            "SUMMARY:Upcoming event beyond old cap",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )

    assert len(ics.encode("utf-8")) > 1024 * 1024
    assert len(ics.encode("utf-8")) < calendar_module.MAX_ICS_BYTES
    return ics, future.date()


class _DummyCalendarResponse:
    encoding = "utf-8"

    def __init__(self, body: str | bytes):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


def test_fetch_ics_does_not_truncate_large_google_calendar(monkeypatch):
    ics, event_day = _large_google_style_calendar_with_upcoming_event()
    response = _DummyCalendarResponse(ics)
    captured = {}

    def fake_get(url, timeout=0, stream=False):  # noqa: ARG001
        captured["stream"] = stream
        return response

    monkeypatch.setattr(calendar_module.requests, "get", fake_get)

    fetched = calendar_module.fetch_ics("https://calendar.google.com/private/basic.ics")
    assert captured["stream"] is True
    assert response.closed is True
    assert fetched is not None
    assert "Upcoming event beyond old cap" in fetched

    events = calendar_module.parse_events(fetched, days_to_show=7, timezone_str="UTC")
    assert event_day in events
    assert events[event_day][0]["summary"] == "Upcoming event beyond old cap"


def test_fetch_ics_rejects_feed_that_exceeds_size_limit(monkeypatch):
    monkeypatch.setattr(calendar_module, "MAX_ICS_BYTES", 64)
    response = _DummyCalendarResponse(b"BEGIN:VCALENDAR\r\n" + (b"X" * 80))

    monkeypatch.setattr(
        calendar_module.requests,
        "get",
        lambda url, timeout=0, stream=False: response,  # noqa: ARG005
    )

    assert calendar_module.fetch_ics("https://example.com/too-large.ics") is None
    assert response.closed is True


def test_calendar_rrule_respects_exdate_in_window():
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = now + timedelta(hours=9)
    skip = start + timedelta(days=1)

    dt_fmt = "%Y%m%dT%H%M%SZ"
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            f"DTSTART:{start.strftime(dt_fmt)}",
            "RRULE:FREQ=DAILY;COUNT=3",
            f"EXDATE:{skip.strftime(dt_fmt)}",
            "SUMMARY:Recurring test event",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )

    events = calendar_module.parse_events(ics, days_to_show=4, timezone_str="UTC")
    event_days = set(events.keys())

    assert start.date() in event_days
    assert skip.date() not in event_days
    assert (start + timedelta(days=2)).date() in event_days


def test_calendar_config_accepts_ui_sources_without_label():
    config = CalendarConfig(
        ical_sources=[
            {"url": "https://example.com/basic.ics"},
            {"name": "Home", "url": "https://example.com/home.ics"},
        ],
        view_mode="month",
    )

    assert [source.url for source in config.ical_sources] == [
        "https://example.com/basic.ics",
        "https://example.com/home.ics",
    ]
    assert config.ical_sources[1].name == "Home"


def test_fetch_ics_rejects_non_calendar_response(monkeypatch):
    monkeypatch.setattr(
        calendar_module.requests,
        "get",
        lambda url, timeout=0, stream=False: _DummyCalendarResponse(  # noqa: ARG005
            "<html><title>Sign in</title></html>"
        ),
    )

    assert calendar_module.fetch_ics("https://example.com/basic.ics") is None


def test_calendar_receipt_reports_unloadable_feed(monkeypatch, capsys):
    monkeypatch.setattr(calendar_module, "fetch_ics", lambda url: None)  # noqa: ARG005

    printer = MockPrinterDriver()
    calendar_module.format_calendar_receipt(
        printer,
        CalendarConfig(
            ical_sources=[{"url": "https://example.com/basic.ics"}],
            view_mode="month",
        ),
        "Calendar",
    )

    output = capsys.readouterr().out
    assert "Could not load calendar feed." in output
    assert "Check the iCal URL." in output
    assert "No upcoming events." not in output


def test_calendar_receipt_reports_unreadable_feed_when_parse_fails(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        calendar_module,
        "fetch_ics",
        lambda url: "BEGIN:VCALENDAR\r\nBROKEN\r\nEND:VCALENDAR\r\n",  # noqa: ARG005
    )

    def fake_parse_events(
        ics_content,
        days_to_show,
        timezone_str,
        *,
        raise_on_parse_error=False,
    ):  # noqa: ARG001
        if raise_on_parse_error:
            raise calendar_module.CalendarParseError("broken feed")
        return {}

    monkeypatch.setattr(calendar_module, "parse_events", fake_parse_events)

    printer = MockPrinterDriver()
    calendar_module.format_calendar_receipt(
        printer,
        CalendarConfig(
            ical_sources=[{"url": "https://example.com/broken.ics"}],
            view_mode="month",
        ),
        "Calendar",
    )

    output = capsys.readouterr().out
    assert "Could not read calendar feed." in output
    assert "Check the iCal URL." in output
    assert "No upcoming events." not in output


def test_quotes_module_does_not_download_when_local_db_is_missing(monkeypatch):
    missing_path = Path("/tmp/pc1-missing-quotes.json")
    monkeypatch.setattr(quotes_module, "_get_quotes_db_path", lambda: missing_path)

    quote = quotes_module.get_random_quote()

    assert quote["quoteText"] == "Offline quotes database is missing."
    assert quote["quoteAuthor"] == "System"


def test_bundled_quotes_db_is_valid_utf8():
    bundled_path = quotes_module._get_quotes_db_path()
    with bundled_path.open("r", encoding="utf-8") as f:
        quotes = json.load(f)

    assert isinstance(quotes, list)
    assert quotes


def test_history_module_does_not_download_when_local_db_is_missing(monkeypatch):
    monkeypatch.setattr(history_module, "LOCAL_DB_PATH", Path("/tmp/pc1-missing-history.json"))

    events = history_module.get_events_for_today()

    assert events == ["Offline history database is missing."]


def test_selection_mode_ignores_overlapping_callbacks():
    import threading
    import app.selection_mode as selection_mode

    selection_mode.exit_selection_mode()
    started = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def slow_callback(dial_position):
        calls.append(dial_position)
        started.set()
        release.wait(timeout=1)

    selection_mode.enter_selection_mode(slow_callback, "busy-test")

    worker = threading.Thread(
        target=lambda: results.append(selection_mode.handle_selection(1))
    )
    worker.start()
    assert started.wait(timeout=1)

    results.append(selection_mode.handle_selection(2))
    release.set()
    worker.join(timeout=1)

    assert calls == [1]
    assert results.count(True) == 1
    assert results.count(False) == 1

    selection_mode.exit_selection_mode()


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


def test_adventure_ignores_invalid_choice_without_printing(monkeypatch, tmp_path):
    import app.hardware as hardware
    import app.selection_mode as selection_mode
    from app.modules import adventure

    selection_mode.exit_selection_mode()

    class FakePrinter:
        def __init__(self):
            self.bodies = []

        def reset_buffer(self, max_lines=0):  # noqa: ARG002
            return None

        def print_header(self, *args, **kwargs):  # noqa: ARG002
            return None

        def print_subheader(self, *args, **kwargs):  # noqa: ARG002
            return None

        def print_body(self, text):
            self.bodies.append(text)

        def print_caption(self, *args, **kwargs):  # noqa: ARG002
            return None

        def print_line(self):
            return None

        def feed(self, lines=1):  # noqa: ARG002
            return None

        def flush_buffer(self):
            return None

    fake_printer = FakePrinter()

    monkeypatch.setattr(hardware, "printer", fake_printer)
    monkeypatch.setattr(
        adventure,
        "_get_state_path",
        lambda module_id: tmp_path / f"{module_id}.json",
    )

    adventure.format_adventure_receipt(
        fake_printer,
        config={},
        module_name="ADVENTURE",
        module_id="adventure-one",
    )

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "adventure-one"
    printed_before_invalid = list(fake_printer.bodies)

    assert selection_mode.can_handle_selection(7) is False
    assert selection_mode.handle_selection(7) is False

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "adventure-one"
    assert fake_printer.bodies == printed_before_invalid

    selection_mode.exit_selection_mode()


def test_quick_actions_ignores_invalid_selection_without_printing(monkeypatch):
    order = []
    captured = {}

    class FakePrinter:
        def reset_buffer(self, max_lines=0):  # noqa: ARG002
            order.append("reset_buffer")

        def print_header(self, *args, **kwargs):  # noqa: ARG002
            order.append("print_header")

        def print_caption(self, *args, **kwargs):  # noqa: ARG002
            order.append("print_caption")

        def feed(self, lines=1):  # noqa: ARG002
            order.append("feed")

        def flush_buffer(self):
            order.append("flush_buffer")

    class FakeDial:
        def read_position(self):
            return 5

    class ImmediateExecutor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLoop:
        async def run_in_executor(self, executor, fn):
            order.append("run_in_executor")
            fn()

    def fake_enter_selection_mode(callback, module_id, valid_positions=None):
        captured["callback"] = callback
        captured["module_id"] = module_id
        captured["valid_positions"] = valid_positions
        order.append(f"enter_selection_mode_{module_id}")

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(main_module, "dial", FakeDial())
    monkeypatch.setattr(main_module, "_print_long_press_menu", lambda pos: order.append(f"print_menu_{pos}"))
    monkeypatch.setattr(
        main_module,
        "clear_print_reservation",
        lambda clear_hold=False: order.append(f"clear_reservation_{clear_hold}"),
    )
    monkeypatch.setattr(main_module.asyncio, "get_event_loop", lambda: FakeLoop())

    import app.hardware as hardware
    import app.selection_mode as selection_mode

    monkeypatch.setattr(hardware, "printer", FakePrinter())
    monkeypatch.setattr(selection_mode, "exit_selection_mode", lambda: order.append("exit_selection_mode"))
    monkeypatch.setattr(selection_mode, "enter_selection_mode", fake_enter_selection_mode)
    monkeypatch.setattr("concurrent.futures.ThreadPoolExecutor", ImmediateExecutor)

    asyncio.run(main_module.long_press_menu_trigger())

    order.clear()
    captured["callback"](6)

    assert order == []
    assert captured["module_id"] == "quick-actions-5"
    assert captured["valid_positions"] == {1, 2, 5, 8}


def test_application_reset_keeps_password_and_runs_no_host_commands(monkeypatch, tmp_path):
    import app.config as config_module
    import app.factory_reset as factory_reset

    saved = []
    fake_config_file = tmp_path / "app" / "config.py"
    fake_config_file.parent.mkdir()
    (tmp_path / "config.json.bak").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config_module, "__file__", str(fake_config_file))
    monkeypatch.setattr(config_module, "save_config", saved.append)
    monkeypatch.setattr(config_module, "settings", config_module.Settings(city_name="Old"))
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "unchanged-password")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("reset must not run host commands")
    )
    monkeypatch.setattr(main_module, "global_loop", None)
    monkeypatch.setattr(main_module, "settings", config_module.settings)

    result = main_module._reset_application_settings()

    assert result == {"config_cleared": True, "errors": []}
    assert len(saved) == 1 and saved[0] is config_module.settings
    assert main_module.settings is config_module.settings
    assert main_module.settings.city_name != "Old"
    assert not (tmp_path / "config.json.bak").exists()
    import app.device_password as device_password
    assert device_password.get_device_password() == "unchanged-password"


def test_application_reset_failure_leaves_live_settings(monkeypatch):
    import app.config as config_module

    original = config_module.Settings(city_name="Keep")
    monkeypatch.setattr(config_module, "settings", original)
    monkeypatch.setattr(main_module, "settings", original)

    def fail_save(_settings):
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "save_config", fail_save)

    result = main_module._reset_application_settings()

    assert result["config_cleared"] is False
    assert "disk full" in result["errors"][0]
    assert config_module.settings is original
    assert main_module.settings is original


def test_quick_factory_reset_confirmation_ignores_empty_slots(monkeypatch):
    import app.hardware as hardware
    import app.selection_mode as selection_mode

    events = []

    class FakePrinter:
        def reset_buffer(self, max_lines=0):  # noqa: ARG002
            events.append("reset_buffer")

        def print_header(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_header")

        def print_line(self):
            events.append("print_line")

        def print_bold(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_bold")

        def print_body(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_body")

        def print_subheader(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_subheader")

        def print_caption(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_caption")

        def feed(self, lines=1):  # noqa: ARG002
            events.append("feed")

        def flush_buffer(self):
            events.append("flush_buffer")

    fake_printer = FakePrinter()
    monkeypatch.setattr(hardware, "printer", fake_printer)

    selection_mode.exit_selection_mode()
    main_module._confirm_quick_factory_reset(fake_printer, "quick-factory-reset")

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "quick-factory-reset-reset-confirm"
    assert selection_mode.can_handle_selection(1) is True
    assert selection_mode.can_handle_selection(2) is False
    assert selection_mode.can_handle_selection(8) is True

    events.clear()
    assert selection_mode.handle_selection(2) is False
    assert events == []

    assert selection_mode.handle_selection(8) is True
    assert "reset_buffer" in events
    assert selection_mode.is_selection_mode_active() is False


def test_invalid_selection_position_does_not_blip(monkeypatch):
    import app.selection_mode as selection_mode

    calls = []

    class FakePrinter:
        def blip(self):
            calls.append("blip")

    selection_mode.exit_selection_mode()
    selection_mode.enter_selection_mode(
        lambda dial_position: calls.append(f"choice_{dial_position}"),
        "valid-position-test",
        valid_positions={1},
    )

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(
        main_module,
        "clear_print_reservation",
        lambda clear_hold=False: calls.append(f"clear_{clear_hold}"),
    )

    asyncio.run(main_module.handle_selection_async(2))

    assert calls == ["clear_False"]
    assert selection_mode.is_selection_mode_active() is True

    selection_mode.exit_selection_mode()


def test_interactive_module_picker_ignores_empty_slots(monkeypatch):
    import app.selection_mode as selection_mode

    events = []

    class FakePrinter:
        def blip(self):
            events.append("blip")

        def reset_buffer(self, max_lines=0):  # noqa: ARG002
            events.append("reset_buffer")

        def print_header(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_header")

        def print_line(self):
            events.append("print_line")

        def print_body(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_body")

        def print_caption(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_caption")

        def feed(self, lines=1):  # noqa: ARG002
            events.append("feed")

        def flush_buffer(self):
            events.append("flush_buffer")

    modules = {
        "adventure-one": ModuleInstance(
            id="adventure-one",
            type="adventure",
            name="Adventure One",
            config={},
        ),
        "adventure-two": ModuleInstance(
            id="adventure-two",
            type="adventure",
            name="Adventure Two",
            config={},
        ),
    }
    channels = {
        1: ChannelConfig(
            modules=[
                ChannelModuleAssignment(module_id="adventure-one", order=0),
                ChannelModuleAssignment(module_id="adventure-two", order=1),
            ]
        )
    }

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(
        main_module,
        "settings",
        types.SimpleNamespace(max_print_lines=200, channels=channels, modules=modules),
    )
    monkeypatch.setattr(
        main_module,
        "get_module",
        lambda module_type: types.SimpleNamespace(interactive=True),
        raising=False,
    )
    monkeypatch.setattr(main_module, "clear_print_reservation", lambda clear_hold=False: None)

    selection_mode.exit_selection_mode()

    asyncio.run(main_module.trigger_channel(1))

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "channel-menu-1"
    assert selection_mode.can_handle_selection(1) is True
    assert selection_mode.can_handle_selection(2) is True
    assert selection_mode.can_handle_selection(3) is False
    assert selection_mode.can_handle_selection(8) is True

    events.clear()
    asyncio.run(main_module.handle_selection_async(3))

    assert events == []
    assert selection_mode.is_selection_mode_active() is True

    selection_mode.exit_selection_mode()
