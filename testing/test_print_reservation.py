"""Print-job reservation, debounce and cancellation (app/hardware.py)."""

import time

import app.hardware as hardware_module


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

