# PC-1 (Paper Console 1)

**A small, self-hosted thermal printer for news, weather, puzzles, email, and more.**

> This fork is migrating to a container + ESP32-S3 architecture. The server
> handles application logic and pushes print commands over HTTP. The ESP32-S3
> firmware will provision WiFi, relay physical inputs, and drive a CSN-A2 printer
> over TTL UART. The server-side relay works with the simulator; firmware and
> container packaging are still planned. See the [migration plan](docs/container-esp32-plan.md).

---

* **No screens:** output is physical thermal paper.
* **No subscriptions:** user-owned API keys or local algorithms.
* **Quality materials:** walnut, brass, and archival-grade paper.
* **Universal channels:** fully configurable channels (Adventure, Astronomy, Calendar, Email, History, Journal Prompt, Maze, News, Print Endpoint, QR Code, Quotes, RSS, Sudoku, System Monitor, Text / Note, Weather, Webhook)

For more information and build photos, visit [travismiller.design/paper-console](https://travismiller.design/paper-console).

![PC-1 Front View](images/pc-1_front2.jpg)
*Original hardware prototype; ESP32-S3 hardware and wiring are planned for Step 6.*

---

## 1. Quick Start (Software)

Run locally without hardware using a Unix-like shell (WSL on Windows). With
Nix and flakes enabled:

```bash
nix develop
export PC1_DEVICE_PASSWORD='choose-your-local-server-password'
just backend                    # API on http://localhost:8000; console receipts

# In another terminal (also inside nix develop):
just frontend-install           # First run only
just frontend                   # Settings UI on http://localhost:5173
```

See [Print outputs](#print-outputs) for USB printing and the ESP32 relay
simulator. API docs are available at [http://localhost:8000/docs](http://localhost:8000/docs).

### First Use (This Fork)

The container does not manage WiFi, run a captive portal, or print setup
receipts. Network onboarding moves to the ESP32-S3 firmware (captive portal and
link-status UI), which is not implemented yet.

1. Start the server with `PC1_DEVICE_PASSWORD` set (see the development section below).
2. Open the settings UI and log in with that password.
3. Configure channels, modules, and schedules.

**Physical controls** are relayed by the ESP32 as HTTP input events (see
[the relay protocol](docs/relay-protocol.md)); the firmware is not written yet, so
use `just device ...` to send the same events during development.

- Select a channel (1-8); press the button to print it.
- Hold ~5 seconds, then release, for **Quick Actions**:
  - `1` Table of Contents
  - `2` System Monitor
  - `5` Reset application settings
  - `8` Cancel
- Quick Actions are single-shot and auto-exit after 2 minutes without a choice.
- Hold ~15 seconds to reset application settings.

Resetting restores default channels/modules only. It does not change the server
password, networking, or restart the host.

---

## 2. Software Installation

### Development Environment

Project commands assume a Unix-like shell environment. On Windows, use WSL.

#### Nix development shell

The proposed container/ESP32-S3 migration is documented in
[the migration plan](docs/container-esp32-plan.md).

With Nix installed and the `nix-command` and `flakes` features enabled:

```bash
nix develop

# Backend (prints are logged to the console)
export PC1_DEVICE_PASSWORD='choose-your-local-server-password'
export PC1_RELAY_TOKEN='choose-a-device-token-16+chars'   # enables device input events
just backend

# In another terminal: act as the ESP32's controls (same PC1_RELAY_TOKEN)
just device position 5
just device press

# In another terminal, also inside nix develop:
just frontend-install
just frontend

# Tests and print snapshots
just test
python testing/render_all_prints.py --output-dir testing/artifacts/print
```

Run `just` to list recipes. `just backend` watches only `app/`, so installing
frontend dependencies does not restart the backend. `web/dist` is not committed;
run `just frontend-build` (then restart the backend) to serve the built UI from
the backend port instead of the Vite dev server. Optional examples:

```bash
just backend 0.0.0.0 8000   # Allow access from other devices on the LAN
just frontend --host 0.0.0.0
just test -k printer        # Forward arguments to pytest
```

Log into the settings UI using the `PC1_DEVICE_PASSWORD` value exported in the
backend terminal (if unset, a generated password is printed in the backend log).
The server does not administer its host: no WiFi, SSH, OS password, clock, reboot,
or in-place updates. It reads the host/container clock without changing it, and
the timezone in settings (not the OS timezone) controls receipts and schedules.

#### Print outputs

Every receipt is rendered by the same renderer into one 384-dot-wide bitmap, then
handed to the output selected by `PC1_PRINTER_OUTPUT`:

| Value | Use | Behavior |
| --- | --- | --- |
| `esp32` | Production (default) | Pushes jobs to the ESP32-S3 relay over HTTP ([protocol](docs/relay-protocol.md)). Needs `PC1_RELAY_TOKEN` and heartbeats from the device; `PC1_RELAY_URL` optionally pins the device URL. Develop against `just relay-sim`. |
| `console` | Development (`just backend` default) | Logs `[PRINT]` text lines plus a `[RECEIPT]` line with size and byte count. Set `PC1_PRINT_CAPTURE_DIR` to also save each receipt as PNG. |
| `usb` | Development only | Prints on a CSN-A2 connected through a USB-serial (TTL) adapter. `PC1_PRINTER_PORT` overrides auto-detection (`/dev/serial/by-id/*`, `/dev/ttyUSB*`, `/dev/ttyACM*`); `PC1_PRINTER_BAUDRATE` defaults to 9600. Your user needs serial port access (usually the `dialout` group). |

```bash
PC1_PRINT_CAPTURE_DIR=testing/artifacts/live just backend   # console + PNGs
PC1_PRINTER_OUTPUT=usb just backend                         # real printer over USB
```

When the output is unavailable (ESP32 offline, USB adapter unplugged), prints are
skipped and logged: UI/API requests get `503`, and scheduled, email and Slack
prints are dropped. Nothing is queued. `/api/health` reports the printer state
but does not mark the server degraded for it. An unknown `PC1_PRINTER_OUTPUT`
value stops startup.

The flake provides `just`, Python 3.12, the application/test dependencies, Node.js 22
(including npm), and launcher utilities on Linux and macOS, for x86-64 and ARM64.
`flake.lock` pins the Nix package set; frontend dependencies use
`web/package-lock.json`. The first shell entry downloads the Nix dependencies;
`npm ci` separately installs the frontend dependencies.

The recipes use Python from the shell rather than
`testing/run_tests.sh`, which creates a pip-managed `.venv`. Enter the shell
without an activated virtual environment to use the Nix-provided Python.
No `.venv` is created or modified by the flake. If Python requirements change,
update the package list in `flake.nix` as well.

The settings UI screenshot helper has an additional Playwright/browser setup
requirement; browser binaries are not included in this development shell.

#### Python virtual environment

First-time setup:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements-dev.txt
cd web && npm install
```

1. **Install Python Dependencies:**
   ```bash
   ./.venv/bin/python -m pip install -r requirements-dev.txt
   ```
    This installs the app dependencies, USB printer support, and test tooling.

2. **Run the Server:**
   ```bash
    PC1_PRINTER_OUTPUT=console ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   ```

### Container packaging

An official Dockerfile and Compose configuration are planned in Step 4. Until
then, run the server directly as shown above. To test the production HTTP print
path without firmware, start `PC1_PRINTER_OUTPUT=esp32 just backend` and
`just relay-sim` in separate terminals with the same `PC1_RELAY_TOKEN`.

---

## 3. Configuration

Configuration is handled via the **Web UI** at `http://localhost:5173` in development,
or the server address after running `just frontend-build`.

### Global Settings
* **Location:** City name, Latitude, Longitude, Timezone (with search functionality)
* **Time Format:** 12-hour (AM/PM) or 24-hour format

### Channel System
* **8 Channel Positions:** Each position (1-8) is a selectable channel reported by the ESP32 input protocol
* **Modular System:** Channels are containers. You can assign **multiple modules** to a single channel (e.g., "News" followed by "Weather" followed by "Sudoku")
* **Reordering:** 
  * **Channels:** Use Up/Down arrows next to Channel title to swap entire channels
  * **Modules:** Use arrow buttons within a channel card to change print order
* **Scheduled Printing:** Click the clock icon on any channel to schedule automatic printing at specific times

### Module Types

Each module is an independent instance with its own configuration. See [Architecture & Modules](#4-architecture--modules) for detailed information.

Available modules: **News API**, **RSS Feeds**, **Weather**, **Email Inbox**, **Sudoku**, **Maze**, **Adventure**, **Astronomy**, **Calendar**, **Webhook**, **Print Endpoint**, **QR Code**, **Text / Note**, **Quotes**, **Journal Prompt**, **History**, **System Monitor**, **Word of the Day**

### Settings Storage
* **Settings File:** `config.json` (auto-saved, gitignored)
* **Reset:** "Reset All Settings" button restores factory defaults

### Printer Status API

Webhook clients can poll `GET /api/printer/status` without authentication. It returns
`{"ready": true, "reason": "idle"}` when the printer driver is available and no
print or button-hold action has reserved it. Otherwise, `ready` is `false` with
reason `printing`, `hold`, or `unavailable`.

This is an advisory snapshot of application readiness. It does not query the
physical printer, detect paper/power faults, or reserve the next job. A print stays
busy until completion or cancellation cleanup. Clients must still handle HTTP
`423` from the print webhook if another job starts before their request arrives,
as well as the endpoint's authentication and content validation errors.

### Local Validation Workflows
Use the local test and snapshot workflows:

* **Pytest:** `./testing/run_tests.sh`
* **Print snapshots:** `./.venv/bin/python testing/render_all_prints.py --output-dir testing/artifacts/print`
* **Settings UI snapshots:** `./.venv/bin/python testing/render_settings_ui.py --output-dir testing/artifacts/ui`

These workflows run without physical controls or a printer.

### Security & Network Environment Variables
For production deployments, configure these environment variables:

* **`PC1_DEVICE_PASSWORD`**: Server password (min. 8 characters) for settings login and the `X-PC1-Device-Password` API header.
* **`PC1_DEVICE_PASSWORD_FILE`**: Alternative to the above: path to a file containing the password (e.g. a Docker secret).
  If neither is set, a random password is generated on first start, saved to `.device_password` (mode 0600; override the path with `PC1_GENERATED_PASSWORD_FILE`), and printed once in the server log.
  The password is an application credential only; it is not synced to OS accounts, and changing it means changing the environment/secret and restarting.
* **`PC1_RELAY_URL`**: Optional fixed base URL of the ESP32 (otherwise the `callback_url` from its heartbeat is used).
* **`PC1_RELAY_TOKEN`** / **`PC1_RELAY_TOKEN_FILE`**: Shared bearer token for the ESP32 relay (at least 16 characters), used in both directions. Without it, `/api/device/events` answers 503. See [docs/relay-protocol.md](docs/relay-protocol.md).
* **`PC1_SESSION_SECRET`**: Optional key for signing login cookies. Defaults to the password, so changing the password logs out existing sessions.
* **Settings Login Sessions**: The web UI can remember a browser with a signed `HttpOnly` session cookie so users do not need to re-enter the password on every visit.
* **`PC1_CORS_ORIGINS`**: Comma-separated CORS origins (default is local/dev origins only).
* **`PC1_LOG_LEVEL`**: Backend log level (default: `WARNING` on device builds).

Container image releases are planned for Step 4; the old in-place device update
and imaging workflows have been removed.

---

## 4. Architecture & Modules

### Tech Stack
* **OS:** Linux server or development host (container packaging planned)
* **Backend:** Python 3.12 + FastAPI
* **Frontend:** React + Vite + Tailwind CSS v4

### Directory Structure
```
paper-console/
├── app/
│   ├── main.py                 # FastAPI app, input actions, print orchestration
│   ├── auth.py                 # Device-password auth and session helpers
│   ├── config.py               # Settings, channels, schedules, module models
│   ├── device_password.py      # Server password (env, secret file, or generated)
│   ├── factory_reset.py        # Application settings reset (no host changes)
│   ├── hardware.py             # Printer reservation and channel position
│   ├── location_lookup.py      # City/timezone lookup helpers
│   ├── module_registry.py      # @register_module metadata and validation
│   ├── selection_mode.py       # Position-driven interactive selection flows
│   ├── device_input.py         # ESP32 input events: position + button gestures
│   ├── relay_auth.py           # Shared container/ESP32 bearer token
│   ├── relay_link.py           # ESP32 heartbeat, link status, job status
│   ├── qr_payloads.py          # QR content encoders (WiFi/contact QR modules)
│   ├── utils.py
│   ├── data/                   # Bundled offline content and lookup datasets
│   ├── drivers/
│   │   ├── printer_raster.py   # Receipt renderer (bitmap for every output)
│   │   ├── print_outputs.py    # Output selection and console capture
│   │   ├── usb_serial_output.py # USB-serial printer output (dev only)
│   │   ├── esp32_relay_output.py # ESP32 relay output (production)
│   │   ├── escpos.py           # CSN-A2 command encoding (raster strips, feeds)
│   │   └── printer_mock.py     # [PRINT] text echo for console output and tests
│   ├── modules/                # Printable module implementations
│   │   ├── adventure.py        # Interactive CYOA (channel positions)
│   │   ├── astronomy.py
│   │   ├── calendar.py
│   │   ├── crossword.py
│   │   ├── email_client.py
│   │   ├── history.py
│   │   ├── journal_prompts.py
│   │   ├── maze.py
│   │   ├── news.py
│   │   ├── print_image.py
│   │   ├── qrcode_print.py
│   │   ├── quotes.py
│   │   ├── rss.py
│   │   ├── sudoku.py
│   │   ├── system_monitor.py
│   │   ├── text.py
│   │   ├── weather.py
│   │   ├── webhook.py
│   │   ├── wordsearch.py
│   │   └── wotd.py
├── web/                        # React + Vite + Tailwind settings UI
│   ├── src/                    # Settings UI, shared constants/styles
│   ├── public/                 # Static icons/assets
│   ├── package.json
│   └── vite.config.js
├── scripts/
│   └── validate_icons.py      # Validate receipt icon references
├── testing/                    # Pytest, print/UI render helpers, snapshot galleries
│   ├── render_all_prints.py
│   ├── render_settings_ui.py
│   ├── console_raster_preview.py
│   ├── relay_simulator.py      # ESP32 reference device for local tests
│   ├── run_tests.sh
│   └── artifacts/
├── development/                # BOM, internal docs, supporting scripts
├── icons/                      # Icon source assets
├── images/                     # Product photos used in the README
├── .github/workflows/
├── .env.example
├── AGENTS.md
├── config.json                 # Local settings (gitignored in normal use)
├── requirements.txt
├── requirements-dev.txt
└── readme.md
```

### Module Details

**News API:** NewsAPI top headlines; requires API key.

**RSS Feeds:** Custom RSS URLs; fetches and prints article blocks with optional QR.

**Weather:** Open-Meteo; uses global location from settings.

**Email Inbox:** IMAP; configurable poll interval; prints unread messages.

**Sudoku:** Easy / Medium / Hard; generated puzzle printed as bitmap grid.

**Maze:** Easy / Medium / Hard; printable maze (bitmap) with start/end markers.

**Adventure:** Interactive choose-your-own-adventure; uses dial + button (prints story nodes, exits to normal channel use when done).

**Astronomy:** Sunrise/sunset, moon phase, sun path graphic; uses global location.

**Calendar:** iCal URLs; month/week/day views; recurring events, timezone-aware.

**Webhook:** GET/POST; headers, body, JSON path extraction for custom APIs.

**QR Code:** URLs, WiFi, contact, phone, SMS, email, plain text.

**Text / Note:** Rich text (TipTap JSON): headings, lists, bold/italic, horizontal rules.

**Quotes:** Offline DB of thousands of quotes; random quote per print.

**Journal Prompt:** Offline prompt library; random prompt per print.

**History:** Offline “on this day” events; optional event count in config.

**System Monitor:** Server hostname, IP, disk/memory bars, uptime, load, CPU temp (where available).

**Word of the Day:** Merriam Webster's word of the day

**Crossword:** Easy / Medium / Hard; generated crossword puzzle printed as bitmap grid.

**Word Search:** Easy / Hard; generated word search puzzle printed as bitmap grid.

---

## 5. Printer and Device

The server never accesses GPIO or provisions a host serial port. For development,
use `console`, or attach a CSN-A2 through a USB-to-TTL serial adapter and select
`PC1_PRINTER_OUTPUT=usb`. For the ESP32 relay protocol, serial framing and
electrical considerations, see [printer UART reference](docs/printer-uart-reference.md)
and [relay protocol](docs/relay-protocol.md). ESP32 wiring and firmware belong to
Step 6; pin assignments will be confirmed for the chosen board.

---

## 6. Troubleshooting

### Printer Issues
* **Relay offline:** Check `GET /api/printer/link` or Printer Connection in General
  Settings. Start `just relay-sim` to exercise the HTTP relay without firmware.
* **USB printer not found:** Check `PC1_PRINTER_PORT`, adapter power and serial
  access permissions; most Linux distributions use the `dialout` group.
* **No output in development:** Start with `PC1_PRINTER_OUTPUT=console just backend`
  and watch the `[PRINT]` and `[RECEIPT]` log entries.

### Server Issues
* **Port in use:** Stop the process already bound to the port, or supply a
  different port to `just backend`, e.g. `just backend 127.0.0.1 8001`.
* **More diagnostic logs:** Set `PC1_LOG_LEVEL=INFO`; for Uvicorn logs,
  run `python -m uvicorn app.main:app --log-level info --access-log` inside `nix develop`.

### Module-Specific Issues
* **NewsAPI Returns 0 Articles:** Check if API key is valid and on free tier
* **RSS Feeds Not Working:** Ensure RSS feed URLs are valid and accessible
* **Email Auth Failed:** Use a **Google App Password**, not your main password. Ensure 2FA is enabled
* **Calendar Not Showing Events:** Verify iCal URL is correct. Check that events exist in the date range
* **Weather Wrong:** Check `config.json` Lat/Long via UI location search feature

---
