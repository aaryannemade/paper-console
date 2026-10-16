"""Print output selection, console echo/capture and the offline policy (Step 3a)."""

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

import app.hardware as hardware
import app.main as main
from app.drivers import escpos
from app.drivers.esp32_relay_output import Esp32RelayOutput
from app.drivers.print_outputs import (
    ConsoleOutput,
    PrinterUnavailableError,
    create_output,
    resolve_output_mode,
)
from app.drivers.printer_mock import PrinterDriver as TextEchoPrinter
from app.drivers.printer_raster import RasterPrinter
from app.relay_link import DeviceLink

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --- selection -----------------------------------------------------------------


def test_default_output_is_esp32(monkeypatch):
    monkeypatch.delenv("PC1_PRINTER_OUTPUT", raising=False)
    assert resolve_output_mode() == "esp32"
    assert isinstance(create_output(), Esp32RelayOutput)


@pytest.mark.parametrize("value,mode", [("console", "console"), (" USB ", "usb"), ("esp32", "esp32")])
def test_output_mode_is_read_from_env(monkeypatch, value, mode):
    monkeypatch.setenv("PC1_PRINTER_OUTPUT", value)
    assert resolve_output_mode() == mode


def test_unknown_output_mode_fails_loudly(monkeypatch):
    monkeypatch.setenv("PC1_PRINTER_OUTPUT", "serial0")
    with pytest.raises(ValueError, match="PC1_PRINTER_OUTPUT"):
        create_output()


def test_usb_output_is_built_from_env(monkeypatch):
    monkeypatch.setenv("PC1_PRINTER_OUTPUT", "usb")
    monkeypatch.setenv("PC1_PRINTER_PORT", "/dev/does-not-exist")
    monkeypatch.setenv("PC1_PRINTER_BAUDRATE", "19200")

    output = create_output()

    assert output.mode == "usb"
    assert (output.configured_port, output.baudrate) == ("/dev/does-not-exist", 19200)
    assert output.is_available() is False


def test_console_mode_echoes_text(monkeypatch):
    monkeypatch.setenv("PC1_PRINTER_OUTPUT", "console")
    printer = hardware.create_printer()
    assert printer.output_mode == "console"
    assert isinstance(printer.echo, TextEchoPrinter)


def test_no_raspberry_pi_or_gpio_code_in_print_path():
    assert not (PROJECT_ROOT / "app" / "drivers" / "printer_serial.py").exists()
    for name in ("hardware.py", "drivers/printer_raster.py", "drivers/print_outputs.py",
                 "drivers/usb_serial_output.py", "drivers/escpos.py"):
        source = (PROJECT_ROOT / "app" / name).read_text(encoding="utf-8")
        for needle in ("device-tree", "_is_raspberry_pi", "gpio_ioctl", "button_gpio",
                       "dial_gpio", "busy_pin", "/dev/serial0"):
            assert needle not in source.lower(), (name, needle)


# --- console output ------------------------------------------------------------------


def test_console_echo_prints_each_call_once(capsys):
    printer = RasterPrinter(output=ConsoleOutput(), echo=TextEchoPrinter())
    printer.print_header("Sudoku")
    printer.print_body("hello")  # print_body calls print_text internally

    out = capsys.readouterr().out
    assert out.count("SUDOKU") == 1
    assert out.count("hello") == 1


def test_console_output_logs_receipt_and_saves_upright_png(tmp_path, capsys):
    output = ConsoleOutput(capture_dir=str(tmp_path))
    printer = RasterPrinter(output=output)
    printer.print_header("Capture")
    printer.flush_buffer()

    line = [l for l in capsys.readouterr().out.splitlines() if "[RECEIPT]" in l][0]
    assert "384x" in line and "strips" in line and "bytes" in line
    (saved,) = tmp_path.glob("receipt-*.png")
    image = Image.open(saved)
    assert image.width == 384
    # Upright: the header box is at the top of the saved image.
    top = image.convert("1").crop((0, 0, 384, image.height // 2))
    assert top.getbbox() is not None


def test_console_byte_count_matches_encoded_receipt():
    img = Image.new("1", (384, 50), 1)
    stats = ConsoleOutput().print_receipt(img, feed_lines_after=2, cancel_event=None)
    expected = len(escpos.JOB_PREAMBLE) + sum(len(s) for s in escpos.iter_raster_strips(img))
    assert stats["bytes_sent"] == expected + len(escpos.feed_lines(2))


# --- esp32 placeholder and offline policy --------------------------------------------


def test_esp32_output_is_unavailable_without_heartbeat():
    output = Esp32RelayOutput(link=DeviceLink())
    assert output.is_available() is False
    with pytest.raises(PrinterUnavailableError):
        output.print_receipt(Image.new("1", (384, 24)), feed_lines_after=0, cancel_event=None)


@pytest.fixture
def offline_printer(monkeypatch):
    printer = RasterPrinter(output=Esp32RelayOutput(link=DeviceLink()))
    monkeypatch.setattr(hardware, "printer", printer)
    monkeypatch.setattr(main, "printer", printer)
    monkeypatch.setattr(hardware, "print_in_progress", False)
    monkeypatch.setattr(hardware, "hold_action_in_progress", False)
    return printer


def test_offline_printer_is_skipped_not_reserved(offline_printer, caplog):
    assert hardware.try_begin_print_job() is False
    assert hardware.print_in_progress is False
    assert "unavailable" in caplog.text


def test_print_endpoints_return_503_when_offline(offline_printer):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.print_channel(1, main.BackgroundTasks()))
    assert exc.value.status_code == 503
    assert "esp32" in exc.value.detail


def test_offline_printer_does_not_degrade_health(offline_printer):
    health = asyncio.run(main.health_check())
    assert health["components"]["printer"] == "unavailable"
    assert health["components"]["printer_output"] == "esp32"
    assert health["components"]["relay_link"] in {"never_seen", "online", "offline", "unreachable"}
    assert "printer unavailable" not in health.get("issues", [])
    assert "gpio" not in health["components"]


def test_failed_delivery_is_logged_not_raised(caplog):
    from app.drivers.print_outputs import PrintOutput, PrinterTransportError

    class Failing(PrintOutput):
        mode = "failing"

        def print_receipt(self, img, *, feed_lines_after, cancel_event):
            raise PrinterTransportError("device went away")

    printer = RasterPrinter(output=Failing())
    printer.print_body("lost receipt")
    printer.flush_buffer()  # must not raise into the print job

    assert printer.last_transport_stats["error"] == "device went away"
    assert "Receipt not printed" in caplog.text
