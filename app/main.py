from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from contextlib import asynccontextmanager
import asyncio
import uuid
import os
import hashlib
import json
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from pathlib import Path
from pydantic import BaseModel
import subprocess
import pytz
import logging
import time

# Configure logging
LOG_LEVEL_NAME = os.environ.get("PC1_LOG_LEVEL", "WARNING").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME, logging.WARNING)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),  # Console output
    ],
    force=True,
)

# Keep noisy dependency logs quiet on server deployments.
for noisy_logger in ("uvicorn.access", "httpx", "urllib3", "asyncio"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def _parse_cors_origins() -> List[str]:
    """
    Parse allowed CORS origins from env.
    Defaults to local/dev origins rather than wildcard.
    """
    raw = os.environ.get("PC1_CORS_ORIGINS", "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if "*" in origins:
            return ["*"]
        return origins
    return [
        "http://localhost",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",
    ]


from app.auth import (
    require_admin_access,
    get_admin_auth_status,
    verify_admin_password,
    set_admin_session_cookie,
    clear_admin_session_cookie,
)


from app.config import (
    settings,
    Settings,
    save_config,
    load_config,
    WebhookConfig,
    TextConfig,
    CalendarConfig,
    EmailConfig,
    ModuleInstance,
    ChannelModuleAssignment,
    ChannelConfig,
    ScheduleRule,
    PRINTER_WIDTH,
    current_datetime,
)
from app.schedule_utils import (
    cron_matches_minute,
    humanize_cron,
    legacy_schedule_to_rules,
    next_fire_times,
    normalize_schedule_rules,
)

# Import modules package to trigger auto-discovery and registration
import app.modules  # noqa: F401

# Import module registry for dynamic dispatch
from app.module_registry import (
    get_module as get_module_def,
    get_all_modules,
    list_module_types,
    is_registered,
    validate_module_config,
)

# Legacy imports for modules with special handling (can be removed after full migration)
from app.modules import email_client, webhook, text, calendar, weather
import app.print_webhook_service as print_webhook_service

import app.device_password as device_password
from app.paths import prepare_data_dir
import app.device_input as device_input
import app.relay_auth as relay_auth
import app.relay_link as relay_link
import app.hardware as hardware
from app.hardware import (
    printer,
    dial,
    try_begin_print_job,
    reserve_hold_action,
    promote_hold_to_print_job,
    request_print_cancel,
    clear_print_reservation,
)
import app.location_lookup as location_lookup
import app.slack_manager as slack_manager

# --- BACKGROUND TASKS ---

config_lock = asyncio.Lock()


async def save_settings_background(settings_snapshot: Settings):
    """Save settings to disk in background with a lock to prevent race conditions."""
    async with config_lock:
        await asyncio.to_thread(save_config, settings_snapshot)


email_polling_task = None
scheduler_task = None
task_monitor_task = None
WEATHER_PREFETCH_MIN_LEAD_SECONDS = 120
WEATHER_PREFETCH_MAX_LEAD_SECONDS = 240
WEATHER_PREFETCH_RETRY_SECONDS = 30
_weather_prefetch_state: Dict[str, Dict[str, object]] = {}


def _printer_is_available() -> bool:
    """Best-effort hardware printer availability check."""
    if not printer:
        return False
    checker = getattr(printer, "is_available", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            logger.debug("Printer availability probe failed", exc_info=True)
            return False
    # Mock drivers generally have no explicit availability method.
    return True


def _require_printer_available():
    """Reject a print request up front when the output cannot take it (no queue)."""
    if not _printer_is_available():
        mode = getattr(printer, "output_mode", "unknown")
        raise HTTPException(
            status_code=503, detail=f"Printer unavailable (output: {mode})"
        )


def _read_printer_status() -> Dict[str, object]:
    """Report logical job readiness without sending commands to the printer.

    Serial status queries take the same I/O lock held throughout a receipt.
    Polling it here would block the event loop, including remote cancellation.
    The reservation stays active through transport completion/cancellation.
    """
    with hardware.print_lock:
        hardware.expire_stale_hold_action_locked(time.time())
        print_in_progress = bool(hardware.print_in_progress)
        hold_action_in_progress = bool(hardware.hold_action_in_progress)

    available = _printer_is_available()
    if not available:
        reason = "unavailable"
    elif print_in_progress:
        reason = "printing"
    elif hold_action_in_progress:
        reason = "hold"
    else:
        reason = "idle"

    return {
        "ready": reason == "idle",
        "reason": reason,
    }


async def task_watchdog():
    """Monitor background tasks and restart them if they die unexpectedly."""
    global email_polling_task, scheduler_task

    while True:
        await asyncio.sleep(60)  # Check every minute

        # Silently restart any dead tasks
        if email_polling_task is not None and email_polling_task.done():
            email_polling_task = asyncio.create_task(email_polling_loop())

        if scheduler_task is not None and scheduler_task.done():
            scheduler_task = asyncio.create_task(scheduler_loop())

        # Safety net: retry errored Slack connections and pick up config drift.
        try:
            await slack_manager.reconcile()
        except Exception:
            logger.exception("Slack reconcile failed in watchdog")


async def email_polling_loop():
    """
    Polls for new emails from all email modules at their configured intervals.
    Uses the shortest polling interval among all active email modules.
    """
    while True:
        try:
            # Find all active email modules
            email_modules = []
            min_interval = None

            for module in settings.modules.values():
                if module.type == "email":
                    email_config = EmailConfig(**(module.config or {}))
                    if email_config.auto_print_new:
                        email_modules.append(module)
                        if min_interval is None or email_config.polling_interval < min_interval:
                            min_interval = email_config.polling_interval

            if not email_modules:
                await asyncio.sleep(60)
                continue

            await asyncio.sleep(max(1, min_interval or 60))

            # Check each email module silently
            for module in email_modules:
                try:
                    if not try_begin_print_job(debounce=False):
                        logger.info(
                            "Skipping auto-print email poll for module_id=%s because printer is busy or reserved.",
                            getattr(module, "id", "unknown"),
                        )
                        continue

                    # Run email fetching and printing in thread pool to avoid blocking event loop
                    from concurrent.futures import ThreadPoolExecutor

                    def _fetch_and_print_email():
                        # Fetch emails (blocking IMAP operation)
                        emails = email_client.fetch_emails(module.config)
                        if emails:
                            # Prepare printer for new job
                            if hasattr(printer, "reset_buffer"):
                                max_lines = getattr(settings, "max_print_lines", 200)
                                printer.reset_buffer(max_lines)

                            email_client.format_email_receipt(
                                printer,
                                messages=emails,
                                config=module.config,
                                module_name=module.name,
                            )

                            # Flush to hardware (spacing is built into bitmap)
                            if hasattr(printer, "flush_buffer"):
                                printer.flush_buffer()

                    loop = asyncio.get_event_loop()
                    with ThreadPoolExecutor() as executor:
                        await loop.run_in_executor(executor, _fetch_and_print_email)

                except Exception:
                    logger.exception(
                        "Email polling iteration failed for module_id=%s",
                        getattr(module, "id", "unknown"),
                    )
                finally:
                    clear_print_reservation(clear_hold=False)

        except Exception:
            logger.exception("Email polling loop encountered an unexpected error")
            await asyncio.sleep(60)  # Wait before retrying on error


def _iter_weather_modules_for_channel(channel: Optional[ChannelConfig]) -> List[ModuleInstance]:
    """Return non-interactive weather modules assigned to a channel."""
    if not channel or not getattr(channel, "modules", None):
        return []

    weather_modules: List[ModuleInstance] = []
    for assignment in channel.modules:
        module = settings.modules.get(assignment.module_id)
        if module and module.type == "weather":
            weather_modules.append(module)
    return weather_modules


def _channel_schedule_rules(channel: Optional[ChannelConfig]) -> List[ScheduleRule]:
    """Return active-format schedule rules for a channel.

    Prefers cron `schedule_rules`; falls back to converting any legacy
    HH:MM `schedule` entries so old configs keep working unchanged.
    """
    if not channel:
        return []

    rules = getattr(channel, "schedule_rules", None)
    if rules:
        return list(rules)

    legacy_schedule = getattr(channel, "schedule", None)
    if legacy_schedule:
        try:
            return [ScheduleRule(**rule) for rule in legacy_schedule_to_rules(legacy_schedule)]
        except ValueError:
            logger.warning("Ignoring invalid legacy HH:MM schedule payload", exc_info=True)

    return []


def _channel_enabled_expressions(channel: Optional[ChannelConfig]) -> List[str]:
    """Cron expressions for a channel's enabled schedule rules."""
    return [
        rule.expression
        for rule in _channel_schedule_rules(channel)
        if rule.enabled and rule.expression.strip()
    ]


def _schedule_preview_lines(channel: Optional[ChannelConfig], limit: int = 8) -> List[str]:
    """Human-readable schedule lines for printed receipts."""
    lines: List[str] = []
    for rule in _channel_schedule_rules(channel)[:limit]:
        label = humanize_cron(rule.expression, settings.time_format)
        if not rule.enabled:
            label += " (off)"
        lines.append(label)
    return lines


def _weather_prefetch_lead_seconds(module_id: str, scheduled_for: datetime) -> int:
    """Spread prefetch start times across the 2-4 minute warm-up window."""
    window = WEATHER_PREFETCH_MAX_LEAD_SECONDS - WEATHER_PREFETCH_MIN_LEAD_SECONDS
    if window <= 0:
        return WEATHER_PREFETCH_MIN_LEAD_SECONDS

    seed = f"{module_id}:{scheduled_for.strftime('%Y-%m-%dT%H:%M')}"
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:2], "big") % (window + 1)
    return WEATHER_PREFETCH_MIN_LEAD_SECONDS + offset


def _weather_prefetch_slot_key(module_id: str, scheduled_for: datetime) -> str:
    """Identify one weather module's prefetch work for one scheduled run."""
    return f"{module_id}:{scheduled_for.strftime('%Y-%m-%dT%H:%M')}"


def _cleanup_weather_prefetch_state(now: datetime) -> None:
    """Forget prefetch bookkeeping once a scheduled slot is well in the past."""
    stale_keys = [
        key
        for key, state in _weather_prefetch_state.items()
        if isinstance(state.get("scheduled_for"), datetime)
        and state["scheduled_for"] < (now - timedelta(minutes=15))
    ]
    for key in stale_keys:
        _weather_prefetch_state.pop(key, None)


async def _prefetch_weather_module_for_schedule(
    module: ModuleInstance, scheduled_for: datetime, slot_key: str
) -> None:
    """Warm weather data in a worker thread for an upcoming scheduled print."""
    state = _weather_prefetch_state.setdefault(slot_key, {})
    state["in_flight"] = True
    state["last_attempt_at"] = time.monotonic()

    try:
        result = await asyncio.to_thread(
            weather.prefetch_weather,
            module.config or {},
            module_id=module.id,
        )
        state["success"] = bool(result.get("ok", False))
        if not state["success"]:
            logger.warning(
                "Weather prefetch failed for module_id=%s schedule=%s error=%s",
                module.id,
                scheduled_for.isoformat(timespec="minutes"),
                result.get("error"),
            )
    except Exception:
        state["success"] = False
        logger.exception(
            "Weather prefetch crashed for module_id=%s schedule=%s",
            module.id,
            scheduled_for.isoformat(timespec="minutes"),
        )
    finally:
        state["in_flight"] = False


async def _run_weather_prefetch_cycle(now: datetime) -> None:
    """Warm weather modules that have scheduled prints approaching soon."""
    _cleanup_weather_prefetch_state(now)

    for channel in settings.channels.values():
        weather_modules = _iter_weather_modules_for_channel(channel)
        if not weather_modules:
            continue

        expressions = _channel_enabled_expressions(channel)
        if not expressions:
            continue

        for scheduled_for in next_fire_times(
            expressions, now, WEATHER_PREFETCH_MAX_LEAD_SECONDS
        ):
            seconds_until = (scheduled_for - now).total_seconds()
            if seconds_until <= 0:
                continue

            for module in weather_modules:
                slot_key = _weather_prefetch_slot_key(module.id, scheduled_for)
                state = _weather_prefetch_state.setdefault(
                    slot_key,
                    {
                        "scheduled_for": scheduled_for,
                        "in_flight": False,
                        "success": False,
                        "last_attempt_at": None,
                    },
                )
                lead_seconds = _weather_prefetch_lead_seconds(module.id, scheduled_for)
                prefetch_at = scheduled_for - timedelta(seconds=lead_seconds)
                if now < prefetch_at or state.get("success") or state.get("in_flight"):
                    continue

                last_attempt_at = state.get("last_attempt_at")
                if isinstance(last_attempt_at, (int, float)):
                    if (time.monotonic() - last_attempt_at) < WEATHER_PREFETCH_RETRY_SECONDS:
                        continue

                state["in_flight"] = True
                state["last_attempt_at"] = time.monotonic()
                asyncio.create_task(
                    _prefetch_weather_module_for_schedule(module, scheduled_for, slot_key)
                )


async def scheduler_loop():
    """
    Evaluate channel cron schedules once per minute.

    `current_datetime()` already returns time in the configured device
    timezone, so cron expressions are matched against local wall-clock time.
    """
    last_run_minute = ""

    while True:
        try:
            await asyncio.sleep(10)

            now = current_datetime()
            await _run_weather_prefetch_cycle(now)
            current_time = now.strftime("%Y-%m-%d %H:%M")

            # Evaluate each minute exactly once.
            if current_time == last_run_minute:
                continue

            last_run_minute = current_time

            for pos, channel in settings.channels.items():
                for expression in _channel_enabled_expressions(channel):
                    try:
                        is_due = cron_matches_minute(expression, now)
                    except Exception:
                        logger.warning(
                            "Skipping invalid cron rule for channel %s: '%s'",
                            pos,
                            expression,
                        )
                        continue

                    if not is_due:
                        continue

                    logger.info(
                        "Cron due for channel %s at %s: %s",
                        pos,
                        current_time,
                        expression,
                    )

                    if not try_begin_print_job(debounce=False):
                        logger.info(
                            "Skipping scheduled print for channel %s because printer is busy or reserved.",
                            pos,
                        )
                        continue

                    await trigger_channel(pos, scheduled=True)
                    break  # One print per channel per minute is enough.

        except Exception:
            logger.exception("Scheduler loop encountered an unexpected error")
            await asyncio.sleep(60)


# --- DEVICE INPUT GESTURES ---
#
# Called by app.device_input for button events from the ESP32. Each returns a
# short outcome string that is sent back to the device. Every gesture needs a
# reachable printer: the receipt is the device's only feedback.

# Print state tracking lives in app.hardware so any producer (device input,
# scheduler, email polling, webhooks, Slack) can share the same reservation.

global_loop = None


def _loop_ready() -> bool:
    return bool(global_loop and global_loop.is_running())


def handle_button_press() -> str:
    """Short press: choose in the active menu, otherwise print the current channel."""
    from app.selection_mode import can_handle_selection, is_selection_mode_active

    if not _printer_is_available():
        return "printer_unavailable"
    if not try_begin_print_job(debounce=True):
        return "busy"

    try:
        if not _loop_ready():
            clear_print_reservation(clear_hold=False)
            return "not_ready"
        if is_selection_mode_active():
            position = dial.read_position()
            accepted = can_handle_selection(position)
            asyncio.run_coroutine_threadsafe(handle_selection_async(position), global_loop)
            return "selection" if accepted else "selection_ignored"
        asyncio.run_coroutine_threadsafe(trigger_current_channel(), global_loop)
        return "printing_channel"
    except Exception:
        logger.exception("Failed to schedule button press")
        clear_print_reservation(clear_hold=False)
        return "error"


async def handle_selection_async(dial_position: int):
    """
    Async wrapper to handle selection mode input.
    Runs blocking operations in a thread pool.
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.selection_mode import can_handle_selection, handle_selection

    def _do_selection():
        # Tactile feedback only for active choices; empty slots stay silent.
        if can_handle_selection(dial_position) and hasattr(printer, "blip"):
            printer.blip()
        handle_selection(dial_position)

    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, _do_selection)
    finally:
        # Always mark print as complete
        clear_print_reservation(clear_hold=False)


def handle_button_hold() -> str:
    """5 s reached while held: cancel the active print, or arm Quick Actions."""
    # While a receipt is active, this gesture cancels it. The device input
    # processor then ignores the rest of this press (release / 15 s hold).
    if request_print_cancel():
        return "cancel_requested"
    if not _printer_is_available():
        return "printer_unavailable"
    if not reserve_hold_action():
        return "busy"
    try:
        # Half-line tactile feed cue: "you can release now".
        if hasattr(printer, "feed_dots"):
            printer.feed_dots(12)
        elif hasattr(printer, "blip"):
            printer.blip()
    except Exception:
        logger.debug("Hold tactile cue failed", exc_info=True)
    return "hold_ready"


def _begin_hold_action() -> Optional[str]:
    """Turn the armed hold into a print job; return an outcome on failure."""
    if not _printer_is_available():
        clear_print_reservation()
        return "printer_unavailable"
    if not promote_hold_to_print_job() and not try_begin_print_job(debounce=False):
        return "busy"
    if not _loop_ready():
        clear_print_reservation()
        return "not_ready"
    return None


def handle_button_hold_release() -> str:
    """Released between 5 and 15 s: print the Quick Actions menu."""
    failure = _begin_hold_action()
    if failure:
        return failure
    try:
        asyncio.run_coroutine_threadsafe(long_press_menu_trigger(), global_loop)
    except Exception:
        logger.exception("Failed to schedule Quick Actions")
        clear_print_reservation()
        return "error"
    return "quick_actions"


def _print_channel_config_summary(position: int):
    """Print a summary of the currently selected channel configuration."""
    if hasattr(printer, "reset_buffer"):
        max_lines = getattr(settings, "max_print_lines", 200)
        printer.reset_buffer(max_lines)

    printer.print_header(f"CHANNEL {position}", icon="list")
    printer.print_line()

    channel = settings.channels.get(position)
    if not channel or not channel.modules:
        printer.print_body("This channel is empty.")
        printer.print_caption("Use web settings to add modules.")
    else:
        sorted_assignments = sorted(channel.modules, key=lambda m: m.order)
        printer.print_caption("Assigned modules:")
        for idx, assignment in enumerate(sorted_assignments, start=1):
            module = settings.modules.get(assignment.module_id)
            module_name = module.name if module else "(missing module)"
            printer.print_body(f"  {idx}. {module_name}")

        schedule_lines = _schedule_preview_lines(channel, limit=8)
        if schedule_lines:
            printer.feed(1)
            printer.print_caption("Schedule:")
            for schedule_line in schedule_lines:
                printer.print_body(f"  - {schedule_line}")
            total_rules = len(_channel_schedule_rules(channel))
            if total_rules > len(schedule_lines):
                printer.print_caption(f"  +{total_rules - len(schedule_lines)} more")
        else:
            printer.feed(1)
            printer.print_caption("Schedule: none")

    printer.print_line()
    printer.print_caption("Open settings in your browser")
    printer.print_caption("to edit this channel.")
    printer.feed(1)

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


def _write_channel_overview_compact():
    """Write all channel assignments in a compact format (no reset/flush)."""
    printer.print_header("CHANNELS", icon="list")
    for channel_num in range(1, 9):
        channel = settings.channels.get(channel_num)
        if channel and channel.modules:
            names = []
            for assignment in sorted(channel.modules, key=lambda m: m.order):
                module = settings.modules.get(assignment.module_id)
                if module:
                    names.append(module.name)
            if names:
                printer.print_body(f"{channel_num}: {' + '.join(names)}")
            else:
                printer.print_caption(f"{channel_num}: (empty)")
        else:
            printer.print_caption(f"{channel_num}: (empty)")


def _write_long_press_menu_compact(position: int):
    """Write long-press quick action menu in compact format (no reset/flush)."""
    printer.print_header("QUICK ACTIONS", icon="settings")
    printer.print_caption(f"Dial: {position}")
    printer.print_body("[1] Table of contents")
    printer.print_body("[2] System monitor")
    printer.print_body("[5] Reset Factory Settings")
    printer.print_caption("[8] Cancel")
    printer.print_caption("Turn dial, press button")


def _print_system_monitor():
    """Print system monitor receipt (single-shot)."""
    if hasattr(printer, "reset_buffer"):
        max_lines = getattr(settings, "max_print_lines", 200)
        printer.reset_buffer(max_lines)
    from app.modules.system_monitor import format_system_monitor_receipt

    format_system_monitor_receipt(printer, {}, "SYSTEM MONITOR")
    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


def _print_current_channel_and_menu(position: int):
    """Print current channel details + quick menu in one print job."""
    if hasattr(printer, "reset_buffer"):
        max_lines = getattr(settings, "max_print_lines", 200)
        printer.reset_buffer(max_lines)
    channel = settings.channels.get(position)

    printer.print_header(f"CHANNEL {position}", icon="list")
    if not channel or not channel.modules:
        printer.print_body("This channel is empty.")
        printer.print_caption("Use web settings to add modules.")
    else:
        sorted_assignments = sorted(channel.modules, key=lambda m: m.order)
        for idx, assignment in enumerate(sorted_assignments, start=1):
            module = settings.modules.get(assignment.module_id)
            module_name = module.name if module else "(missing module)"
            printer.print_body(f"{idx}. {module_name}")
        schedule_lines = _schedule_preview_lines(channel, limit=2)
        if schedule_lines:
            printer.print_caption("Schedule: " + "; ".join(schedule_lines))
            total_rules = len(_channel_schedule_rules(channel))
            if total_rules > len(schedule_lines):
                printer.print_caption(f"+{total_rules - len(schedule_lines)} more rules")
        else:
            printer.print_caption("Schedule: none")

    _write_long_press_menu_compact(position)
    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


def _print_channel_overview():
    """Print all channel assignments (compact view)."""
    if hasattr(printer, "reset_buffer"):
        max_lines = getattr(settings, "max_print_lines", 200)
        printer.reset_buffer(max_lines)
    _write_channel_overview_compact()

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


def _print_long_press_menu(position: int):
    """Print long-press quick action menu."""
    if hasattr(printer, "reset_buffer"):
        max_lines = getattr(settings, "max_print_lines", 200)
        printer.reset_buffer(max_lines)
    _write_long_press_menu_compact(position)

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()


def _confirm_quick_factory_reset(printer, module_id: str):
    """Show the Quick Actions factory reset confirmation receipt."""
    from app.selection_mode import enter_selection_mode, exit_selection_mode

    printer.print_header("FACTORY RESET", icon="alert")
    printer.print_line()
    printer.print_bold("WARNING")
    printer.print_body("")
    printer.print_body("This will delete ALL")
    printer.print_body("application settings.")
    printer.print_body("")
    printer.print_body("Module configurations")
    printer.print_body("and channels")
    printer.print_body("will be erased.")
    printer.print_line()
    printer.print_subheader("ARE YOU SURE?")
    printer.feed(1)
    printer.print_body("  [1] Yes, Reset Everything")
    printer.print_caption("  [8] Cancel")
    printer.print_line()

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()

    def _handle_reset_confirmation(dial_position: int):
        from app.hardware import printer as hw_printer

        exit_selection_mode()
        if dial_position == 1:
            _do_quick_factory_reset(hw_printer)
            return

        if hasattr(hw_printer, "reset_buffer"):
            hw_printer.reset_buffer()

        hw_printer.print_header("QUICK ACTIONS", icon="settings")
        hw_printer.print_body("Factory reset cancelled.")
        hw_printer.print_line()
        hw_printer.feed(1)

        if hasattr(hw_printer, "flush_buffer"):
            hw_printer.flush_buffer()

    enter_selection_mode(
        _handle_reset_confirmation,
        f"{module_id}-reset-confirm",
        valid_positions={1, 8},
    )


def _reset_application_settings():
    """Reset persisted and live settings without changing the host or credentials."""
    global settings
    from app.factory_reset import perform_factory_reset
    import app.config as config_module

    result = perform_factory_reset()
    if result["config_cleared"]:
        settings = config_module.settings
        if global_loop and global_loop.is_running():
            asyncio.run_coroutine_threadsafe(slack_manager.reconcile(), global_loop)
    return result


def _do_quick_factory_reset(printer):
    """Perform a confirmed factory reset from Quick Actions."""

    printer.print_header("RESETTING...", icon="alert")
    printer.print_line()
    printer.print_body("Clearing all settings...")
    printer.print_body("Server will stay running.")
    printer.print_line()
    printer.feed(1)

    if hasattr(printer, "flush_buffer"):
        printer.flush_buffer()

    result = _reset_application_settings()
    if not result.get("config_cleared", False):
        logger.error(
            "Application settings reset failed: %s",
            "; ".join(result.get("errors", [])),
        )


async def long_press_menu_trigger():
    """Long-press flow: open quick action menu and enter selection mode."""
    from concurrent.futures import ThreadPoolExecutor
    from app.selection_mode import enter_selection_mode, exit_selection_mode

    # Cancel any existing interactive mode before opening quick actions
    exit_selection_mode()

    position = dial.read_position()

    def _initial_print():
        # Print quick actions menu only (no auto-TOC print).
        _print_long_press_menu(position)

    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, _initial_print)
    except Exception:
        logger.exception("Failed to render long-press menu")
        clear_print_reservation(clear_hold=False)
        return

    def _handle_quick_action(dial_position: int):
        from app.selection_mode import exit_selection_mode
        from app.hardware import printer as hw_printer

        if dial_position == 8:
            exit_selection_mode()
            if hasattr(hw_printer, "reset_buffer"):
                hw_printer.reset_buffer()
            hw_printer.print_header("CANCELLED", icon="x")
            hw_printer.print_line()
            hw_printer.feed(1)
            if hasattr(hw_printer, "flush_buffer"):
                hw_printer.flush_buffer()
            return

        if dial_position == 1:
            exit_selection_mode()
            _print_channel_overview()
            return

        if dial_position == 2:
            exit_selection_mode()
            _print_system_monitor()
            return

        if dial_position == 5:
            exit_selection_mode()
            try:
                _confirm_quick_factory_reset(hw_printer, "quick-factory-reset")
            except Exception:
                logger.exception("Failed to open factory reset confirmation")
            return

        # Empty dial slots are ignored to avoid wasting paper.
        return

    quick_actions_id = f"quick-actions-{position}"
    enter_selection_mode(
        _handle_quick_action,
        quick_actions_id,
        valid_positions={1, 2, 5, 8},
    )
    # Release lock only after quick-actions selection mode is fully active.
    clear_print_reservation(clear_hold=False)

    async def _auto_exit_quick_actions_after_timeout(module_id: str):
        """Auto-exit quick actions if no selection is made within timeout."""
        from app.selection_mode import is_selection_mode_active, get_current_module_id

        await asyncio.sleep(120)

        if not is_selection_mode_active():
            return
        if get_current_module_id() != module_id:
            return

        # Still in this quick-actions session after timeout: exit with a brief hint.
        exit_selection_mode()
        from app.hardware import printer as hw_printer

        if hasattr(hw_printer, "reset_buffer"):
            max_lines = getattr(settings, "max_print_lines", 200)
            hw_printer.reset_buffer(max_lines)
        hw_printer.print_header("QUICK ACTIONS", icon="settings")
        hw_printer.print_caption("No action selected.")
        hw_printer.print_caption("Menu timed out after 2 min.")
        hw_printer.feed(1)
        if hasattr(hw_printer, "flush_buffer"):
            hw_printer.flush_buffer()

    asyncio.create_task(_auto_exit_quick_actions_after_timeout(quick_actions_id))


async def factory_reset_trigger():
    """Reset application settings (button hold 15+ seconds).
    Runs blocking printer operations in a thread pool to avoid blocking the event loop.
    """
    from concurrent.futures import ThreadPoolExecutor
    from app.selection_mode import exit_selection_mode

    exit_selection_mode()

    def _print_reset_message():
        # Start from a clean transport state before sending reset notice.
        if hasattr(printer, "clear_hardware_buffer"):
            printer.clear_hardware_buffer()
        # Reset printer buffer
        if hasattr(printer, "reset_buffer"):
            printer.reset_buffer()

        # Print factory reset message
        printer.feed(1)
        printer.print_text("=" * 32)
        printer.print_text("")
        printer.print_text("     FACTORY RESET")
        printer.print_text("")
        printer.print_text("  All settings will be")
        printer.print_text("  cleared. Server will")
        printer.print_text("  stay running.")
        printer.print_text("")
        printer.print_text("=" * 32)
        printer.feed(2)

        # Flush buffer to print (spacing is built into bitmap)
        if hasattr(printer, "flush_buffer"):
            printer.flush_buffer()

    try:
        # Run blocking printer operations in thread pool to avoid blocking event loop
        try:
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                await loop.run_in_executor(executor, _print_reset_message)
        except Exception:
            logger.warning(
                "Factory reset receipt failed to print",
                exc_info=True,
            )  # Continue reset flow even if print fails

        # Wait for print to complete
        await asyncio.sleep(3)

        reset_result = _reset_application_settings()
        if not reset_result.get("config_cleared", False):
            logger.error(
                "Application settings reset failed: %s",
                "; ".join(reset_result.get("errors", [])),
            )
    finally:
        clear_print_reservation()


def handle_button_long_hold() -> str:
    """15 s reached while held: reset application settings."""
    failure = _begin_hold_action()
    if failure:
        if failure == "busy":
            logger.info("Ignoring factory reset hold because printer is already busy.")
        return failure
    try:
        asyncio.run_coroutine_threadsafe(factory_reset_trigger(), global_loop)
    except Exception:
        logger.exception("Failed to schedule factory reset")
        clear_print_reservation()
        return "error"
    return "factory_reset"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global email_polling_task, scheduler_task, task_monitor_task, global_loop
    from concurrent.futures import ThreadPoolExecutor

    # Capture the running loop
    global_loop = asyncio.get_running_loop()

    # Fail fast if the persistent bind mount is missing or not writable.
    prepare_data_dir()

    # Fail fast on a misconfigured password, and log a generated one at startup.
    device_password.get_device_password()
    logger.info(
        "Settings password source: %s", device_password.get_device_password_source()
    )
    # Fail fast on a misconfigured relay token; without one the device API is off.
    if relay_auth.get_relay_token() is None:
        logger.warning(
            "%s is not set; device input events are disabled.", relay_auth.TOKEN_ENV
        )

    # Clear printer hardware buffer first to prevent startup garbage
    # Run in thread pool to avoid blocking event loop
    def _init_printer():
        if hasattr(printer, "clear_hardware_buffer"):
            printer.clear_hardware_buffer()

    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, _init_printer)
    except Exception:
        logger.exception(
            "Printer startup initialization failed (continuing without blocking startup)"
        )
    if not _printer_is_available():
        logger.warning(
            "Printer output %r is unavailable at startup; prints are skipped until it is reachable.",
            getattr(printer, "output_mode", "unknown"),
        )

    # The server does not provision host networking or print on restart.
    # WiFi onboarding belongs to the ESP32; printing must be explicitly requested
    # by an input/API action or an application producer such as the scheduler.

    # Start background tasks
    email_polling_task = asyncio.create_task(email_polling_loop())
    scheduler_task = asyncio.create_task(scheduler_loop())
    task_monitor_task = asyncio.create_task(task_watchdog())

    # Connect Slack workspaces for any configured Slack modules.
    slack_manager.init_runtime(trigger_channel)
    asyncio.create_task(slack_manager.reconcile())

    # Physical inputs arrive as HTTP events from the ESP32 (POST /api/device/events).
    # Lambdas resolve the handlers at call time.
    device_input.processor.reset()
    device_input.init_runtime(
        dial,
        press=lambda: handle_button_press(),
        hold=lambda: handle_button_hold(),
        hold_release=lambda: handle_button_hold_release(),
        long_hold=lambda: handle_button_long_hold(),
    )

    yield

    # Shutdown - cancel all background tasks
    for task in [email_polling_task, scheduler_task, task_monitor_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background task shutdown failed")

    try:
        await slack_manager.shutdown()
    except Exception:
        logger.exception("Slack shutdown failed")

    # Release the print output (closes a USB serial port)
    if hasattr(printer, "close"):
        printer.close()


app = FastAPI(
    title="PC-1 (Paper Console 1)",
    description="Backend API for the PC-1 offline news printer.",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow Frontend (React) to talk to Backend
app.include_router(device_input.router)
app.include_router(relay_link.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CORE API ---


@app.get("/api/system/status")
async def read_root():
    pos = dial.read_position()
    channel = settings.channels.get(pos)
    module_info = "unknown"
    if channel:
        if channel.modules:
            module_info = f"{len(channel.modules)} module(s)"
        elif channel.type:
            module_info = channel.type

    return {
        "status": "online",
        "app": "PC-1",
        "version": "v1.0",
        "configured_timezone": settings.timezone,
        "current_channel": pos,
        "current_module": module_info,
    }


@app.get("/api/printer/status")
async def get_printer_status():
    """Return advisory job readiness, not physical health or a job reservation."""
    return _read_printer_status()


class AuthLoginRequest(BaseModel):
    password: str
    remember: bool = False


@app.get("/api/system/auth/status")
async def get_auth_status(request: Request):
    """Return privileged endpoint auth mode for UI guidance."""
    return get_admin_auth_status(request)


@app.post("/api/system/auth/login")
async def login_auth(request: Request, payload: AuthLoginRequest, response: Response):
    """Authenticate a settings session and set a signed cookie."""
    if not verify_admin_password(payload.password):
        raise HTTPException(
            status_code=401,
            detail="Invalid server password.",
            headers={"X-PC1-Auth-Required": "true"},
        )

    set_admin_session_cookie(
        response,
        remember=bool(payload.remember),
        secure=request.url.scheme == "https",
    )
    auth_status = get_admin_auth_status()
    auth_status["authenticated"] = True
    return {
        "message": "Authenticated",
        "auth": auth_status,
    }


@app.post("/api/system/auth/logout")
async def logout_auth(request: Request, response: Response):
    """Clear any saved settings session cookie."""
    clear_admin_session_cookie(response, secure=request.url.scheme == "https")
    return {"message": "Logged out"}


@app.get("/api/health")
async def health_check():
    """
    Health check endpoint for monitoring.
    Returns detailed status of all system components.
    """
    import os

    health = {"status": "healthy", "components": {}}

    # Check background tasks
    health["components"]["email_polling"] = (
        "running" if email_polling_task and not email_polling_task.done() else "stopped"
    )
    health["components"]["scheduler"] = (
        "running" if scheduler_task and not scheduler_task.done() else "stopped"
    )
    health["components"]["task_monitor"] = (
        "running" if task_monitor_task and not task_monitor_task.done() else "stopped"
    )

    # Per-module Slack connection status (only present when Slack modules exist)
    slack_statuses = slack_manager.get_statuses()
    if slack_statuses:
        health["components"]["slack"] = {
            module_id: status.get("status", "unknown")
            for module_id, status in slack_statuses.items()
        }

    # Check hardware drivers
    health["components"]["printer"] = (
        "available" if _printer_is_available() else "unavailable"
    )
    health["components"]["printer_output"] = getattr(printer, "output_mode", "unknown")
    health["components"]["device_input"] = (
        "enabled" if relay_auth.relay_token_source() != "unset" else "disabled"
    )
    health["components"]["relay_link"] = relay_link.link.snapshot()["state"]

    # Check config file
    import app.config as config_module

    config_path = config_module.get_config_dir() / "config.json"
    health["components"]["config"] = (
        "exists" if os.path.exists(config_path) else "missing"
    )

    # Overall status
    critical_issues = []
    if health["components"]["email_polling"] == "stopped":
        critical_issues.append("email_polling stopped")
    if health["components"]["scheduler"] == "stopped":
        critical_issues.append("scheduler stopped")
    # An unreachable printer (e.g. the ESP32 is offline) is reported above but is
    # not a server health failure; prints are skipped until it returns.

    if critical_issues:
        health["status"] = "degraded"
        health["issues"] = critical_issues

    return health


@app.get("/status")
async def get_status():
    """Returns the current state of the device."""
    pos = dial.read_position()
    channel = settings.channels.get(pos)
    module_info = "unknown"
    if channel:
        if channel.modules:
            module_info = f"{len(channel.modules)} module(s)"
        elif channel.type:
            module_info = channel.type

    return {"state": "idle", "current_channel": pos, "current_module": module_info}


# --- SETTINGS API ---


@app.get(
    "/api/settings",
    response_model=Settings,
    dependencies=[Depends(require_admin_access)],
)
async def get_settings():
    return settings


def _normalize_imported_settings_data(data: dict) -> dict:
    normalized = dict(data or {})

    channels = normalized.get("channels", {})
    if isinstance(channels, dict):
        normalized_channels = {}
        for key, value in channels.items():
            try:
                normalized_channels[int(key)] = value
            except (ValueError, TypeError):
                normalized_channels[key] = value
        normalized["channels"] = normalized_channels

    return normalized


def _collect_settings_validation_warnings(new_settings: Settings) -> List[str]:
    validation_warnings: List[str] = []

    for module_id, module in new_settings.modules.items():
        if module_id == "unassigned":
            continue

        try:
            validate_module_config(module.type, module.config or {})
        except ValueError as exc:
            warning = f"{module_id} ({module.type}): {exc}"
            validation_warnings.append(warning)
            logging.warning("Validation warning: %s", warning)

    return validation_warnings


@app.post("/api/settings", dependencies=[Depends(require_admin_access)])
async def update_settings(new_settings: Settings, background_tasks: BackgroundTasks):
    """Updates the configuration and saves it to disk."""
    global settings
    import app.config as config_module
    # Update in-memory - create new settings object
    settings = new_settings

    validation_warnings = _collect_settings_validation_warnings(settings)

    # Update module-level reference so modules that access app.config.settings will see the update
    config_module.settings = settings

    # Save to disk
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))

    # Reconnect/disconnect Slack workspaces if Slack modules changed.
    background_tasks.add_task(slack_manager.reconcile)

    response = {"message": "Settings saved", "config": settings}
    if validation_warnings:
        response["validation_warnings"] = validation_warnings
    return response


@app.get("/api/settings/export", dependencies=[Depends(require_admin_access)])
async def export_settings():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(
        settings.model_dump(exclude_unset=False),
        indent=4,
    )
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="pc1-config-{timestamp}.json"',
        },
    )


@app.post("/api/settings/import", dependencies=[Depends(require_admin_access)])
async def import_settings(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Import settings from a previously exported JSON config payload."""
    global settings
    import app.config as config_module

    try:
        raw_bytes = await request.body()
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Config file must be valid UTF-8 JSON") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Config file is not valid JSON") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Config file must contain a JSON object")

    try:
        imported_settings = Settings(**_normalize_imported_settings_data(data))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid config file: {exc}") from exc

    settings = imported_settings
    config_module.settings = settings
    validation_warnings = _collect_settings_validation_warnings(settings)
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))
    background_tasks.add_task(slack_manager.reconcile)

    response = {
        "message": "Configuration imported",
        "config": settings,
    }
    if validation_warnings:
        response["validation_warnings"] = validation_warnings
    return response


@app.post("/api/settings/reset", dependencies=[Depends(require_admin_access)])
async def reset_settings(background_tasks: BackgroundTasks):
    """Resets all settings to their default values."""
    result = await asyncio.to_thread(_reset_application_settings)
    if not result["config_cleared"]:
        raise HTTPException(status_code=500, detail="Could not reset application settings")
    return {"message": "Settings reset to defaults", "config": settings, "warnings": result["errors"]}


@app.post("/api/settings/reload", dependencies=[Depends(require_admin_access)])
async def reload_settings():
    """Reloads settings from config.json on disk."""
    global settings
    import app.config as config_module

    new_settings = load_config()

    # Update in-memory globals
    settings = new_settings
    config_module.settings = settings

    asyncio.create_task(slack_manager.reconcile())

    return {"message": "Settings reloaded from disk", "config": settings}


# --- SYSTEM TIME API ---


def _read_system_timezone_info() -> Dict[str, Optional[str] | bool]:
    """Best-effort lookup of the container's timezone (no host tools)."""
    try:
        timezone = os.environ.get("TZ")
        if timezone:
            return {
                "timezone": timezone,
                "found": True,
            }

        try:
            with open("/etc/timezone", encoding="utf-8") as f:
                timezone = f.read().strip()
                if timezone:
                    return {"timezone": timezone, "found": True}
        except OSError:
            logger.debug("Container timezone file unavailable", exc_info=True)

        return {
            "timezone": None,
            "found": False,
            "message": "Could not detect system timezone",
        }
    except Exception as e:
        return {
            "timezone": None,
            "found": False,
            "error": str(e),
        }


@app.get("/api/system/timezone")
async def get_system_timezone():
    """
    Get the current system timezone.
    """
    return _read_system_timezone_info()


@app.get("/api/system/timezone/list")
async def list_timezones():
    """
    Get a list of available timezones.
    Returns common US timezones and a few others.
    """
    # Common timezones organized by region
    timezones = {
        "US Eastern": [
            "America/New_York",
            "America/Detroit",
            "America/Indiana/Indianapolis",
            "America/Indiana/Vevay",
            "America/Indiana/Vincennes",
            "America/Indiana/Winamac",
            "America/Kentucky/Louisville",
            "America/Kentucky/Monticello",
        ],
        "US Central": [
            "America/Chicago",
            "America/Indiana/Knox",
            "America/Indiana/Tell_City",
            "America/Menominee",
            "America/North_Dakota/Center",
            "America/North_Dakota/New_Salem",
            "America/North_Dakota/Beulah",
        ],
        "US Mountain": [
            "America/Denver",
            "America/Boise",
        ],
        "US Arizona": [
            "America/Phoenix",
        ],
        "US Pacific": [
            "America/Los_Angeles",
            "America/Anchorage",
            "America/Juneau",
            "America/Sitka",
            "America/Metlakatla",
            "America/Yakutat",
            "America/Nome",
        ],
        "US Territories": [
            "Pacific/Honolulu",
            "America/Puerto_Rico",
            "America/St_Thomas",
            "Pacific/Guam",
            "Pacific/Saipan",
            "Pacific/Pago_Pago",
        ],
        "Other": [
            "UTC",
            "Europe/London",
            "Europe/Paris",
            "Asia/Tokyo",
            "Australia/Sydney",
        ],
    }

    # Flatten into a simple list with labels
    timezone_list = []
    for region, tz_list in timezones.items():
        for tz in tz_list:
            timezone_list.append({"value": tz, "label": tz, "region": region})

    return {"timezones": timezone_list}


@app.get("/api/system/time")
async def get_system_time():
    """
    Get the current system time and date.
    Returns time in the configured timezone and format.
    """
    try:
        from app.config import format_time

        tz = pytz.timezone(settings.timezone)
        now = datetime.now(tz)

        # Format time according to user's preference
        time_formatted = format_time(now, settings.time_format)
        date_str = now.strftime("%Y-%m-%d")

        # Format full datetime string with date and formatted time
        formatted = f"{date_str} {time_formatted}"

        return {
            "datetime": now.isoformat(),
            "date": date_str,
            "time": now.strftime("%H:%M:%S"),
            "timezone": settings.timezone,
            "formatted": formatted,
            "time_formatted": time_formatted,
        }
    except Exception as e:
        # Fallback to UTC if timezone is invalid
        from app.config import format_time

        now = datetime.now(pytz.UTC)
        time_formatted = format_time(now, settings.time_format)
        date_str = now.strftime("%Y-%m-%d")
        formatted = f"{date_str} {time_formatted}"

        return {
            "datetime": now.isoformat(),
            "date": date_str,
            "time": now.strftime("%H:%M:%S"),
            "timezone": "UTC",
            "formatted": formatted,
            "time_formatted": time_formatted,
            "error": str(e),
        }


# --- VERSION ---


def _get_project_root() -> Path:
    """Return the project root directory (parent of app/)."""
    return Path(__file__).parent.parent


@app.get("/api/system/version")
async def get_current_version():
    """Report the running version (``.version`` file, else short git commit).

    Updates are delivered by replacing the container image, not installed in place.
    """
    project_root = _get_project_root()
    version_file = project_root / ".version"
    try:
        version_text = version_file.read_text(encoding="utf-8").strip()
        if version_text:
            return {"version": version_text}
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"version": result.stdout.strip()}
    except (OSError, subprocess.SubprocessError):
        logger.debug("git version lookup failed", exc_info=True)
    return {"version": "unknown"}


# --- LOCATION SEARCH API ---


@app.get("/api/location/system-default")
async def get_system_default_location():
    """
    Suggest a location based on the container's timezone.
    """
    try:
        system_timezone = _read_system_timezone_info()["timezone"]

        if not system_timezone:
            return {
                "found": False,
                "message": "Could not detect system timezone",
            }

        # Find a default location for this timezone
        # Use major cities in each timezone as defaults
        timezone_to_default_location = {
            "America/New_York": {"city": "New York", "state": "NY", "zipcode": "10001"},
            "America/Chicago": {"city": "Chicago", "state": "IL", "zipcode": "60601"},
            "America/Denver": {"city": "Denver", "state": "CO", "zipcode": "80201"},
            "America/Phoenix": {"city": "Phoenix", "state": "AZ", "zipcode": "85001"},
            "America/Los_Angeles": {
                "city": "Los Angeles",
                "state": "CA",
                "zipcode": "90001",
            },
            "America/Anchorage": {
                "city": "Anchorage",
                "state": "AK",
                "zipcode": "99501",
            },
            "Pacific/Honolulu": {"city": "Honolulu", "state": "HI", "zipcode": "96801"},
            "America/Puerto_Rico": {
                "city": "San Juan",
                "state": "PR",
                "zipcode": "00901",
            },
        }

        # Try exact match first
        default = timezone_to_default_location.get(system_timezone)

        # If no exact match, try to find any location in the timezone
        if not default:
            # Search for locations in the timezone by looking up a common city
            # We'll use the timezone name to infer a location
            timezone_parts = system_timezone.split("/")
            if len(timezone_parts) >= 2:
                location_name = timezone_parts[-1].replace("_", " ")
                # Try searching for the location name
                results = location_lookup.search_locations(location_name, limit=1)
                if results:
                    result = results[0]
                    return {
                        "found": True,
                        "timezone": system_timezone,
                        "location": {
                            "name": result["name"],
                            "city": result["city"],
                            "state": result["state"],
                            "zipcode": result["zipcode"],
                            "latitude": result["latitude"],
                            "longitude": result["longitude"],
                            "timezone": result["timezone"],
                        },
                    }

            return {
                "found": False,
                "timezone": system_timezone,
                "message": f"Found timezone {system_timezone} but no default location available",
            }

        # Get full location details for the default
        location_result = location_lookup.get_location_by_zip(default["zipcode"])
        if location_result:
            return {
                "found": True,
                "timezone": system_timezone,
                "location": {
                    "name": location_result["name"],
                    "city": location_result["city"],
                    "state": location_result["state"],
                    "zipcode": location_result["zipcode"],
                    "latitude": location_result["latitude"],
                    "longitude": location_result["longitude"],
                    "timezone": location_result["timezone"],
                },
            }
        else:
            # Fallback: create location from default data
            state = default["state"]

            # Import the constants from location_lookup
            from app.location_lookup import STATE_TO_TIMEZONE, STATE_COORDINATES

            # Get timezone and coordinates from state
            timezone = STATE_TO_TIMEZONE.get(state, "America/New_York")
            coords = STATE_COORDINATES.get(state, (40.7128, -74.0060))
            lat, lon = coords

            return {
                "found": True,
                "timezone": system_timezone,
                "location": {
                    "name": default["city"],
                    "city": default["city"],
                    "state": state,
                    "zipcode": default["zipcode"],
                    "latitude": lat,
                    "longitude": lon,
                    "timezone": timezone,
                },
            }

    except Exception as e:
        return {
            "found": False,
            "error": str(e),
            "message": "Error detecting system location",
        }


@app.get("/api/location/search")
async def search_location(q: str, limit: int = 20, use_api: Optional[str] = None):
    """
    Search for locations by city name using the local GeoNames database.
    Returns location data with timezone and coordinates.
    """
    if not q or len(q.strip()) < 2:
        return {"results": []}

    query = q.strip()
    results = []

    # Check if API is requested (default to False - always use local database)
    use_api_enabled = False
    if use_api is not None:
        use_api_enabled = (
            use_api.lower() == "true" if isinstance(use_api, str) else bool(use_api)
        )

    # Try API first if explicitly requested (for future use or testing)
    if use_api_enabled:
        try:
            import requests
            import time
            from concurrent.futures import ThreadPoolExecutor

            # Use Nominatim API (free, no API key required)
            # Rate limit: 1 request per second - we'll respect this
            nominatim_url = "https://nominatim.openstreetmap.org/search"
            params = {
                "q": query,
                "format": "json",
                "limit": limit,
                "addressdetails": 1,
                "countrycodes": "us",  # Focus on US locations
            }
            headers = {
                "User-Agent": "Paper-Console/1.0 (PC-1 Thermal Printer)",  # Required by Nominatim
            }

            # Run blocking network request in thread pool to avoid blocking event loop
            def _fetch_location_api():
                return requests.get(
                    nominatim_url, params=params, headers=headers, timeout=3
                )

            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                response = await loop.run_in_executor(executor, _fetch_location_api)

            if response.status_code == 200:
                api_results = response.json()
                if api_results:
                    # Convert Nominatim results to our format
                    for item in api_results:
                        address = item.get("address", {})
                        city = (
                            address.get("city")
                            or address.get("town")
                            or address.get("village")
                            or address.get("municipality")
                            or ""
                        )
                        # Nominatim provides state_code (2-letter) or state (full name)
                        state = address.get("state_code", "") or address.get(
                            "state", ""
                        )
                        zipcode = address.get("postcode", "")

                        # Convert full state name to abbreviation if needed
                        if state and len(state) > 2:
                            # Map common full state names to abbreviations
                            state_name_to_code = {
                                "Alabama": "AL",
                                "Alaska": "AK",
                                "Arizona": "AZ",
                                "Arkansas": "AR",
                                "California": "CA",
                                "Colorado": "CO",
                                "Connecticut": "CT",
                                "Delaware": "DE",
                                "Florida": "FL",
                                "Georgia": "GA",
                                "Hawaii": "HI",
                                "Idaho": "ID",
                                "Illinois": "IL",
                                "Indiana": "IN",
                                "Iowa": "IA",
                                "Kansas": "KS",
                                "Kentucky": "KY",
                                "Louisiana": "LA",
                                "Maine": "ME",
                                "Maryland": "MD",
                                "Massachusetts": "MA",
                                "Michigan": "MI",
                                "Minnesota": "MN",
                                "Mississippi": "MS",
                                "Missouri": "MO",
                                "Montana": "MT",
                                "Nebraska": "NE",
                                "Nevada": "NV",
                                "New Hampshire": "NH",
                                "New Jersey": "NJ",
                                "New Mexico": "NM",
                                "New York": "NY",
                                "North Carolina": "NC",
                                "North Dakota": "ND",
                                "Ohio": "OH",
                                "Oklahoma": "OK",
                                "Oregon": "OR",
                                "Pennsylvania": "PA",
                                "Rhode Island": "RI",
                                "South Carolina": "SC",
                                "South Dakota": "SD",
                                "Tennessee": "TN",
                                "Texas": "TX",
                                "Utah": "UT",
                                "Vermont": "VT",
                                "Virginia": "VA",
                                "Washington": "WA",
                                "West Virginia": "WV",
                                "Wisconsin": "WI",
                                "Wyoming": "WY",
                                "District of Columbia": "DC",
                                "Puerto Rico": "PR",
                                "Virgin Islands": "VI",
                                "Guam": "GU",
                                "American Samoa": "AS",
                                "Northern Mariana Islands": "MP",
                            }
                            state = state_name_to_code.get(
                                state, state[:2].upper() if len(state) >= 2 else ""
                            )

                        # Only include US locations with city and valid 2-letter state code
                        if city and state and len(state) == 2:
                            # Get timezone from state
                            from app.location_lookup import (
                                STATE_TO_TIMEZONE,
                                STATE_COORDINATES,
                            )

                            timezone = STATE_TO_TIMEZONE.get(state, "America/New_York")
                            coords = STATE_COORDINATES.get(state, (40.7128, -74.0060))

                            # Use actual coordinates from API
                            lat = float(item.get("lat", coords[0]))
                            lon = float(item.get("lon", coords[1]))

                            display_name = f"{city}, {state}"
                            if zipcode:
                                display_name = f"{city}, {state} {zipcode}"

                            result = {
                                "id": f"{zipcode}-{city}-{state}-{item.get('place_id', '')}",
                                "name": display_name,
                                "zipcode": zipcode,
                                "city": city,
                                "state": state,
                                "latitude": lat,
                                "longitude": lon,
                                "timezone": timezone,
                            }
                            results.append(result)

                            if len(results) >= limit:
                                break

                    if results:
                        return {"results": results, "source": "api"}
        except Exception as e:
            # API failed, fall back to local database
            print(f"Location API search failed: {e}, falling back to local database")

    # Fall back to local database (works offline)
    local_results = location_lookup.search_locations(query, limit=limit)
    if local_results:
        return {"results": local_results, "source": "local"}

    return {"results": [], "source": "none"}


# --- MODULE MANAGEMENT API ---


@app.get("/api/module-types")
async def get_module_types():
    """
    Returns all available module types from the registry.

    This endpoint enables the frontend to dynamically discover what module
    types are available without hardcoding them. Each module type includes:
    - id: The module type identifier (e.g., "weather", "quotes")
    - label: Human-readable name for the UI
    - description: Brief description of what the module does
    - icon: Icon name for the UI
    - offline: Whether the module works without internet
    - category: Grouping category for UI organization
    - configSchema: JSON schema for config form generation (optional)
    """
    return {"moduleTypes": list_module_types()}


@app.get("/api/modules", dependencies=[Depends(require_admin_access)])
async def list_modules():
    """List all module instances."""
    return {"modules": settings.modules}


def _normalize_text_module_config(module: ModuleInstance) -> None:
    """Ensure text modules use content_doc and strip legacy markdown content."""
    if module.type != "text":
        return

    config = module.config if isinstance(module.config, dict) else {}

    content_doc = config.get("content_doc")
    is_valid_doc = (
        isinstance(content_doc, dict)
        and content_doc.get("type") == "doc"
        and isinstance(content_doc.get("content"), list)
    )

    if not is_valid_doc:
        legacy_content = config.get("content")
        if isinstance(legacy_content, str) and legacy_content:
            lines = legacy_content.split("\n")
            paragraphs = []
            for line in lines:
                if line.strip():
                    paragraphs.append(
                        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                    )
                else:
                    paragraphs.append({"type": "paragraph"})
            config["content_doc"] = {
                "type": "doc",
                "content": paragraphs or [{"type": "paragraph"}],
            }
        else:
            config["content_doc"] = {"type": "doc", "content": [{"type": "paragraph"}]}

    config.pop("content", None)
    module.config = config

def _convert_and_resize_image_module_config(module: ModuleInstance) -> None:
    """Compact uploaded image data before storing it in config.json."""
    if module.type != "image" or not module.config:
        return

    image_data = module.config.get("image_data")
    if isinstance(image_data, str) and image_data.startswith("data:"):
        from app.modules.print_image import resize_and_convert_image

        module.config["image_data"] = resize_and_convert_image(image_data)


@app.post("/api/modules", dependencies=[Depends(require_admin_access)])
async def create_module(module: ModuleInstance, background_tasks: BackgroundTasks):
    """Create a new module instance."""
    global settings

    # If no ID provided, generate one
    if not module.id:
        module.id = str(uuid.uuid4())

    _normalize_text_module_config(module)
    print_webhook_service.normalize_module_config(
        settings.modules,
        module,
        generate_token_if_missing=True,
    )
    _convert_and_resize_image_module_config(module)
    print_webhook_service.validate_endpoint_uniqueness(
        settings.modules,
        module.id,
        module,
    )
    settings.modules[module.id] = module
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))
    background_tasks.add_task(slack_manager.reconcile)

    return {"message": "Module created", "module": module}


@app.get("/api/modules/{module_id}", dependencies=[Depends(require_admin_access)])
async def get_module(module_id: str):
    """Get a specific module instance."""
    module = settings.modules.get(module_id)
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")
    return module


@app.put("/api/modules/{module_id}", dependencies=[Depends(require_admin_access)])
async def update_module(
    module_id: str, module: ModuleInstance, background_tasks: BackgroundTasks
):
    """Update a module instance."""
    global settings
    import app.config as config_module

    if module_id not in settings.modules:
        raise HTTPException(status_code=404, detail="Module not found")

    # Ensure ID matches
    module.id = module_id
    _normalize_text_module_config(module)
    print_webhook_service.normalize_module_config(settings.modules, module)
    _convert_and_resize_image_module_config(module)
    print_webhook_service.validate_endpoint_uniqueness(
        settings.modules,
        module_id,
        module,
    )
    settings.modules[module_id] = module
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))

    # Update module-level reference so modules that access app.config.settings will see the update
    config_module.settings = settings

    background_tasks.add_task(slack_manager.reconcile)

    return {"message": "Module updated", "module": module}


@app.delete("/api/modules/{module_id}", dependencies=[Depends(require_admin_access)])
async def delete_module(module_id: str, background_tasks: BackgroundTasks):
    """Delete a module instance."""
    global settings

    if module_id not in settings.modules:
        raise HTTPException(status_code=404, detail="Module not found")

    # Check if module is assigned to any channels
    assigned_channels = []
    for pos, channel in settings.channels.items():
        if channel.modules:
            for assignment in channel.modules:
                if assignment.module_id == module_id:
                    assigned_channels.append(pos)

    if assigned_channels:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete module: it is assigned to channels {assigned_channels}. Remove it from channels first.",
        )

    del settings.modules[module_id]
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))
    background_tasks.add_task(slack_manager.reconcile)

    return {"message": "Module deleted"}


@app.post(
    "/api/modules/{module_id}/actions/{action}",
    dependencies=[Depends(require_admin_access)],
)
async def execute_module_action(module_id: str, action: str):
    """
    Execute a module-specific action.

    Supported actions depend on the module type.
    """
    if module_id not in settings.modules:
        raise HTTPException(status_code=404, detail="Module not found")

    module = settings.modules[module_id]

    # Add other module actions here as needed

    raise HTTPException(
        status_code=400,
        detail=f"Unknown action '{action}' for module type '{module.type}'",
    )


@app.post("/api/channels/{position}/modules", dependencies=[Depends(require_admin_access)])
async def assign_module_to_channel(
    position: int,
    module_id: str,
    background_tasks: BackgroundTasks,
    order: Optional[int] = None,
):
    """Assign a module to a channel."""
    global settings

    if position < 1 or position > 8:
        raise HTTPException(status_code=400, detail="Position must be 1-8")

    if module_id not in settings.modules:
        raise HTTPException(status_code=404, detail="Module not found")

    # Get or create channel config
    if position not in settings.channels:
        settings.channels[position] = ChannelConfig(modules=[])

    channel = settings.channels[position]

    # Ensure modules list exists
    if not channel.modules:
        channel.modules = []

    # Check if module is already assigned
    if any(a.module_id == module_id for a in channel.modules):
        raise HTTPException(
            status_code=400, detail="Module already assigned to this channel"
        )

    # Determine order (default to end)
    if order is None:
        order = max([a.order for a in channel.modules], default=-1) + 1

    # Add assignment
    channel.modules.append(ChannelModuleAssignment(module_id=module_id, order=order))
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))

    return {"message": "Module assigned to channel", "channel": channel}


@app.delete(
    "/api/channels/{position}/modules/{module_id}",
    dependencies=[Depends(require_admin_access)],
)
async def remove_module_from_channel(
    position: int, module_id: str, background_tasks: BackgroundTasks
):
    """Remove a module from a channel."""
    global settings

    if position not in settings.channels:
        raise HTTPException(status_code=404, detail="Channel not found")

    channel = settings.channels[position]

    if not channel.modules:
        raise HTTPException(
            status_code=404, detail="Module not assigned to this channel"
        )

    # Remove assignment
    channel.modules = [a for a in channel.modules if a.module_id != module_id]
    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))

    return {"message": "Module removed from channel", "channel": channel}


@app.post(
    "/api/channels/{position}/modules/reorder",
    dependencies=[Depends(require_admin_access)],
)
async def reorder_channel_modules(
    position: int, module_orders: Dict[str, int], background_tasks: BackgroundTasks
):
    """Reorder modules within a channel. module_orders is {module_id: new_order}."""
    global settings

    if position not in settings.channels:
        raise HTTPException(status_code=404, detail="Channel not found")

    channel = settings.channels[position]

    if not channel.modules:
        raise HTTPException(status_code=400, detail="Channel has no modules")

    # Update orders
    for assignment in channel.modules:
        if assignment.module_id in module_orders:
            assignment.order = module_orders[assignment.module_id]

    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))

    return {"message": "Modules reordered", "channel": channel}


@app.post(
    "/api/channels/{position}/schedule",
    dependencies=[Depends(require_admin_access)],
)
async def update_channel_schedule(
    position: int,
    background_tasks: BackgroundTasks,
    payload: object = Body(...),
):
    """Update the print schedule for a channel.

    Accepted payloads:
    - Current: {"rules": [{"expression": "30 8 * * 1-5", "enabled": true}]}
    - Legacy:  ["08:30", "18:00"] (HH:MM list, converted to cron rules)
    """
    global settings

    if position not in settings.channels:
        settings.channels[position] = ChannelConfig(modules=[])

    channel = settings.channels[position]

    try:
        if isinstance(payload, list):
            times = [str(item).strip() for item in payload if str(item).strip()]
            rule_payloads = legacy_schedule_to_rules(sorted(set(times)))
        elif isinstance(payload, dict):
            rules_value = payload.get("rules", [])
            if not isinstance(rules_value, list):
                raise ValueError("'rules' must be an array")
            rule_payloads = normalize_schedule_rules(
                [dict(rule) for rule in rules_value]
            )
        else:
            raise ValueError("Schedule payload must be an array or object")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    channel.schedule_rules = [ScheduleRule(**rule) for rule in rule_payloads]
    # Legacy HH:MM list is superseded once cron rules are written.
    channel.schedule = []

    background_tasks.add_task(save_settings_background, settings.model_copy(deep=True))

    return {
        "message": "Schedule updated",
        "channel": channel,
        "schedule_preview": _schedule_preview_lines(channel, limit=8),
    }


# --- EVENT ROUTER ---


def execute_module(module: ModuleInstance, *, scheduled: bool = False) -> bool:
    """
    Execute a single module instance using the module registry.

    Returns True if successful, False if failed.
    """
    module_type = module.type
    config = module.config or {}
    module_name = module.name or module_type.upper()

    # Handle disabled modules
    if module_type == "off":
        return True  # Disabled modules are "successful" (intentionally empty)

    try:
        # Special handling for modules with Pydantic config classes
        # These require config to be parsed into specific types before calling
        if module_type == "webhook":
            action_config = WebhookConfig(**config)
            webhook.run_webhook(action_config, printer, module_name)
            return True

        elif module_type == "text":
            text_config = TextConfig(**config)
            text.format_text_receipt(printer, text_config, module_name)
            return True

        elif module_type == "calendar":
            cal_config = CalendarConfig(**config)
            calendar.format_calendar_receipt(printer, cal_config, module_name)
            return True

        elif module_type == "email":
            # Email has special handling - fetch emails first, then format
            emails = email_client.fetch_emails(config)
            email_client.format_email_receipt(
                printer, messages=emails, config=config, module_name=module_name
            )
            return True

        # Use registry for all other modules
        module_def = get_module_def(module_type)
        if module_def:
            import inspect

            # Most modules use (printer, config, module_name). Interactive
            # modules can opt into the real instance ID for per-module state.
            params = inspect.signature(module_def.execute_fn).parameters
            accepts_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            execute_kwargs = {}
            if "module_id" in params or accepts_kwargs:
                execute_kwargs["module_id"] = module.id
            if "scheduled" in params or accepts_kwargs:
                execute_kwargs["scheduled"] = scheduled
            if execute_kwargs:
                module_def.execute_fn(
                    printer,
                    config,
                    module_name,
                    **execute_kwargs,
                )
            else:
                module_def.execute_fn(printer, config, module_name)
            return True
        else:
            # Unknown module type
            printer.print_text(f"{module_name}")
            printer.print_line()
            printer.print_text("This module type is not")
            printer.print_text("recognized. Please check")
            printer.print_text("your settings.")
            return False

    except Exception as e:
        # Log the error for debugging
        logging.error(f"Error executing module '{module_type}': {e}")
        # Print a friendly error message
        printer.print_text(f"{module_name}")
        printer.print_line()
        printer.print_text("Could not load this content.")
        printer.print_text("Please check your settings")
        printer.print_text("or try again later.")
        return False


async def trigger_channel(position: int, *, scheduled: bool = False):
    """
    Executes all modules assigned to a specific channel position.
    Runs blocking printer operations in a thread pool to avoid blocking the event loop.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from app.selection_mode import exit_selection_mode

    # Cancel any active interactive mode when a new channel is triggered
    exit_selection_mode()

    def _do_print():
        """Synchronous function that does the actual printing work."""
        # Instant tactile feedback - tiny paper blip (2 dots, ~0.01")
        if hasattr(printer, "blip"):
            printer.blip()

        # Reset printer buffer at start of print job (for invert mode)
        # Also set max lines limit
        max_lines = getattr(settings, "max_print_lines", 200)
        if hasattr(printer, "reset_buffer"):
            printer.reset_buffer(max_lines)

        channel = settings.channels.get(position)

        # Handle empty or unconfigured channels
        if not channel or not channel.modules:
            printer.print_text(f"CHANNEL {position}")
            printer.print_line()
            printer.print_text("This channel is empty.")
            printer.print_text("")
            printer.print_text("Open settings in your browser")
            printer.print_text("to set it up.")
            printer.feed(1)

            # Flush buffer to print
            if hasattr(printer, "flush_buffer"):
                printer.flush_buffer()
            return

        # Sort modules by order
        sorted_modules = sorted(channel.modules, key=lambda m: m.order)

        # Separate standard and interactive modules
        from app.module_registry import get_module

        standard_modules = []
        interactive_modules = []

        for assignment in sorted_modules:
            module = settings.modules.get(assignment.module_id)
            if module:
                module_def = get_module(module.type)
                if module_def and getattr(module_def, "interactive", False):
                    interactive_modules.append(module)
                else:
                    standard_modules.append(module)

        # 1. Execute all standard modules first
        for module in standard_modules:
            execute_module(module, scheduled=scheduled)

            # Separator between modules (unless it's the last standard one)
            if module != standard_modules[-1]:
                printer.feed(2)

            # Check for max lines exceeded
            if (
                hasattr(printer, "is_max_lines_exceeded")
                and printer.is_max_lines_exceeded()
            ):
                if hasattr(printer, "flush_buffer"):
                    printer.flush_buffer()
                printer.print_text("")
                printer.print_text("--- MAX LENGTH REACHED ---")
                printer.feed(1)

                # Flush again for the message
                if hasattr(printer, "flush_buffer"):
                    printer.flush_buffer()
                return

        # 2. Handle Interactive Modules
        if not interactive_modules:
            # Done
            pass

        elif len(interactive_modules) == 1:
            # Single interactive module - run it directly
            if standard_modules:
                printer.feed(2)  # Separator from previous content
            execute_module(interactive_modules[0], scheduled=scheduled)

        else:
            # Multiple interactive modules - show selection menu
            if standard_modules:
                printer.feed(2)

            printer.print_header("SELECT APP", icon="list")
            printer.print_line()

            # Print menu options
            for i, module in enumerate(interactive_modules, 1):
                # Ensure we don't exceed 7 options (8 is reserved for Exit)
                if i > 7:
                    break
                printer.print_body(f"[{i}] {module.name}")

            printer.feed(1)
            printer.print_caption("[8] Cancel")
            printer.print_line()
            printer.print_caption("Turn dial to select")
            printer.feed(1)

            if hasattr(printer, "flush_buffer"):
                printer.flush_buffer()

            # Enter special selection mode to choose the app
            from app.selection_mode import enter_selection_mode, exit_selection_mode

            def handle_app_selection(dial_position: int):
                # Position 8 = Cancel
                if dial_position == 8:
                    exit_selection_mode()
                    # Print cancellation message
                    from app.hardware import printer as hw_printer

                    if hasattr(hw_printer, "reset_buffer"):
                        hw_printer.reset_buffer()
                    hw_printer.print_header("CANCELLED", icon="x")
                    hw_printer.print_line()
                    hw_printer.feed(1)
                    if hasattr(hw_printer, "flush_buffer"):
                        hw_printer.flush_buffer()
                    return

                # Check valid selection index (1-based)
                idx = dial_position - 1
                if 0 <= idx < len(interactive_modules):
                    # Valid selection - Exit menu selection mode first
                    exit_selection_mode()

                    # Execute the chosen module
                    # This module will then likely enter its OWN selection mode
                    target_module = interactive_modules[idx]

                    # We need to run this on the main thread via execute_module,
                    # but we are currently in the selection callback (potentially thread pool).
                    # Actually, execute_module just queues prints or runs logic.
                    # The tricky part is the adventure module expects to be called to PRINT content
                    # and enters its OWN selection mode.

                    # We can directly call execute_module from here.
                    # IMPORTANT: The printer buffer needs reset? Maybe.
                    from app.hardware import printer as hw_printer

                    if hasattr(hw_printer, "reset_buffer"):
                        hw_printer.reset_buffer()

                    execute_module(target_module, scheduled=scheduled)

                    if hasattr(hw_printer, "flush_buffer"):
                        hw_printer.flush_buffer()

                else:
                    # Invalid selection
                    # Ideally we'd re-print or just beep, but for now we do nothing
                    pass

            # Register the callback
            # We use a special ID to indicate this is the channel meta-menu
            valid_positions = set(range(1, min(len(interactive_modules), 7) + 1))
            valid_positions.add(8)
            enter_selection_mode(
                handle_app_selection,
                f"channel-menu-{position}",
                valid_positions=valid_positions,
            )

            # Return early since we've handled the printing/logic
            return

        if hasattr(printer, "flush_buffer"):
            printer.flush_buffer()

        # Flush buffer to actually print. The printer driver applies a fixed
        # post-print cutter feed after each job.
        if hasattr(printer, "flush_buffer"):
            printer.flush_buffer()

    try:
        # Run blocking printer operations in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, _do_print)
    finally:
        # Always mark print as complete (thread-safe)
        clear_print_reservation(clear_hold=False)


async def trigger_current_channel():
    """
    The Core Logic:
    Reads the dial position and executes all modules assigned to that channel.
    """
    position = dial.read_position()
    await trigger_channel(position)


@app.post("/action/trigger", dependencies=[Depends(require_admin_access)])
async def manual_trigger():
    """Print the channel at the current position (like a device button press)."""
    _require_printer_available()
    if not try_begin_print_job(debounce=False):
        raise HTTPException(status_code=409, detail="Print already in progress")

    try:
        await trigger_current_channel()
    finally:
        clear_print_reservation(clear_hold=False)
    return {"message": "Triggered"}


@app.post("/debug/cancel-print", dependencies=[Depends(require_admin_access)])
async def debug_cancel_print():
    """Request the same safe cancellation used by the five-second button hold."""
    if not request_print_cancel():
        raise HTTPException(status_code=409, detail="No print is currently in progress")
    return {"message": "Print cancellation requested"}


@app.post("/action/dial/{position}", dependencies=[Depends(require_admin_access)])
async def set_dial(position: int):
    """Set the channel position (normally reported by the ESP32)."""
    if position < 1 or position > 8:
        raise HTTPException(status_code=400, detail="Position must be 1-8")

    dial.set_position(position)
    return {"message": f"Dial turned to {position}"}


@app.post(
    "/action/print-channel/{position}",
    dependencies=[Depends(require_admin_access)],
)
async def print_channel(position: int, background_tasks: BackgroundTasks):
    """Set dial position and trigger print atomically. Returns immediately while print runs in background."""
    logger = logging.getLogger(__name__)

    if position < 1 or position > 8:
        raise HTTPException(status_code=400, detail="Position must be 1-8")

    _require_printer_available()
    if not try_begin_print_job(debounce=False):
        raise HTTPException(status_code=409, detail="Print already in progress")

    # Run print in background and return immediately
    # trigger_channel handles errors and clears print_in_progress in its finally block
    background_tasks.add_task(trigger_channel, position)
    return {"message": f"Printing channel {position}"}


# --- DEBUG / VIRTUAL HARDWARE CONTROLS ---


@app.post(
    "/debug/print-module/{module_id}",
    dependencies=[Depends(require_admin_access)],
)
async def print_module(module_id: str, background_tasks: BackgroundTasks):
    """Forces a specific module instance to print (for testing)."""
    module = settings.modules.get(module_id)
    if not module:
        raise HTTPException(status_code=404, detail="Module not found")

    _require_printer_available()
    if not try_begin_print_job(debounce=False):
        raise HTTPException(status_code=409, detail="Print already in progress")

    # Run print in background and return immediately
    # print_module_direct handles errors and clears print_in_progress in its finally block
    background_tasks.add_task(print_module_direct, module_id)
    return {"message": f"Printing module '{module.name}'"}


async def print_module_direct(module_id: str):
    """Internal function to print a single module with proper buffer setup.
    Runs blocking printer operations in a thread pool to avoid blocking the event loop.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from app.selection_mode import exit_selection_mode

    # Cancel any active interactive mode
    exit_selection_mode()

    def _do_print():
        """Synchronous function that does the actual printing work."""
        module = settings.modules.get(module_id)
        if not module:
            return

        # Instant tactile feedback - tiny paper blip (2 dots, ~0.01")
        if hasattr(printer, "blip"):
            printer.blip()

        # Reset printer buffer at start of print job (for invert mode)
        # Also set max lines limit
        max_lines = getattr(settings, "max_print_lines", 200)
        if hasattr(printer, "reset_buffer"):
            printer.reset_buffer(max_lines)

        # Execute the module
        execute_module(module)

        # Flush buffer to actually print. The printer driver applies a fixed
        # post-print cutter feed after each job.
        if hasattr(printer, "flush_buffer"):
            printer.flush_buffer()

    try:
        # Run blocking printer operations in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, _do_print)
    finally:
        # Always mark print as complete (thread-safe)
        clear_print_reservation(clear_hold=False)

def _print_print_webhook_job_sync(module_id: str, job: dict) -> None:
    print_webhook_service.print_job(
        modules=settings.modules,
        printer=printer,
        max_print_lines=getattr(settings, "max_print_lines", 200),
        module_id=module_id,
        job=job,
    )


async def _run_print_webhook_print_job(module_id: str, job: dict) -> None:
    try:
        await asyncio.to_thread(_print_print_webhook_job_sync, module_id, job)
    finally:
        clear_print_reservation(clear_hold=False)


@app.post("/hook/{endpoint_path:path}")
async def receive_print_webhook(
    endpoint_path: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    try:
        dial_position = dial.read_position()
    except Exception:
        logger.exception("Could not read dial position while checking webhook channel gate")
        dial_position = None

    target = print_webhook_service.resolve_incoming_target(
        modules=settings.modules,
        channels=settings.channels,
        endpoint_path=endpoint_path,
        request=request,
        dial_position=dial_position,
    )
    body = await print_webhook_service.read_limited_request_body(request)
    prepared_job = print_webhook_service.finalize_incoming_job(
        target=target,
        content_type=request.headers.get("content-type", ""),
        body=body,
    )

    _require_printer_available()
    if not try_begin_print_job(debounce=False):
        raise HTTPException(status_code=423, detail="Printer is already busy")

    background_tasks.add_task(
        _run_print_webhook_print_job,
        prepared_job.module_id,
        prepared_job.job,
    )
    return Response(
        content='{"message":"Print request accepted"}',
        status_code=202,
        media_type="application/json",
    )


class SlackTestRequest(BaseModel):
    bot_token: str = ""
    app_token: str = ""


@app.post("/api/slack/test", dependencies=[Depends(require_admin_access)])
async def test_slack(payload: SlackTestRequest):
    """Validate a Slack bot/app token pair without touching live connections."""
    return await slack_manager.test_credentials(payload.bot_token, payload.app_token)


@app.post("/debug/test-webhook", dependencies=[Depends(require_admin_access)])
async def test_webhook(action: WebhookConfig):
    """
    Executes a custom webhook immediately for testing.
    Pass the webhook configuration in the body.
    Runs blocking operations in a thread pool to avoid blocking the event loop.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _run_webhook():
        webhook.run_webhook(action, printer, module_name=None)

    try:
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, _run_webhook)
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error in test_webhook: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Webhook execution failed: {str(e)}"
        )

    return {"message": "Webhook executed"}


def _preview_webhook_sync(config: dict):
    import requests
    import json

    action = WebhookConfig(**config)
    url = action.url
    json_path = action.json_path

    if not url:
        return {"success": False, "error": "URL is required"}

    try:
        response = webhook.request_webhook_response(action)

        if response.status_code >= 400:
            return {
                "success": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}",
                "status_code": response.status_code,
            }

        content_type = (response.headers.get("content-type") or "").split(";", 1)[
            0
        ].strip().lower()
        if content_type.startswith("image/"):
            try:
                import base64
                import io
                from PIL import Image

                with Image.open(io.BytesIO(response.content)) as image:
                    dimensions = f"{image.width}x{image.height}"
                    preview_image = webhook._prepare_image_for_print(image)
                    preview_buffer = io.BytesIO()
                    preview_image.save(preview_buffer, format="PNG")
                preview_data_url = (
                    "data:image/png;base64,"
                    f"{base64.b64encode(preview_buffer.getvalue()).decode('ascii')}"
                )
            except Exception:
                dimensions = "unknown size"
                preview_data_url = ""
            return {
                "success": True,
                "content": f"Image response: {content_type} ({dimensions})",
                "content_type": "image",
                "preview_data_url": preview_data_url,
                "status_code": response.status_code,
            }

        # Parse response
        try:
            data = response.json()
        except Exception:
            # Not JSON, return as text
            return {
                "success": True,
                "content": response.text[:500],
                "content_type": "text",
                "status_code": response.status_code,
            }

        # Extract value at json_path if specified
        if json_path:
            keys = json_path.split(".")
            value = data
            for k in keys:
                if isinstance(value, dict):
                    value = value.get(k, {})
                elif isinstance(value, list) and k.isdigit():
                    idx = int(k)
                    if 0 <= idx < len(value):
                        value = value[idx]
                    else:
                        value = None
                else:
                    value = None
                    break

            if value and not isinstance(value, (dict, list)):
                return {
                    "success": True,
                    "content": str(value),
                    "content_type": "extracted",
                    "json_path": json_path,
                    "status_code": response.status_code,
                }
            return {
                "success": False,
                "error": f"Path '{json_path}' not found in response",
                "raw_response": json.dumps(data, indent=2)[:500],
                "status_code": response.status_code,
            }

        # Return full JSON response
        return {
            "success": True,
            "content": json.dumps(data, indent=2)[:500],
            "content_type": "json",
            "status_code": response.status_code,
        }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "Request timed out (10s limit)"}
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "error": "Could not connect to server. Check the URL.",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/webhook/test", dependencies=[Depends(require_admin_access)])
async def preview_webhook(config: dict):
    """
    Tests a webhook configuration and returns the response without printing.
    Useful for validating webhook setup before actual use.
    """

    try:
        return await asyncio.to_thread(_preview_webhook_sync, config)
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- STATIC FILES (FRONTEND) ---

# Mount the built React app
# Ensure 'web/dist' exists (run 'npm run build' in web/ directory first)
if os.path.exists("web/dist"):
    app.mount("/assets", StaticFiles(directory="web/dist/assets"), name="assets")

    # Serve fonts directory with explicit MIME type
    if os.path.exists("web/dist/fonts"):

        @app.get("/fonts/{font_path:path}")
        async def serve_font(font_path: str):
            font_file_path = os.path.join("web/dist/fonts", font_path)
            if os.path.exists(font_file_path):
                # Determine MIME type based on file extension
                if font_path.endswith(".woff2"):
                    return FileResponse(font_file_path, media_type="font/woff2")
                elif font_path.endswith(".woff"):
                    return FileResponse(font_file_path, media_type="font/woff")
                elif font_path.endswith(".ttf"):
                    return FileResponse(font_file_path, media_type="font/ttf")
                elif font_path.endswith(".otf"):
                    return FileResponse(font_file_path, media_type="font/otf")
            raise HTTPException(status_code=404, detail="Font not found")

    # Serve favicon explicitly
    @app.get("/favicon.svg")
    async def serve_favicon():
        return FileResponse("web/dist/favicon.svg", media_type="image/svg+xml")

    # The settings UI uses tabs, not client-side URL routes. Unknown paths,
    # including retired captive-portal probes, must return 404 instead of the UI.
    @app.get("/")
    async def serve_frontend():
        return FileResponse("web/dist/index.html")

else:
    print("[WARNING] web/dist directory not found. Frontend will not be served.")
