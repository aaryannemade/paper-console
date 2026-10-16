"""Cron-based schedule helpers.

All schedule rules are evaluated in the application's configured timezone
(``settings.timezone``). Rules are stored as 5-field cron expressions.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Iterable, List, Optional, Sequence

from croniter import croniter


DEFAULT_TIMEZONE = "UTC"

_WEEKDAY_NAMES = {
    "0": "Sun",
    "1": "Mon",
    "2": "Tue",
    "3": "Wed",
    "4": "Thu",
    "5": "Fri",
    "6": "Sat",
    "7": "Sun",
    "SUN": "Sun",
    "MON": "Mon",
    "TUE": "Tue",
    "WED": "Wed",
    "THU": "Thu",
    "FRI": "Fri",
    "SAT": "Sat",
}


def resolve_timezone_name(timezone_name: Optional[str], fallback: str = DEFAULT_TIMEZONE) -> str:
    """Return a valid IANA timezone name, falling back safely."""
    candidate = (timezone_name or "").strip()
    if candidate:
        try:
            ZoneInfo(candidate)
            return candidate
        except (ZoneInfoNotFoundError, ValueError):
            pass

    try:
        ZoneInfo(fallback)
        return fallback
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TIMEZONE


def strip_cron_prefix(raw_expression: str) -> str:
    """Drop a leading ``CRON_TZ=``/``TZ=`` token if present.

    Per-rule timezones are not supported; schedules always run in the
    configured timezone. The prefix is tolerated (and ignored) so pasted
    expressions from other systems don't hard-fail.
    """
    parts = str(raw_expression or "").strip().split()
    if parts and (parts[0].startswith("CRON_TZ=") or parts[0].startswith("TZ=")):
        parts = parts[1:]
    return " ".join(parts)


def validate_cron_expression(raw_expression: str) -> str:
    """Validate a cron expression and return its normalized form.

    Raises ValueError with a user-facing message when invalid.
    """
    expression = strip_cron_prefix(raw_expression)
    if not expression:
        raise ValueError("Cron expression is required")

    if len(expression.split()) != 5:
        raise ValueError(
            "Cron expression must contain exactly 5 fields "
            "(minute hour day month weekday)"
        )

    if not croniter.is_valid(expression):
        raise ValueError(f"Invalid cron expression: '{raw_expression}'")

    return expression


def hhmm_to_cron(time_value: str) -> str:
    """Convert a legacy ``HH:MM`` string to a daily cron expression."""
    normalized = str(time_value or "").strip()
    parts = normalized.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"Invalid HH:MM time '{time_value}'")

    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid HH:MM time '{time_value}'")

    return f"{minute} {hour} * * *"


def _format_time_label(hour: int, minute: int, time_format: str) -> str:
    if time_format == "24h":
        return f"{hour:02d}:{minute:02d}"
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _weekday_label(weekday_field: str) -> Optional[str]:
    """Human label for a cron weekday field, or None if too complex."""
    field = weekday_field.upper()
    if field in {"1-5", "MON-FRI"}:
        return "Weekdays"
    if field in {"0,6", "6,0", "SAT,SUN", "SUN,SAT"}:
        return "Weekends"

    names: List[str] = []
    for token in field.split(","):
        name = _WEEKDAY_NAMES.get(token)
        if name is None:
            return None
        if name not in names:
            names.append(name)
    return ", ".join(names) if names else None


def humanize_cron(cron_expression: str, time_format: str = "12h") -> str:
    """Best-effort human-readable label for a cron expression."""
    try:
        minute, hour, day, month, weekday = cron_expression.split()
    except ValueError:
        return f"Cron '{cron_expression}'"

    if minute.isdigit() and hour.isdigit() and month == "*":
        time_label = _format_time_label(int(hour), int(minute), time_format)

        if day == "*":
            if weekday == "*":
                return f"Every day at {time_label}"
            weekday_label = _weekday_label(weekday)
            if weekday_label:
                return f"{weekday_label} at {time_label}"
        elif day.isdigit() and weekday == "*":
            return f"Day {int(day)} of each month at {time_label}"

    if minute == "*" and hour == "*" and day == "*" and month == "*" and weekday == "*":
        return "Every minute"

    if minute.startswith("*/") and minute[2:].isdigit() and hour == "*":
        return f"Every {int(minute[2:])} minutes"

    if minute.isdigit() and hour == "*" and day == "*" and month == "*" and weekday == "*":
        return f"Every hour at :{int(minute):02d}"

    return f"Cron '{cron_expression}'"


def legacy_schedule_to_rules(schedule_times: Sequence[str]) -> List[dict]:
    """Convert a legacy list of HH:MM strings to schedule rule dicts."""
    rules: List[dict] = []
    for time_value in schedule_times:
        rules.append({"expression": hhmm_to_cron(time_value), "enabled": True})
    return rules


def normalize_schedule_rules(rule_payloads: Iterable[dict]) -> List[dict]:
    """Validate and normalize incoming schedule rule payloads.

    Raises ValueError with a user-facing message when any rule is invalid.
    """
    rules: List[dict] = []
    seen: set[str] = set()

    for raw_rule in rule_payloads:
        expression = validate_cron_expression(
            str(raw_rule.get("expression") or raw_rule.get("cron") or "")
        )
        if expression in seen:
            continue
        seen.add(expression)
        rules.append(
            {
                "expression": expression,
                "enabled": bool(raw_rule.get("enabled", True)),
            }
        )

    return rules


def cron_matches_minute(expression: str, now: datetime) -> bool:
    """Return True when the cron expression matches the given minute."""
    reference = now.replace(second=0, microsecond=0)
    return bool(croniter.match(expression, reference))


def next_fire_times(
    expressions: Iterable[str],
    now: datetime,
    horizon_seconds: int,
) -> List[datetime]:
    """Upcoming fire datetimes within ``horizon_seconds`` of ``now``.

    Results carry the same tzinfo as ``now`` and are de-duplicated and sorted.
    """
    upcoming: set[datetime] = set()

    for expression in expressions:
        try:
            fire_at = croniter(expression, now).get_next(datetime)
        except Exception:
            continue
        if (fire_at - now).total_seconds() <= horizon_seconds:
            upcoming.add(fire_at)

    return sorted(upcoming)
