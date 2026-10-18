"""Development output: a CSN-A2 on a USB-to-serial adapter.

The native USB printer-class interface uses UsbPrinterOutput below, sharing this
output's raster-strip writing. Not used in production (the ESP32 drives the
printer there). Wire a serial adapter's
TX to the printer's RX (and optionally RX to TX for status replies), at TTL level.
The printer's DTR busy line is not available over USB; pacing relies on the
128-byte writes and per-strip flushes described in readme.md ("Printer UART").

``PC1_PRINTER_PORT`` selects the port (default: first /dev/serial/by-id,
/dev/ttyUSB* or /dev/ttyACM* device). ``PC1_PRINTER_BAUDRATE`` defaults to 9600.
"""

import glob
import logging
import os
import threading
import time
from typing import Callable, Optional

from app.drivers import escpos
from app.drivers.print_outputs import (
    PrintOutput,
    PrinterTransportError,
    PrinterUnavailableError,
)

logger = logging.getLogger(__name__)

PORT_ENV = "PC1_PRINTER_PORT"
BAUDRATE_ENV = "PC1_PRINTER_BAUDRATE"
PORT_PATTERNS = ("/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*")


def detect_port() -> Optional[str]:
    for pattern in PORT_PATTERNS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


def _open_pyserial(port: str, baudrate: int):
    import serial

    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
        write_timeout=5,
    )


class UsbSerialOutput(PrintOutput):
    mode = "usb"

    SERIAL_WRITE_CHUNK_BYTES = 128
    REOPEN_INTERVAL_SECONDS = 5.0

    def __init__(
        self,
        port: Optional[str] = None,
        baudrate: int = 9600,
        *,
        serial_factory: Callable = _open_pyserial,
        open_now: bool = True,
    ):
        self.configured_port = port
        self.port = port
        self.baudrate = baudrate
        self._serial_factory = serial_factory
        self._io_lock = threading.RLock()
        self._last_open_attempt = 0.0
        self.ser = None
        self.last_error: Optional[str] = None
        self.last_transport_stats: dict = {}
        if open_now:
            self._try_open(force=True)

    @classmethod
    def from_env(cls) -> "UsbSerialOutput":
        port = os.environ.get(PORT_ENV, "").strip() or None
        raw_baud = os.environ.get(BAUDRATE_ENV, "").strip()
        try:
            baudrate = int(raw_baud) if raw_baud else 9600
        except ValueError as exc:
            raise ValueError(f"{BAUDRATE_ENV} must be an integer") from exc
        return cls(port=port, baudrate=baudrate)

    # --- connection -------------------------------------------------------

    def _try_open(self, *, force: bool = False) -> bool:
        if self.ser is not None and getattr(self.ser, "is_open", False):
            return True
        now = time.monotonic()
        if not force and (now - self._last_open_attempt) < self.REOPEN_INTERVAL_SECONDS:
            return False
        self._last_open_attempt = now

        port = self.configured_port or detect_port()
        if not port:
            self.last_error = "No USB serial port found (set PC1_PRINTER_PORT)"
            return False
        self.port = port
        try:
            self.ser = self._serial_factory(port, self.baudrate)
            self._initialize_printer()
        except Exception as exc:
            self._drop_connection()
            self.last_error = f"{port}: {exc}"
            logger.warning("USB printer unavailable: %s", self.last_error)
            return False
        self.last_error = None
        if self.baudrate:
            logger.info("USB-to-serial printer connected on %s at %d baud", port, self.baudrate)
        else:
            logger.info("Native USB printer connected on %s", getattr(self.ser, "port", port))
        return True

    def _drop_connection(self):
        ser, self.ser = self.ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def is_available(self) -> bool:
        with self._io_lock:
            return self._try_open()

    def describe(self) -> dict:
        info = super().describe()
        info.update({"port": self.port, "baudrate": self.baudrate})
        if self.last_error:
            info["detail"] = self.last_error
        return info

    def close(self) -> None:
        with self._io_lock:
            self._drop_connection()

    # --- low-level I/O ----------------------------------------------------

    def _write(self, data: bytes) -> int:
        """Write every byte or raise instead of silently accepting truncation."""
        if not data:
            return 0
        if self.ser is None or not getattr(self.ser, "is_open", False):
            raise PrinterUnavailableError("USB printer is not connected")
        total = 0
        view = memoryview(data)
        while total < len(data):
            try:
                written = self.ser.write(view[total:])
            except Exception as exc:
                raise PrinterTransportError(
                    f"Serial write failed after {total}/{len(data)} bytes",
                    bytes_written=total,
                ) from exc
            if not written or written < 0:
                raise PrinterTransportError(
                    f"Serial write made no progress at {total}/{len(data)} bytes",
                    bytes_written=total,
                )
            total += int(written)
        return total

    def _flush(self):
        try:
            self.ser.flush()
        except Exception as exc:
            raise PrinterTransportError("Serial flush failed") from exc

    def _read(self, size: int = 1, timeout: float = 1.0) -> bytes:
        try:
            old_timeout = self.ser.timeout
            self.ser.timeout = timeout
            try:
                return self.ser.read(size)
            finally:
                self.ser.timeout = old_timeout
        except Exception:
            return b""

    def _initialize_printer(self):
        if getattr(self.ser, "in_waiting", 0):
            self.ser.reset_input_buffer()
        if hasattr(self.ser, "reset_output_buffer"):
            self.ser.reset_output_buffer()
        time.sleep(0.5)
        self._write(escpos.NUL * 5)
        time.sleep(0.1)
        self._write(escpos.ESC_INIT)
        time.sleep(0.3)
        self._write(escpos.ASCII_SETTINGS)

    # --- PrintOutput ------------------------------------------------------

    def print_receipt(self, img, *, feed_lines_after, cancel_event):
        with self._io_lock:
            if not self._try_open(force=True):
                raise PrinterUnavailableError(self.last_error or "USB printer unavailable")
            try:
                stats = self._send_strips(img, cancel_event)
                if not stats["cancelled"]:
                    feed = escpos.feed_lines(feed_lines_after)
                    if feed:
                        time.sleep(0.05)
                        stats["bytes_sent"] += self._write(feed)
                        self._flush()
                    self.wait_for_idle()
            except PrinterTransportError:
                # Force a reopen next time; the adapter may have been unplugged.
                self._drop_connection()
                raise
            logger.info(
                "USB receipt queued: %dx%d strips=%d bytes=%d",
                stats["width"], stats["height"], stats["strips"], stats["bytes_sent"],
            )
            self.last_transport_stats = dict(stats)
            return stats

    def _send_strips(self, img, cancel_event: threading.Event) -> dict:
        img = escpos.pad_to_byte_width(img)
        width, height = img.size
        started = time.monotonic()
        stats = {
            "width": width,
            "height": height,
            "strips": 0,
            "bytes_sent": 0,
            "cancelled": False,
        }
        try:
            stats["bytes_sent"] += self._write(escpos.JOB_PREAMBLE)
            for index, command in enumerate(escpos.iter_raster_strips(img), start=1):
                # Stop only between complete GS v 0 commands so the printer never
                # waits for the rest of a raster payload.
                if cancel_event.is_set():
                    break
                for offset in range(0, len(command), self.SERIAL_WRITE_CHUNK_BYTES):
                    chunk = command[offset : offset + self.SERIAL_WRITE_CHUNK_BYTES]
                    stats["bytes_sent"] += self._write(chunk)
                # Waits for the host side only, not the print mechanism.
                self._flush()
                stats["strips"] = index
        except PrinterTransportError as exc:
            stats["bytes_sent"] += exc.bytes_written
            stats["failed_strip"] = stats["strips"] + 1
            stats["error"] = str(exc)
            self.last_transport_stats = dict(stats)
            logger.error("USB print failed at strip %d: %s", stats["failed_strip"], exc)
            raise

        if cancel_event.is_set():
            stats["cancelled"] = True
            stats["cancelled_after_strip"] = stats["strips"]
        stats["elapsed_seconds"] = time.monotonic() - started
        return stats

    def is_printer_busy(self) -> bool:
        """DLE EOT 1; False when the printer does not answer (RX not wired)."""
        try:
            self._write(escpos.STATUS_REQUEST)
        except PrinterTransportError:
            return False
        response = self._read(1, timeout=0.5)
        return bool(response) and escpos.is_offline_status(response[0])

    def wait_for_idle(self, timeout: float = 3.0, quiet_period: float = 0.35):
        """Short wait so back-to-back jobs do not overlap at the feed stage."""
        deadline = time.time() + max(timeout, quiet_period)
        time.sleep(min(0.15, quiet_period))
        busy_seen = False
        while time.time() < deadline:
            if self.is_printer_busy():
                busy_seen = True
                time.sleep(0.1)
                continue
            if busy_seen:
                time.sleep(quiet_period)
            else:
                time.sleep(max(0.0, min(quiet_period, deadline - time.time())))
            return

    def feed(self, dots: int) -> None:
        with self._io_lock:
            if not self._try_open():
                return
            try:
                self._write(escpos.feed_dots(dots))
                self._flush()
            except PrinterTransportError:
                logger.debug("USB feed failed", exc_info=True)
                self._drop_connection()

def _open_native_usb(_port: str, _baudrate: int):
    from app.drivers.usb_bulk_printer import UsbBulkPort, find_native_printer

    device = find_native_printer()
    if device is None:
        raise PrinterUnavailableError("No USB printer-class device found")
    return UsbBulkPort(device)


class UsbPrinterOutput(UsbSerialOutput):
    """Native USB printer-class output (bulk transfers, no UART baud rate)."""

    def __init__(self, *, open_now=True, serial_factory=_open_native_usb):
        super().__init__(
            port="native USB", baudrate=0, serial_factory=serial_factory, open_now=open_now
        )

    def _try_open(self, *, force: bool = False) -> bool:
        available = super()._try_open(force=force)
        if available:
            self.port = self.ser.port
        return available

    def describe(self) -> dict:
        info = super().describe()
        info.pop("baudrate", None)
        return info

    def is_printer_busy(self) -> bool:
        # Native USB printers need not support UART DLE EOT status replies;
        # don't inject status queries into the bulk stream after printing.
        return False
