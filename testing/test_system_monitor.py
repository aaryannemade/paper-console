"""System Monitor reports application state instead of container-host metrics."""

import asyncio

import app.main as main
import app.selection_mode as selection_mode
from app.config import Settings
from app.drivers.print_outputs import ConsoleOutput
from app.drivers.printer_mock import PrinterDriver
from app.drivers.printer_raster import RasterPrinter
from app.modules import system_monitor
from app.relay_link import link


def test_system_monitor_shows_output_relay_and_data(monkeypatch, capsys):
    monkeypatch.setattr(link, "snapshot", lambda: {
        "state": "online", "device_id": "esp32-test", "heartbeat_age_s": 4.2,
        "last_job": {"state": "completed"},
    })
    monkeypatch.setattr(system_monitor.shutil, "disk_usage", lambda path: (2**30, 0, 2**30))
    printer = RasterPrinter(output=ConsoleOutput(), echo=PrinterDriver())

    system_monitor.format_system_monitor_receipt(printer)
    output = capsys.readouterr().out

    assert "Output: console" in output
    assert "Link: online" in output
    assert "Device: esp32-test" in output
    assert "Heartbeat: 4s ago" in output
    assert "Last job: completed" in output
    assert "Free: 1.0 GiB" in output


def test_standard_only_channel_does_not_print_empty_app_picker(monkeypatch, capsys):
    printer = RasterPrinter(output=ConsoleOutput(), echo=PrinterDriver())
    monkeypatch.setattr(main, "printer", printer)
    monkeypatch.setattr(main, "settings", Settings())

    asyncio.run(main.trigger_channel(8))

    output = capsys.readouterr().out
    assert "SYSTEM MONITOR" in output
    assert "SELECT APP" not in output
    assert not selection_mode.is_selection_mode_active()
