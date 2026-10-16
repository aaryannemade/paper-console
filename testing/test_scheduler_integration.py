"""Integration tests for cron scheduling: scheduler loop, API endpoint, and
weather prefetch interplay."""

import asyncio
import time
import types
from datetime import datetime

import pytest
from zoneinfo import ZoneInfo
from fastapi import BackgroundTasks, HTTPException

import app.hardware as hardware_module
import app.main as main_module
from app.config import (
    ChannelConfig,
    ChannelModuleAssignment,
    ModuleInstance,
    ScheduleRule,
)


def _run_one_scheduler_pass(monkeypatch, now, channels):
    """Run scheduler_loop for exactly one evaluation pass."""
    triggered = []
    sleep_calls = {"count": 0}

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError

    async def fake_trigger_channel(position, scheduled=False):
        triggered.append((position, scheduled))

    monkeypatch.setattr(main_module, "current_datetime", lambda: now)
    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "trigger_channel", fake_trigger_channel)
    monkeypatch.setattr(main_module.settings, "channels", channels)
    monkeypatch.setattr(main_module, "try_begin_print_job", lambda debounce=False: True)
    # Isolate from prefetch bookkeeping left behind by other tests.
    monkeypatch.setattr(main_module, "_weather_prefetch_state", {})

    try:
        asyncio.run(main_module.scheduler_loop())
    except asyncio.CancelledError:
        pass

    return triggered


class TestSchedulerLoop:
    def test_cron_rule_fires_at_matching_minute(self, monkeypatch):
        tz = ZoneInfo("America/New_York")
        now = datetime(2026, 6, 10, 22, 15, 5, tzinfo=tz)
        channels = {
            3: ChannelConfig(
                modules=[],
                schedule_rules=[ScheduleRule(expression="15 22 * * *")],
            )
        }

        assert _run_one_scheduler_pass(monkeypatch, now, channels) == [(3, True)]

    def test_cron_rule_does_not_fire_off_minute(self, monkeypatch):
        now = datetime(2026, 6, 10, 22, 16, 0)
        channels = {
            3: ChannelConfig(
                modules=[],
                schedule_rules=[ScheduleRule(expression="15 22 * * *")],
            )
        }

        assert _run_one_scheduler_pass(monkeypatch, now, channels) == []

    def test_disabled_rule_is_skipped(self, monkeypatch):
        now = datetime(2026, 6, 10, 22, 15, 0)
        channels = {
            3: ChannelConfig(
                modules=[],
                schedule_rules=[ScheduleRule(expression="15 22 * * *", enabled=False)],
            )
        }

        assert _run_one_scheduler_pass(monkeypatch, now, channels) == []

    def test_overlapping_rules_print_once_per_channel(self, monkeypatch):
        # Wednesday 2026-06-10: both rules match the same minute.
        now = datetime(2026, 6, 10, 9, 0, 0)
        channels = {
            1: ChannelConfig(
                modules=[],
                schedule_rules=[
                    ScheduleRule(expression="0 9 * * *"),
                    ScheduleRule(expression="0 9 * * 1-5"),
                ],
            )
        }

        assert _run_one_scheduler_pass(monkeypatch, now, channels) == [(1, True)]

    def test_legacy_hhmm_schedule_still_fires(self, monkeypatch):
        now = datetime(2026, 4, 3, 12, 0)
        channels = {1: types.SimpleNamespace(schedule=["12:00"])}

        assert _run_one_scheduler_pass(monkeypatch, now, channels) == [(1, True)]

    def test_invalid_rule_does_not_break_other_channels(self, monkeypatch):
        now = datetime(2026, 6, 10, 9, 0, 0)
        channels = {
            1: types.SimpleNamespace(
                schedule=[],
                schedule_rules=[types.SimpleNamespace(expression="bad cron ! ! !", enabled=True)],
            ),
            2: ChannelConfig(
                modules=[],
                schedule_rules=[ScheduleRule(expression="0 9 * * *")],
            ),
        }

        assert _run_one_scheduler_pass(monkeypatch, now, channels) == [(2, True)]


class TestChannelScheduleRules:
    def test_prefers_cron_rules_over_legacy(self):
        channel = ChannelConfig(
            modules=[],
            schedule=["09:05"],
            schedule_rules=[ScheduleRule(expression="15 6 * * 1-5")],
        )

        rules = main_module._channel_schedule_rules(channel)
        assert [r.expression for r in rules] == ["15 6 * * 1-5"]

    def test_converts_legacy_schedule(self):
        channel = ChannelConfig(modules=[], schedule=["09:05", "18:30"])

        rules = main_module._channel_schedule_rules(channel)
        assert [r.expression for r in rules] == ["5 9 * * *", "30 18 * * *"]
        assert all(r.enabled for r in rules)

    def test_empty_channel(self):
        assert main_module._channel_schedule_rules(None) == []
        assert main_module._channel_schedule_rules(ChannelConfig(modules=[])) == []

    def test_enabled_expressions_skip_disabled(self):
        channel = ChannelConfig(
            modules=[],
            schedule_rules=[
                ScheduleRule(expression="0 9 * * *"),
                ScheduleRule(expression="0 17 * * *", enabled=False),
            ],
        )

        assert main_module._channel_enabled_expressions(channel) == ["0 9 * * *"]

    def test_preview_lines_respect_time_format(self, monkeypatch):
        channel = ChannelConfig(
            modules=[],
            schedule_rules=[
                ScheduleRule(expression="15 22 * * *"),
                ScheduleRule(expression="0 9 * * 1-5", enabled=False),
            ],
        )

        monkeypatch.setattr(main_module.settings, "time_format", "24h")
        lines = main_module._schedule_preview_lines(channel)
        assert lines == ["Every day at 22:15", "Weekdays at 09:00 (off)"]

        monkeypatch.setattr(main_module.settings, "time_format", "12h")
        lines = main_module._schedule_preview_lines(channel)
        assert lines[0] == "Every day at 10:15 PM"


class TestUpdateChannelScheduleEndpoint:
    def _call(self, monkeypatch, position, payload):
        monkeypatch.setattr(
            main_module.settings, "channels", {position: ChannelConfig(modules=[])}
        )

        async def fake_save(_snapshot):
            return None

        monkeypatch.setattr(main_module, "save_settings_background", fake_save)
        return asyncio.run(
            main_module.update_channel_schedule(position, BackgroundTasks(), payload)
        )

    def test_cron_payload_writes_typed_rules(self, monkeypatch):
        result = self._call(
            monkeypatch,
            3,
            {"rules": [{"expression": "30 8 * * 1-5", "enabled": True}]},
        )

        channel = main_module.settings.channels[3]
        assert channel.schedule_rules == [ScheduleRule(expression="30 8 * * 1-5")]
        assert channel.schedule == []
        assert result["schedule_preview"]

    def test_legacy_payload_is_converted(self, monkeypatch):
        self._call(monkeypatch, 3, ["18:30", "08:05"])

        channel = main_module.settings.channels[3]
        assert [r.expression for r in channel.schedule_rules] == [
            "5 8 * * *",
            "30 18 * * *",
        ]
        assert channel.schedule == []

    def test_invalid_cron_returns_400(self, monkeypatch):
        with pytest.raises(HTTPException) as exc_info:
            self._call(monkeypatch, 3, {"rules": [{"expression": "99 99 * * *"}]})
        assert exc_info.value.status_code == 400
        assert "Invalid cron expression" in exc_info.value.detail

    def test_invalid_legacy_time_returns_400(self, monkeypatch):
        with pytest.raises(HTTPException) as exc_info:
            self._call(monkeypatch, 3, ["25:99"])
        assert exc_info.value.status_code == 400

    def test_non_list_rules_returns_400(self, monkeypatch):
        with pytest.raises(HTTPException) as exc_info:
            self._call(monkeypatch, 3, {"rules": "0 9 * * *"})
        assert exc_info.value.status_code == 400

    def test_bad_payload_type_returns_400(self, monkeypatch):
        with pytest.raises(HTTPException) as exc_info:
            self._call(monkeypatch, 3, "0 9 * * *")
        assert exc_info.value.status_code == 400


class TestWeatherPrefetchWithCronRules:
    def _setup_channel(self, monkeypatch, schedule_rules):
        weather_module = ModuleInstance(id="weather-1", type="weather", name="Weather")
        channel = ChannelConfig(
            modules=[ChannelModuleAssignment(module_id="weather-1", order=0)],
            schedule_rules=schedule_rules,
        )

        monkeypatch.setattr(main_module.settings, "channels", {1: channel})
        monkeypatch.setattr(main_module.settings, "modules", {"weather-1": weather_module})
        monkeypatch.setattr(main_module, "_weather_prefetch_state", {})
        # Deterministic max lead so the prefetch window is open in tests.
        monkeypatch.setattr(
            main_module,
            "_weather_prefetch_lead_seconds",
            lambda module_id, scheduled_for: main_module.WEATHER_PREFETCH_MAX_LEAD_SECONDS,
        )

        prefetched = []

        async def fake_prefetch(module, scheduled_for, slot_key):
            prefetched.append((module.id, scheduled_for))

        monkeypatch.setattr(
            main_module, "_prefetch_weather_module_for_schedule", fake_prefetch
        )
        return prefetched

    def test_prefetch_warms_before_cron_fire(self, monkeypatch):
        prefetched = self._setup_channel(
            monkeypatch, [ScheduleRule(expression="30 8 * * *")]
        )
        now = datetime(2026, 6, 10, 8, 27, 0)

        async def run():
            await main_module._run_weather_prefetch_cycle(now)
            # Let the created prefetch task run.
            await asyncio.sleep(0)

        asyncio.run(run())

        assert prefetched == [("weather-1", datetime(2026, 6, 10, 8, 30))]

    def test_no_prefetch_when_fire_is_far_away(self, monkeypatch):
        prefetched = self._setup_channel(
            monkeypatch, [ScheduleRule(expression="0 18 * * *")]
        )
        now = datetime(2026, 6, 10, 8, 27, 0)

        asyncio.run(main_module._run_weather_prefetch_cycle(now))

        assert prefetched == []

    def test_no_prefetch_for_disabled_rule(self, monkeypatch):
        prefetched = self._setup_channel(
            monkeypatch, [ScheduleRule(expression="30 8 * * *", enabled=False)]
        )
        now = datetime(2026, 6, 10, 8, 27, 0)

        asyncio.run(main_module._run_weather_prefetch_cycle(now))

        assert prefetched == []


def test_scheduler_loop_skips_trigger_when_hold_reserved(monkeypatch):
    triggered = []
    sleep_calls = {"count": 0}

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError

    async def fake_trigger_channel(position, scheduled=False):
        triggered.append((position, scheduled))

    monkeypatch.setattr(main_module, "current_datetime", lambda: datetime(2026, 4, 3, 12, 0))
    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module, "trigger_channel", fake_trigger_channel)
    monkeypatch.setattr(
        main_module.settings,
        "channels",
        {1: types.SimpleNamespace(schedule=["12:00"])},
    )
    monkeypatch.setattr(hardware_module, "print_in_progress", False)
    monkeypatch.setattr(hardware_module, "hold_action_in_progress", True)
    monkeypatch.setattr(
        hardware_module,
        "hold_action_started_at",
        time.time(),
    )

    try:
        asyncio.run(main_module.scheduler_loop())
    except asyncio.CancelledError:
        pass

    assert triggered == []
    hardware_module.clear_print_reservation()


def test_run_weather_prefetch_cycle_warms_upcoming_scheduled_weather(monkeypatch):
    calls = []
    main_module._weather_prefetch_state.clear()

    async def run_cycle():
        await main_module._run_weather_prefetch_cycle(datetime(2026, 4, 3, 7, 57, 30))
        await asyncio.sleep(0)

    module = types.SimpleNamespace(id="weather-one", type="weather", config={"city_name": "Worcester"})
    channel = ChannelConfig(
        modules=[ChannelModuleAssignment(module_id="weather-one", order=0)],
        schedule=["08:00"],
    )

    monkeypatch.setattr(
        main_module,
        "settings",
        types.SimpleNamespace(
            channels={1: channel},
            modules={"weather-one": module},
        ),
    )
    monkeypatch.setattr(main_module, "_weather_prefetch_lead_seconds", lambda *_args: 180)
    monkeypatch.setattr(
        main_module.weather,
        "prefetch_weather",
        lambda config, module_id=None: calls.append((module_id, dict(config or {}))) or {"ok": True},
    )

    asyncio.run(run_cycle())

    assert calls == [("weather-one", {"city_name": "Worcester"})]


def test_email_polling_loop_uses_configured_interval_for_auto_print(monkeypatch):
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise asyncio.CancelledError

    module = types.SimpleNamespace(
        type="email",
        config={"auto_print_new": True, "polling_interval": 60},
    )

    monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(main_module.settings, "modules", {"email-1": module})

    try:
        asyncio.run(main_module.email_polling_loop())
    except asyncio.CancelledError:
        pass

    assert sleep_calls == [60]
