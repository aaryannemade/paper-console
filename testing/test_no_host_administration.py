"""The container server must not administer its host (Step 2 of the migration)."""

import re
from pathlib import Path

import app.main as main

PROJECT_ROOT = Path(__file__).resolve().parents[1]

REMOVED_ROUTES = {
    ("POST", "/api/system/time"),
    ("POST", "/api/system/time/sync"),
    ("POST", "/api/system/time/sync/disable"),
    ("POST", "/api/system/timezone"),
    ("GET", "/api/system/updates/check"),
    ("POST", "/api/system/updates/install"),
    ("POST", "/api/system/updates/convert-to-production"),
    ("GET", "/api/system/ssh/status"),
    ("POST", "/api/system/ssh/enable"),
    ("POST", "/api/system/ssh/disable"),
    ("GET", "/api/system/device-password/status"),
    ("POST", "/api/system/device-password"),
    ("POST", "/api/system/reboot"),
}

# Commands that change host state. Read-only probes (e.g. "timedatectl show",
# "git rev-parse") are allowed.
FORBIDDEN_COMMAND = re.compile(
    r'"sudo"|systemctl|chpasswd|raspi-config|"reboot"|nmcli|hwclock|ntpdate|'
    r"set-ntp|set-timezone|\"date\",\s*\"-s\""
)


def _registered_routes():
    routes = set()
    for route in main.app.routes:
        for method in getattr(route, "methods", None) or ():
            routes.add((method, getattr(route, "path", "")))
    return routes


def test_host_administration_routes_are_not_registered():
    assert REMOVED_ROUTES.isdisjoint(_registered_routes())


def test_read_only_system_routes_remain():
    routes = _registered_routes()
    assert ("GET", "/api/system/time") in routes
    assert ("GET", "/api/system/version") in routes
    assert ("GET", "/api/system/timezone/list") in routes


def test_application_code_runs_no_privileged_host_commands():
    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN_COMMAND.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def test_pi_uart_migration_is_removed():
    assert not (PROJECT_ROOT / "app" / "printer_uart.py").exists()
    assert not (PROJECT_ROOT / "scripts" / "wifi_ap_nmcli.sh").exists()
    # Kept for the ESP32 firmware step.
    assert (PROJECT_ROOT / "docs" / "printer-uart-reference.md").exists()


def test_frontend_does_not_call_host_administration_endpoints():
    retired = (
        "/api/system/ssh",
        "/api/system/reboot",
        "/api/system/updates",
        "/api/system/device-password",
        "/api/system/time/sync",
    )
    for path in (PROJECT_ROOT / "web" / "src").rglob("*"):
        if path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
            source = path.read_text(encoding="utf-8")
            assert not any(endpoint in source for endpoint in retired), path


def test_version_endpoint_is_read_only(monkeypatch, tmp_path):
    import asyncio

    (tmp_path / ".version").write_text("v9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(main, "_get_project_root", lambda: tmp_path)

    assert asyncio.run(main.get_current_version()) == {"version": "v9.9.9"}
