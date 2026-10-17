import logging
import threading
import time

from app.config import PRINTER_WIDTH
from app.drivers.print_outputs import create_output
from app.drivers.printer_mock import PrinterDriver as TextEchoPrinter
from app.drivers.printer_raster import RasterPrinter
from app.device_input import ChannelSelector

logger = logging.getLogger(__name__)


def create_printer() -> RasterPrinter:
    """Build the receipt renderer for the output chosen by PC1_PRINTER_OUTPUT.

    Every output shares the same renderer. Console mode also echoes readable
    [PRINT] text lines. An unsupported value fails startup.
    """
    output = create_output()
    echo = TextEchoPrinter(width=PRINTER_WIDTH) if output.mode == "console" else None
    logger.info("Printer output: %s", output.mode)
    return RasterPrinter(width=PRINTER_WIDTH, output=output, echo=echo)


# Global instances. Physical inputs arrive as HTTP events from the ESP32
# (app/device_input.py); `dial` holds the last reported channel position.
printer = create_printer()
dial = ChannelSelector()


def printer_is_available() -> bool:
    """Best-effort check that the print output can accept a receipt."""
    if not printer:
        return False
    checker = getattr(printer, "is_available", None)
    if not callable(checker):
        return True
    try:
        return bool(checker())
    except Exception:
        logger.debug("Printer availability probe failed", exc_info=True)
        return False


# --- PRINT ORCHESTRATION ---
# Shared reservation state so any producer (device input, scheduler, email poll,
# webhooks, Slack, etc.) can claim the printer without racing the others.
print_lock = threading.Lock()
print_in_progress = False
hold_action_in_progress = False
hold_action_started_at = 0.0
last_print_time = 0.0
PRINT_DEBOUNCE_SECONDS = 3.0  # Minimum time between print jobs
HOLD_ACTION_TIMEOUT_SECONDS = 20.0


def printer_reserved_locked() -> bool:
    """Return True when the printer is reserved for a print or hold action."""
    return print_in_progress or hold_action_in_progress


def expire_stale_hold_action_locked(current_time: float):
    """Release a long-hold reservation if its matching release event was lost."""
    global hold_action_in_progress, hold_action_started_at

    if not hold_action_in_progress:
        return
    if (current_time - hold_action_started_at) < HOLD_ACTION_TIMEOUT_SECONDS:
        return

    hold_action_in_progress = False
    hold_action_started_at = 0.0
    logger.warning(
        "Cleared stale hold reservation after %.1fs without a matching release.",
        HOLD_ACTION_TIMEOUT_SECONDS,
    )


def try_begin_print_job(*, debounce: bool = False) -> bool:
    """Reserve the printer for a new print job."""
    global print_in_progress, last_print_time

    # Checked outside the lock: reopening a USB port can take a moment.
    if not printer_is_available():
        # Offline policy: skip and log; nothing is queued for later.
        logger.warning(
            "Print skipped: printer output %r is unavailable",
            getattr(printer, "output_mode", "unknown"),
        )
        return False

    with print_lock:
        current_time = time.time()
        expire_stale_hold_action_locked(current_time)

        if printer_reserved_locked():
            return False

        if debounce and (current_time - last_print_time) < PRINT_DEBOUNCE_SECONDS:
            return False

        print_in_progress = True
        last_print_time = current_time
        if hasattr(printer, "clear_cancel_request"):
            printer.clear_cancel_request()
        return True


def request_print_cancel() -> bool:
    """Cancel the active logical print job, if one is currently reserved."""
    with print_lock:
        if not print_in_progress:
            return False
        if not hasattr(printer, "request_cancel"):
            return False
        printer.request_cancel()
        logger.info("Print cancellation requested by the user")
        return True


def reserve_hold_action() -> bool:
    """Reserve the printer once the user crosses a long-hold threshold."""
    global hold_action_in_progress, hold_action_started_at, last_print_time

    with print_lock:
        current_time = time.time()
        expire_stale_hold_action_locked(current_time)

        if print_in_progress:
            return False

        hold_action_in_progress = True
        hold_action_started_at = current_time
        last_print_time = current_time
        return True


def promote_hold_to_print_job() -> bool:
    """Convert a hold reservation into an active print job."""
    global print_in_progress, hold_action_in_progress, hold_action_started_at, last_print_time

    with print_lock:
        current_time = time.time()
        expire_stale_hold_action_locked(current_time)

        if print_in_progress:
            return False

        hold_action_in_progress = False
        hold_action_started_at = 0.0
        print_in_progress = True
        last_print_time = current_time
        return True


def release_hold_action() -> bool:
    """Drop an armed hold reservation without printing; leaves prints alone."""
    global hold_action_in_progress, hold_action_started_at

    with print_lock:
        if not hold_action_in_progress:
            return False
        hold_action_in_progress = False
        hold_action_started_at = 0.0
        return True


def clear_print_reservation(*, clear_hold: bool = True):
    """Release active print/hold reservations."""
    global print_in_progress, hold_action_in_progress, hold_action_started_at, last_print_time

    with print_lock:
        if print_in_progress:
            # Start debounce from the end of the physical print window, not the start.
            last_print_time = time.time()
        print_in_progress = False
        if hasattr(printer, "clear_cancel_request"):
            printer.clear_cancel_request()
        if clear_hold:
            hold_action_in_progress = False
            hold_action_started_at = 0.0
