"""Print outputs: where a rendered receipt goes.

The raster printer renders every receipt to one bitmap and hands it to an output:

* ``console`` — development: log a summary (the renderer also echoes ``[PRINT]``
  text lines) and optionally save each receipt as PNG.
* ``usb`` — development: a CSN-A2 on a USB-serial adapter (see usb_serial_output.py).
* ``esp32`` — production: relay to the ESP32-S3 over HTTP (esp32_relay_output.py).

Select with ``PC1_PRINTER_OUTPUT``; the default is ``esp32``.
"""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from app.drivers import escpos

logger = logging.getLogger(__name__)

OUTPUT_ENV = "PC1_PRINTER_OUTPUT"
CAPTURE_DIR_ENV = "PC1_PRINT_CAPTURE_DIR"
OUTPUT_MODES = ("esp32", "console", "usb")
DEFAULT_OUTPUT = "esp32"


class PrinterTransportError(RuntimeError):
    """Raised when a receipt cannot be delivered completely."""

    def __init__(self, message: str, *, bytes_written: int = 0):
        super().__init__(message)
        self.bytes_written = max(0, int(bytes_written))


class PrinterUnavailableError(PrinterTransportError):
    """Raised when the configured output cannot accept a receipt at all."""


class PrintOutput:
    """Interface shared by all outputs."""

    mode = "base"

    def is_available(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"mode": self.mode, "available": self.is_available()}

    def print_receipt(
        self,
        img: Image.Image,
        *,
        feed_lines_after: int,
        cancel_event: threading.Event,
    ) -> dict:
        """Deliver one rendered (already rotated) receipt and feed for the cutter.

        Returns transport stats; ``stats["cancelled"]`` is True when delivery
        stopped at a strip boundary because ``cancel_event`` was set.
        """
        raise NotImplementedError

    def feed(self, dots: int) -> None:
        """Feed paper outside a receipt (tactile feedback). Must not raise."""

    def reset(self) -> None:
        """Clear the printer's buffer/settings (server startup). Must not raise."""

    def close(self) -> None:
        pass


class ConsoleOutput(PrintOutput):
    """Logs receipts instead of printing them."""

    mode = "console"

    def __init__(self, capture_dir: Optional[str] = None):
        self.capture_dir = Path(capture_dir) if capture_dir else None
        self.receipts = 0

    def describe(self) -> dict:
        info = super().describe()
        if self.capture_dir:
            info["capture_dir"] = str(self.capture_dir)
        return info

    def print_receipt(self, img, *, feed_lines_after, cancel_event):
        strips = list(escpos.iter_raster_strips(img))
        command_bytes = len(escpos.JOB_PREAMBLE) + sum(len(s) for s in strips)
        command_bytes += len(escpos.feed_lines(feed_lines_after))
        self.receipts += 1
        saved = self._save(img)
        print(
            f"[PRINT] [RECEIPT] {img.width}x{img.height} dots, {len(strips)} strips, "
            f"{command_bytes} bytes" + (f", saved {saved}" if saved else ""),
            flush=True,
        )
        return {
            "width": img.width,
            "height": img.height,
            "strips": len(strips),
            "bytes_sent": command_bytes,
            "cancelled": False,
        }

    def _save(self, img: Image.Image) -> Optional[str]:
        if not self.capture_dir:
            return None
        try:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.capture_dir / f"receipt-{stamp}-{self.receipts:04d}.png"
            # Receipts are rendered upside down for the printer; save them upright.
            img.rotate(180).save(path)
            return str(path)
        except Exception:
            logger.warning("Could not save receipt capture", exc_info=True)
            return None


def resolve_output_mode(value: Optional[str] = None) -> str:
    mode = (value if value is not None else os.environ.get(OUTPUT_ENV, "")).strip().lower()
    if not mode:
        return DEFAULT_OUTPUT
    if mode not in OUTPUT_MODES:
        raise ValueError(
            f"{OUTPUT_ENV}={mode!r} is not supported; use one of: {', '.join(OUTPUT_MODES)}"
        )
    return mode


def create_output(mode: Optional[str] = None) -> PrintOutput:
    mode = resolve_output_mode(mode)
    if mode == "console":
        return ConsoleOutput(capture_dir=os.environ.get(CAPTURE_DIR_ENV, "").strip() or None)
    if mode == "usb":
        from app.drivers.usb_serial_output import UsbSerialOutput

        return UsbSerialOutput.from_env()
    from app.drivers.esp32_relay_output import Esp32RelayOutput

    return Esp32RelayOutput()
