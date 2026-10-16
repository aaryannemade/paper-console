"""USB serial output: strip framing, cancellation and write integrity."""

import threading

import pytest
from PIL import Image

from app.drivers import escpos
from app.drivers.print_outputs import PrinterTransportError, PrinterUnavailableError
from app.drivers.printer_raster import RasterPrinter
from app.drivers.usb_serial_output import UsbPrinterOutput, UsbSerialOutput
from app.drivers.usb_bulk_printer import UsbBulkPort, find_native_printer, printer_interface


class _FakeSerial:
    def __init__(self, *, max_write=None, fail_after=None, zero_progress=False):
        self.is_open = True
        self.max_write = max_write
        self.fail_after = fail_after
        self.zero_progress = zero_progress
        self.written = bytearray()
        self.write_calls = 0
        self.flush_calls = 0
        self.timeout = 1
        self.in_waiting = 0

    def write(self, data):
        self.write_calls += 1
        if self.zero_progress:
            return 0
        if self.fail_after is not None and len(self.written) >= self.fail_after:
            raise OSError("injected serial failure")

        raw = bytes(data)
        limit = len(raw)
        if self.max_write is not None:
            limit = min(limit, self.max_write)
        if self.fail_after is not None:
            limit = min(limit, self.fail_after - len(self.written))
        self.written.extend(raw[:limit])
        return limit

    def flush(self):
        self.flush_calls += 1

    def read(self, size=1):
        return b""

    def reset_output_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _make_output(fake_serial):
    output = UsbSerialOutput(port="/dev/fake-printer", open_now=False)
    output.ser = fake_serial
    output.wait_for_idle = lambda: None
    return output


def _make_driver(fake_serial):
    """A raster printer writing to a fake USB serial port."""
    return RasterPrinter(output=_make_output(fake_serial))


STRIP_BYTES = 8 + 48 * 24 + escpos.RASTER_GUARD_BYTES


def test_cancel_stops_only_between_complete_raster_strips():
    fake = _FakeSerial()
    output = _make_output(fake)
    cancel = threading.Event()
    original_flush = fake.flush

    def cancel_after_first_strip():
        original_flush()
        if fake.flush_calls == 1:
            cancel.set()

    fake.flush = cancel_after_first_strip

    stats = output.print_receipt(
        Image.new("1", (384, 72), 0), feed_lines_after=3, cancel_event=cancel
    )

    assert bytes(fake.written) == escpos.JOB_PREAMBLE + fake.written[2:]
    assert len(fake.written) == len(escpos.JOB_PREAMBLE) + STRIP_BYTES
    assert stats["cancelled"] is True
    assert stats["strips"] == 1
    assert stats["cancelled_after_strip"] == 1
    # A cancelled receipt gets no cutter feed.
    assert b"\x1b\x64" not in fake.written


def test_receipt_uses_guarded_24_row_strips_then_cutter_feed():
    fake = _FakeSerial()
    output = _make_output(fake)

    stats = output.print_receipt(
        Image.new("1", (384, 50), 1), feed_lines_after=2, cancel_event=threading.Event()
    )

    written = bytes(fake.written)
    cursor = len(escpos.JOB_PREAMBLE)
    assert written[:cursor] == escpos.JOB_PREAMBLE
    for height in (24, 24, 2):
        payload_size = 48 * height
        assert written[cursor : cursor + 8] == b"\x1d\x76\x30\x00" + bytes((48, 0, height, 0))
        cursor += 8
        assert written[cursor : cursor + payload_size] == b"\x00" * payload_size
        cursor += payload_size
        assert written[cursor : cursor + 4] == b"\x00" * 4
        cursor += 4

    assert written[cursor:] == escpos.feed_lines(2)
    assert stats["strips"] == 3
    assert stats["bytes_sent"] == len(written)
    assert stats["cancelled"] is False


def test_post_print_feed_is_single_command_with_tear_clearance():
    from app.config import DEFAULT_CUTTER_FEED_LINES

    assert DEFAULT_CUTTER_FEED_LINES == 5
    assert escpos.feed_lines(DEFAULT_CUTTER_FEED_LINES) == b"\x1b\x4a\x78"
    assert escpos.feed_lines(0) == b""


def test_writes_are_chunked_to_128_bytes():
    fake = _FakeSerial()
    sizes = []
    original_write = fake.write
    fake.write = lambda data: sizes.append(len(data)) or original_write(data)

    _make_output(fake).print_receipt(
        Image.new("1", (384, 24), 0), feed_lines_after=0, cancel_event=threading.Event()
    )

    assert max(sizes) <= UsbSerialOutput.SERIAL_WRITE_CHUNK_BYTES


def test_write_retries_partial_writes_until_every_byte_is_sent():
    fake = _FakeSerial(max_write=7)
    output = _make_output(fake)
    payload = bytes(range(100))

    assert output._write(payload) == len(payload)
    assert bytes(fake.written) == payload
    assert fake.write_calls > 1


def test_write_raises_when_serial_makes_no_progress():
    output = _make_output(_FakeSerial(zero_progress=True))

    with pytest.raises(PrinterTransportError, match="no progress"):
        output._write(b"receipt")


def test_failure_is_raised_and_connection_dropped_for_reopen():
    fake = _FakeSerial(fail_after=100)
    output = _make_output(fake)

    with pytest.raises(PrinterTransportError, match="Serial write failed"):
        output.print_receipt(
            Image.new("1", (384, 24), 0), feed_lines_after=0, cancel_event=threading.Event()
        )

    assert output.ser is None
    assert output.last_transport_stats["failed_strip"] == 1
    assert output.last_transport_stats["bytes_sent"] == 100


def test_open_failure_reports_unavailable(monkeypatch):
    def refuse(port, baudrate):
        raise OSError("no such device")

    output = UsbSerialOutput(port="/dev/missing", serial_factory=refuse)

    assert output.is_available() is False
    assert "no such device" in output.describe()["detail"]
    with pytest.raises(PrinterUnavailableError):
        output.print_receipt(
            Image.new("1", (384, 24), 1), feed_lines_after=0, cancel_event=threading.Event()
        )


def test_reconnects_when_adapter_returns(monkeypatch):
    fake = _FakeSerial()
    attempts = []

    def flaky(port, baudrate):
        attempts.append(port)
        if len(attempts) == 1:
            raise OSError("unplugged")
        return fake

    monkeypatch.setattr("app.drivers.usb_serial_output.time.sleep", lambda s: None)
    output = UsbSerialOutput(port="/dev/ttyUSB0", serial_factory=flaky)
    assert output.ser is None

    output.wait_for_idle = lambda: None
    stats = output.print_receipt(
        Image.new("1", (384, 24), 1), feed_lines_after=0, cancel_event=threading.Event()
    )

    assert stats["strips"] == 1
    # Opening sends the printer initialization before the receipt.
    assert bytes(fake.written).startswith(escpos.NUL * 5 + escpos.ESC_INIT)


def test_raster_printer_flush_delivers_cutter_feed_through_output():
    fake = _FakeSerial()
    driver = _make_driver(fake)
    driver.set_cutter_feed(4)
    driver.print_body("hello")

    driver.flush_buffer()

    assert bytes(fake.written).endswith(escpos.feed_lines(4))
    assert driver.last_transport_stats["strips"] >= 1


class _UsbEndpoint:
    bEndpointAddress = 0x03
    bmAttributes = 0x02


class _UsbInterface:
    bInterfaceClass = 7
    bInterfaceNumber = 0

    def __iter__(self):
        return iter([_UsbEndpoint()])


class _UsbConfiguration:
    bConfigurationValue = 1

    def __iter__(self):
        return iter([_UsbInterface()])


class _UsbDevice:
    idVendor = 0x0FE6
    idProduct = 0x811E

    def __init__(self):
        self.writes = []
        self.closed = False

    def __iter__(self):
        return iter([_UsbConfiguration()])

    def get_active_configuration(self):
        return _UsbConfiguration()

    def is_kernel_driver_active(self, interface):
        assert interface == 0
        return False

    def write(self, endpoint, data, timeout):
        assert endpoint == 0x03 and timeout == 5000
        self.writes.append(bytes(data))
        return len(data)


def test_native_usb_printer_discovery_and_bulk_writes(monkeypatch):
    import usb.core
    import usb.util

    device = _UsbDevice()
    monkeypatch.delenv("PC1_PRINTER_USB_VENDOR_ID", raising=False)
    monkeypatch.delenv("PC1_PRINTER_USB_PRODUCT_ID", raising=False)
    monkeypatch.setattr("app.drivers.usb_bulk_printer._backend", lambda: object())
    monkeypatch.setattr(usb.core, "find", lambda **kwargs: [device])
    monkeypatch.setattr(usb.util, "claim_interface", lambda dev, interface: None)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda dev: setattr(dev, "closed", True))

    assert printer_interface(device)[-1].bEndpointAddress == 0x03
    assert find_native_printer() is device
    port = UsbBulkPort(device)
    assert port.port == "usb:0fe6:811e"
    assert port.write(b"\x1d\x76\x30") == 3
    assert device.writes == [b"\x1d\x76\x30"]
    port.close()
    assert device.closed and not port.is_open


def test_native_usb_output_uses_serial_raster_framing(monkeypatch):
    fake = _FakeSerial()
    fake.port = "usb:0fe6:811e"
    output = UsbPrinterOutput(open_now=False, serial_factory=lambda port, baud: fake)
    output.wait_for_idle = lambda: None

    stats = output.print_receipt(
        Image.new("1", (384, 24), 0), feed_lines_after=2, cancel_event=threading.Event()
    )

    data = bytes(fake.written)
    assert data.startswith(escpos.NUL * 5 + escpos.ESC_INIT + escpos.ASCII_SETTINGS)
    assert escpos.encode_raster_strip(Image.new("1", (384, 24), 0)) in data
    assert data.endswith(escpos.feed_lines(2))
    assert stats["strips"] == 1
    assert output.describe()["port"] == "usb:0fe6:811e"
    assert "baudrate" not in output.describe()


def test_native_usb_access_error_does_not_fall_back_to_serial(monkeypatch):
    from app.drivers.print_outputs import create_output

    monkeypatch.delenv("PC1_PRINTER_PORT", raising=False)
    monkeypatch.setattr("app.drivers.usb_bulk_printer.find_native_printer", lambda: _UsbDevice())
    monkeypatch.setattr(
        "app.drivers.usb_bulk_printer.UsbBulkPort",
        lambda device: (_ for _ in ()).throw(PermissionError("access denied")),
    )
    output = create_output("usb")
    assert isinstance(output, UsbPrinterOutput)
    assert not output.is_available()
    assert "access denied" in output.describe()["detail"]


def test_native_usb_requires_explicit_ids_when_multiple_printers(monkeypatch):
    import usb.core

    monkeypatch.delenv("PC1_PRINTER_USB_VENDOR_ID", raising=False)
    monkeypatch.delenv("PC1_PRINTER_USB_PRODUCT_ID", raising=False)
    monkeypatch.setattr("app.drivers.usb_bulk_printer._backend", lambda: object())
    devices = [_UsbDevice(), _UsbDevice()]

    def discover(**filters):
        if filters.get("idVendor") == 0x0FE6 and filters.get("idProduct") == 0x811E:
            return devices[:1]
        return devices

    monkeypatch.setattr(usb.core, "find", discover)
    with pytest.raises(PrinterUnavailableError, match="Multiple USB printers"):
        find_native_printer()

    monkeypatch.setenv("PC1_PRINTER_USB_VENDOR_ID", "0fe6")
    monkeypatch.setenv("PC1_PRINTER_USB_PRODUCT_ID", "811e")
    assert find_native_printer() is devices[0]
