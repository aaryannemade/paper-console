"""Unit tests for app/schedule_utils.py (cron-based channel scheduling)."""

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from app.schedule_utils import (
    cron_matches_minute,
    hhmm_to_cron,
    humanize_cron,
    legacy_schedule_to_rules,
    next_fire_times,
    normalize_schedule_rules,
    resolve_timezone_name,
    strip_cron_prefix,
    validate_cron_expression,
)


class TestValidateCronExpression:
    def test_valid_expression_is_returned_normalized(self):
        assert validate_cron_expression(" 30 8 * * 1-5 ") == "30 8 * * 1-5"

    def test_cron_tz_prefix_is_stripped_and_ignored(self):
        assert validate_cron_expression("CRON_TZ=Europe/London 0 9 * * *") == "0 9 * * *"
        assert validate_cron_expression("TZ=UTC 0 9 * * *") == "0 9 * * *"

    def test_empty_expression_raises(self):
        with pytest.raises(ValueError, match="required"):
            validate_cron_expression("")
        with pytest.raises(ValueError, match="required"):
            validate_cron_expression("CRON_TZ=UTC")

    def test_wrong_field_count_raises(self):
        with pytest.raises(ValueError, match="5 fields"):
            validate_cron_expression("0 9 * *")
        with pytest.raises(ValueError, match="5 fields"):
            validate_cron_expression("0 0 9 * * *")

    def test_out_of_range_values_raise(self):
        with pytest.raises(ValueError, match="Invalid cron expression"):
            validate_cron_expression("99 99 * * *")

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            validate_cron_expression("not a cron at all hi")


class TestStripCronPrefix:
    def test_no_prefix_passthrough(self):
        assert strip_cron_prefix("0 9 * * *") == "0 9 * * *"

    def test_whitespace_normalized(self):
        assert strip_cron_prefix("  0  9 * * *  ") == "0 9 * * *"


class TestHHMMToCron:
    def test_basic_conversion(self):
        assert hhmm_to_cron("08:30") == "30 8 * * *"
        assert hhmm_to_cron("00:00") == "0 0 * * *"
        assert hhmm_to_cron("23:59") == "59 23 * * *"

    @pytest.mark.parametrize("bad", ["24:00", "12:60", "noon", "12", "12:3:4", ""])
    def test_invalid_times_raise(self, bad):
        with pytest.raises(ValueError):
            hhmm_to_cron(bad)


class TestHumanizeCron:
    def test_daily_12h(self):
        assert humanize_cron("15 22 * * *", "12h") == "Every day at 10:15 PM"

    def test_daily_24h(self):
        assert humanize_cron("15 22 * * *", "24h") == "Every day at 22:15"

    def test_weekdays(self):
        assert humanize_cron("0 9 * * 1-5", "24h") == "Weekdays at 09:00"
        assert humanize_cron("0 9 * * MON-FRI", "24h") == "Weekdays at 09:00"

    def test_weekends(self):
        assert humanize_cron("30 10 * * 0,6", "12h") == "Weekends at 10:30 AM"

    def test_specific_days(self):
        assert humanize_cron("0 7 * * 1,3,5", "24h") == "Mon, Wed, Fri at 07:00"

    def test_monthly(self):
        assert humanize_cron("0 8 1 * *", "24h") == "Day 1 of each month at 08:00"

    def test_every_minute(self):
        assert humanize_cron("* * * * *") == "Every minute"

    def test_interval_minutes(self):
        assert humanize_cron("*/15 * * * *") == "Every 15 minutes"

    def test_hourly(self):
        assert humanize_cron("5 * * * *") == "Every hour at :05"

    def test_complex_falls_back_to_raw(self):
        assert humanize_cron("0 8,12 * * 1") == "Cron '0 8,12 * * 1'"

    def test_malformed_never_raises(self):
        assert humanize_cron("garbage").startswith("Cron ")


class TestLegacyScheduleToRules:
    def test_converts_times(self):
        rules = legacy_schedule_to_rules(["09:05", "18:30"])
        assert rules == [
            {"expression": "5 9 * * *", "enabled": True},
            {"expression": "30 18 * * *", "enabled": True},
        ]

    def test_invalid_time_raises(self):
        with pytest.raises(ValueError):
            legacy_schedule_to_rules(["25:00"])


class TestNormalizeScheduleRules:
    def test_normalizes_and_preserves_enabled(self):
        rules = normalize_schedule_rules(
            [
                {"expression": "0 9 * * *", "enabled": False},
                {"cron": "30 18 * * 1-5"},
            ]
        )
        assert rules == [
            {"expression": "0 9 * * *", "enabled": False},
            {"expression": "30 18 * * 1-5", "enabled": True},
        ]

    def test_duplicates_are_dropped(self):
        rules = normalize_schedule_rules(
            [
                {"expression": "0 9 * * *"},
                {"expression": " 0 9 * * * "},
            ]
        )
        assert len(rules) == 1

    def test_invalid_rule_raises(self):
        with pytest.raises(ValueError):
            normalize_schedule_rules([{"expression": "99 99 * * *"}])

    def test_missing_expression_raises(self):
        with pytest.raises(ValueError):
            normalize_schedule_rules([{"enabled": True}])


class TestCronMatchesMinute:
    def test_exact_minute_matches(self):
        now = datetime(2026, 6, 10, 22, 15, 0)
        assert cron_matches_minute("15 22 * * *", now) is True

    def test_seconds_are_ignored(self):
        now = datetime(2026, 6, 10, 22, 15, 42, 999999)
        assert cron_matches_minute("15 22 * * *", now) is True

    def test_non_matching_minute(self):
        now = datetime(2026, 6, 10, 22, 16, 0)
        assert cron_matches_minute("15 22 * * *", now) is False

    def test_weekday_field(self):
        wednesday = datetime(2026, 6, 10, 9, 0)  # 2026-06-10 is a Wednesday
        saturday = datetime(2026, 6, 13, 9, 0)
        assert cron_matches_minute("0 9 * * 1-5", wednesday) is True
        assert cron_matches_minute("0 9 * * 1-5", saturday) is False

    def test_timezone_aware_now(self):
        tz = ZoneInfo("America/New_York")
        now = datetime(2026, 6, 10, 22, 15, tzinfo=tz)
        assert cron_matches_minute("15 22 * * *", now) is True


class TestNextFireTimes:
    def test_within_horizon(self):
        now = datetime(2026, 6, 10, 8, 27, 0)
        fires = next_fire_times(["30 8 * * *"], now, horizon_seconds=240)
        assert fires == [datetime(2026, 6, 10, 8, 30)]

    def test_outside_horizon_excluded(self):
        now = datetime(2026, 6, 10, 8, 20, 0)
        assert next_fire_times(["30 8 * * *"], now, horizon_seconds=240) == []

    def test_deduplicates_overlapping_rules(self):
        now = datetime(2026, 6, 10, 8, 29, 0)
        fires = next_fire_times(["30 8 * * *", "30 8 * * 1-5"], now, horizon_seconds=240)
        assert fires == [datetime(2026, 6, 10, 8, 30)]

    def test_invalid_expressions_are_skipped(self):
        now = datetime(2026, 6, 10, 8, 29, 0)
        fires = next_fire_times(["garbage", "30 8 * * *"], now, horizon_seconds=240)
        assert fires == [datetime(2026, 6, 10, 8, 30)]

    def test_preserves_timezone_awareness(self):
        tz = ZoneInfo("America/New_York")
        now = datetime(2026, 6, 10, 8, 28, tzinfo=tz)
        fires = next_fire_times(["30 8 * * *"], now, horizon_seconds=240)
        assert len(fires) == 1
        assert fires[0].tzinfo is not None
        assert fires[0].hour == 8
        assert fires[0].minute == 30


class TestResolveTimezoneName:
    def test_valid_timezone(self):
        assert resolve_timezone_name("America/New_York") == "America/New_York"

    def test_invalid_falls_back(self):
        assert resolve_timezone_name("Mars/Olympus", "UTC") == "UTC"

    def test_empty_falls_back(self):
        assert resolve_timezone_name("", "Australia/Sydney") == "Australia/Sydney"

    def test_invalid_fallback_resolves_to_utc(self):
        assert resolve_timezone_name("Mars/Olympus", "Pluto/Whatever") == "UTC"
