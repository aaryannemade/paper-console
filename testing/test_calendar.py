"""Calendar feed fetching, parsing and receipts."""

from datetime import date, datetime, timedelta, timezone

from app.config import CalendarConfig
from app.drivers.printer_mock import PrinterDriver
from app.modules import calendar as calendar_module


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


def test_fetch_ics_rejects_non_calendar_response(monkeypatch):
    monkeypatch.setattr(
        calendar_module.requests,
        "get",
        lambda url, timeout=0, stream=False: _DummyCalendarResponse(  # noqa: ARG005
            "<html><title>Sign in</title></html>"
        ),
    )

    assert calendar_module.fetch_ics("https://example.com/basic.ics") is None


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


def test_calendar_receipt_reports_unloadable_feed(monkeypatch, capsys):
    monkeypatch.setattr(calendar_module, "fetch_ics", lambda url: None)  # noqa: ARG005

    printer = PrinterDriver()
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

    printer = PrinterDriver()
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


def test_calendar_day_view_prints_agenda_list(monkeypatch):
    today = date(2026, 5, 12)
    captured = []

    monkeypatch.setattr(calendar_module, "current_date", lambda: today)

    class FakePrinter:
        width = 42

        def print_subheader(self, text):
            captured.append(("subheader", text))

        def print_bold(self, text):
            captured.append(("bold", text))

        def print_body(self, text):
            captured.append(("body", text))

        def feed(self, lines):  # noqa: ARG002
            captured.append(("feed", lines))

    events = {
        today: [
            {
                "time": "All Day",
                "summary": "Library pickup day",
                "sort_key": "00:00",
                "datetime": None,
                "is_all_day": True,
            },
            {
                "time": "9:30 AM",
                "summary": (
                    "Mock calendar event with a very long descriptive title "
                    "that should be handed to the printer as one paragraph"
                ),
                "sort_key": "09:30",
                "datetime": None,
                "is_all_day": False,
            }
        ]
    }

    calendar_module._print_calendar_day_view(
        FakePrinter(),
        [today],
        events,
    )

    # The label uses the configured local date, not the server's UTC date.
    assert captured[0] == ("subheader", "TODAY (05/12)")
    assert ("bold", "All Day") in captured
    assert ("body", "Library pickup day") in captured
    assert ("bold", "9:30 AM") in captured
    assert (
        "body",
        (
            "Mock calendar event with a very long descriptive title "
            "that should be handed to the printer as one paragraph"
        ),
    ) in captured


def test_calendar_week_and_month_views_do_not_truncate_normal_long_titles(monkeypatch):
    today = date(2026, 5, 12)
    long_title = (
        "Mock calendar event with a descriptive title that is longer than "
        "one thermal printer line but should still be preserved"
    )
    captured = []

    monkeypatch.setattr(calendar_module, "current_date", lambda: today)

    class FakePrinter:
        width = 42

        def print_subheader(self, text):
            captured.append(("subheader", text))

        def print_bold(self, text):
            captured.append(("bold", text))

        def print_body(self, text):
            captured.append(("body", text))

        def print_line(self):
            captured.append(("line", ""))

        def feed(self, lines):  # noqa: ARG002
            captured.append(("feed", lines))

        def print_image(self, image):
            captured.append(("image", image.size))

    events = {
        today: [
            {
                "time": "9:30 AM",
                "summary": long_title,
                "sort_key": "09:30",
                "datetime": None,
                "is_all_day": False,
            }
        ]
    }

    printer = FakePrinter()
    calendar_module._print_calendar_week_view(printer, [today], events)
    calendar_module._print_calendar_month_view(printer, [today], events)

    body_lines = [text for kind, text in captured if kind == "body"]
    assert f"9:30 AM {long_title}" in body_lines
    assert f"  9:30 AM {long_title}" in body_lines
    assert all(".." not in line for line in body_lines)


def test_calendar_event_titles_have_high_safety_cap():
    title = "A" * (calendar_module.MAX_EVENT_TITLE_CHARS + 20)

    rendered = calendar_module._event_title(title)

    assert len(rendered) == calendar_module.MAX_EVENT_TITLE_CHARS
    assert rendered.endswith("...")


def test_calendar_receipt_parses_mock_ics_content(capsys):
    # The gallery's offline fixture goes through the real ICS parser.
    today = calendar_module.current_date() + timedelta(days=1)
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "BEGIN:VEVENT",
            f"DTSTART:{today.strftime('%Y%m%d')}T103000",
            "SUMMARY:Mock calendar event",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    printer = PrinterDriver()

    calendar_module.format_calendar_receipt(
        printer,
        CalendarConfig(mock_ics_content=ics, view_mode="month"),
        "Calendar",
    )

    output = capsys.readouterr().out
    assert "Mock calendar event" in output
