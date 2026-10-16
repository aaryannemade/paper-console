"""Regression tests for snapshot gallery fixture data."""

from datetime import date
import importlib.util
from pathlib import Path

from app.drivers.printer_mock import PrinterDriver
from app.modules import email_client
from app.modules import history as history_module
from PIL import Image


def _load_render_all_prints_module():
    script_path = Path(__file__).resolve().parent / "render_all_prints.py"
    spec = importlib.util.spec_from_file_location(
        "render_all_prints_test_module", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_snapshot_defaults_fill_blank_modules():
    render_all_prints = _load_render_all_prints_module()

    history_config = render_all_prints._apply_snapshot_defaults("history", {})
    text_config = render_all_prints._apply_snapshot_defaults(
        "text",
        {"content_doc": {"type": "doc", "content": [{"type": "paragraph"}]}},
    )
    calendar_config = render_all_prints._apply_snapshot_defaults("calendar", {})
    email_config = render_all_prints._apply_snapshot_defaults("email", {})

    assert history_config["reference_date"] == render_all_prints.SNAPSHOT_HISTORY_DATE
    assert render_all_prints.text._doc_has_visible_content(text_config["content_doc"])
    assert "BEGIN:VCALENDAR" in calendar_config["mock_ics_content"]
    assert calendar_config["view_mode"] == "month"
    assert email_config["mock_messages"][0]["subject"] == "Hi Grandma"

    # Live credentials (e.g. from .env) never reach IMAP during a gallery render.
    live_email = render_all_prints._apply_snapshot_defaults(
        "email",
        {"email_user": "real@example.com", "email_password": "secret", "email_host": "imap.example.com"},
    )
    assert live_email["mock_messages"][0]["subject"] == "Hi Grandma"


def test_calendar_view_mode_snapshots_force_day_and_week(monkeypatch, tmp_path):
    render_all_prints = _load_render_all_prints_module()
    captured = []

    def _fake_execute_module(module, printer, include_interactive):  # noqa: ARG001
        captured.append(
            (
                module.name,
                module.config["view_mode"],
                module.config["ical_sources"],
                module.config["mock_ics_content"],
            )
        )
        printer.captured_bitmaps.append(Image.new("1", (8, 8), 1))
        return True

    monkeypatch.setattr(render_all_prints, "_execute_module", _fake_execute_module)

    for view_mode in render_all_prints.ADDITIONAL_CALENDAR_VIEW_MODE_SNAPSHOTS:
        saved = render_all_prints._render_calendar_view_mode_snapshot(
            view_mode=view_mode,
            out_dir=tmp_path,
            include_interactive=True,
            max_lines=200,
        )
        assert saved.name == f"module_type_calendar_{view_mode}.png"

    assert [(item[0], item[1], item[2]) for item in captured] == [
        ("Calendar (Day)", "day", []),
        ("Calendar (Week)", "week", []),
    ]
    assert all("BEGIN:VCALENDAR" in item[3] for item in captured)


def test_fetch_emails_returns_mock_messages_without_imap(monkeypatch):
    called = {"imap": False}

    def _unexpected_imap(*args, **kwargs):  # noqa: ARG001
        called["imap"] = True
        raise AssertionError("IMAP should not be used for mock gallery messages")

    monkeypatch.setattr(email_client.imaplib, "IMAP4_SSL", _unexpected_imap)

    messages = email_client.fetch_emails(
        {
            "mock_messages": [
                {
                    "from": "Mom <mom@example.com>",
                    "subject": "Bring tea to Grandma",
                    "body": "Can you drop off the chamomile tea tonight?",
                }
            ]
        }
    )

    assert called["imap"] is False
    assert messages == [
        {
            "from": "Mom <mom@example.com>",
            "subject": "Bring tea to Grandma",
            "body": "Can you drop off the chamomile tea tonight?",
        }
    ]


def test_history_reference_date_uses_target_day(monkeypatch, capsys):
    captured = {}

    def _fake_events(target_date):
        captured["target_date"] = target_date
        return [
            "1969: First event",
            "1970: Second event",
            "1971: Third event",
        ]

    def _fake_timeline(width, items, item_height, font):  # noqa: ARG001
        captured["items"] = items
        return Image.new("1", (32, 32), 1)

    monkeypatch.setattr(history_module, "get_events_for_date", _fake_events)
    monkeypatch.setattr(history_module, "draw_timeline_image", _fake_timeline)

    history_module.format_history_receipt(
        PrinterDriver(),
        {"reference_date": "1969-07-20", "count": 2},
        "On This Day",
    )

    output = capsys.readouterr().out
    assert captured["target_date"] == date(1969, 7, 20)
    assert captured["items"] == [
        {"year": 1969, "text": "First event"},
        {"year": 1970, "text": "Second event"},
    ]
    assert "July 20, 1969" in output
