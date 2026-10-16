"""The server lifecycle starts and stops its own tasks without unsolicited prints."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.main as main


@pytest.mark.asyncio
async def test_startup_starts_and_stops_background_tasks(monkeypatch):
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "server-test-password")

    monkeypatch.setattr(main, "printer", MagicMock())
    monkeypatch.setattr(main, "dial", MagicMock())

    started, stopped = set(), set()

    def worker(name):
        async def run():
            started.add(name)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(name)
        return run

    for name in ("email_polling_loop", "scheduler_loop", "task_watchdog"):
        monkeypatch.setattr(main, name, worker(name))
    monkeypatch.setattr(main.slack_manager, "init_runtime", MagicMock())
    monkeypatch.setattr(main.slack_manager, "reconcile", AsyncMock())
    shutdown = AsyncMock()
    monkeypatch.setattr(main.slack_manager, "shutdown", shutdown)
    # Restore the global loop/task references after this test's loop closes.
    for name in ("global_loop", "email_polling_task", "scheduler_task", "task_monitor_task"):
        monkeypatch.setattr(main, name, None)

    tasks_before = asyncio.all_tasks()
    async with main.lifespan(main.app):
        await asyncio.sleep(0)
        assert started == {"email_polling_loop", "scheduler_loop", "task_watchdog"}

    assert stopped == started
    main.printer.print_header.assert_not_called()
    main.printer.print_text.assert_not_called()
    main.printer.print_body.assert_not_called()
    shutdown.assert_awaited_once()
    assert asyncio.all_tasks() == tasks_before
