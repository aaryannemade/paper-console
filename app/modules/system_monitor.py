"""Receipt showing application and relay state rather than container-host metrics."""

import logging
import shutil
from typing import Any

from app.config import format_print_datetime
from app.module_registry import register_module
from app.paths import data_dir
from app.version import read_version

logger = logging.getLogger(__name__)


@register_module(
    type_id="system_monitor",
    label="System Monitor",
    description="Application version, printer output, ESP32 link and data storage",
    icon="desktop",
    offline=True,
    category="utilities",
    config_schema={"type": "object", "properties": {}},
)
def format_system_monitor_receipt(
    printer, config: dict[str, Any] | None = None, module_name: str | None = None
):
    """Print container-owned state; do not probe the host's hardware or network."""
    from app.relay_link import link

    printer.print_header(module_name or "APP STATUS", icon="desktop")
    printer.print_caption(format_print_datetime())
    printer.print_line()

    printer.print_subheader("APPLICATION")
    printer.print_body(f"Version: {read_version()}")
    printer.print_body(f"Output: {getattr(printer, 'output_mode', 'unknown')}")
    printer.print_body(f"Printer: {'ready' if printer.is_available() else 'unavailable'}")
    printer.print_line()

    status = link.snapshot()
    printer.print_subheader("ESP32 RELAY")
    printer.print_body(f"Link: {status['state']}")
    if status.get("device_id"):
        printer.print_caption(f"Device: {status['device_id']}")
    if status.get("heartbeat_age_s") is not None:
        printer.print_caption(f"Heartbeat: {status['heartbeat_age_s']:.0f}s ago")
    if status.get("last_job"):
        printer.print_caption(f"Last job: {status['last_job']['state']}")
    printer.print_line()

    try:
        total, used, free = shutil.disk_usage(data_dir())
        printer.print_subheader("APPLICATION DATA")
        printer.print_body(f"Free: {free / 2**30:.1f} GiB")
        printer.print_caption(f"Used: {used / 2**30:.1f} / {total / 2**30:.1f} GiB")
    except OSError:
        logger.debug("Data mount usage unavailable", exc_info=True)
        printer.print_caption("Data usage unavailable")
