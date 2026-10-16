"""Read-only system endpoints and the container boundary of the application code."""

import asyncio
import re
from pathlib import Path

import app.main as main

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The server runs in a container: it never administers its host (power, clock,
# networking, accounts) and never drives board hardware directly. Read-only
# "git rev-parse" for the version fallback is allowed.
FORBIDDEN = re.compile(
    r'"sudo"|systemctl|timedatectl|chpasswd|raspi-config|"reboot"|nmcli|hwclock|'
    r'ntpdate|set-ntp|set-timezone|"date",\s*"-s"|'
    r"RPi\.GPIO|gpiozero|/dev/gpiochip|/dev/serial0|/dev/ttyAMA|vcgencmd|"
    r"/sys/class/thermal|/proc/device-tree",
    re.IGNORECASE,
)


def test_application_code_stays_inside_the_container():
    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FORBIDDEN.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def test_version_comes_from_the_image_version_file(monkeypatch, tmp_path):
    (tmp_path / ".version").write_text("v9.9.9\n", encoding="utf-8")
    monkeypatch.setattr(main, "_get_project_root", lambda: tmp_path)

    assert asyncio.run(main.get_current_version()) == {"version": "v9.9.9"}
