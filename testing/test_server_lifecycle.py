"""The server lifecycle must not provision networking or print boot receipts."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.main as main


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_boot", [False, True])
async def test_startup_is_independent_of_host_networking(monkeypatch, tmp_path, previous_boot):
    marker = tmp_path / ".welcome_printed"
    if previous_boot:
        marker.write_text("1")
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "server-test-password")

    host_commands = []

    def forbidden_command(*args, **kwargs):
        host_commands.append(args)
        raise AssertionError("Server startup must not run host administration commands")

    monkeypatch.setattr(main.subprocess, "run", forbidden_command)
    monkeypatch.setattr(main, "printer", MagicMock())
    monkeypatch.setattr(main, "button", MagicMock())
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
    assert host_commands == []
    main.printer.print_header.assert_not_called()
    main.printer.print_text.assert_not_called()
    main.printer.print_body.assert_not_called()
    shutdown.assert_awaited_once()
    assert asyncio.all_tasks() == tasks_before
    assert marker.exists() == previous_boot
