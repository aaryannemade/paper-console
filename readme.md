# PC-1 (Paper Console 1)

**A small, self-hosted thermal printer for news, weather, puzzles, email, and more.**

> This fork runs as a **container + ESP32-S3** system. The container serves the
> settings UI and API, renders every receipt and decides what inputs mean. A
> Seeed XIAO ESP32S3 provisions WiFi (captive portal), relays the rotary
> encoder/button, shows the channel, menus and a clock on an optional OLED, and
> writes the receipts it is sent to a CSN-A2 thermal printer over TTL UART.
> Compared with the original Raspberry Pi appliance, the server no longer
> touches GPIO, WiFi, the host OS (SSH, passwords, clock, reboot, updates) or
> serial ports, and prints no setup/welcome receipts; those jobs belong to the
> container host and the relay firmware.

---

* **No screens:** output is physical thermal paper.
* **No subscriptions:** user-owned API keys or local algorithms.
* **Quality materials:** walnut, brass, and archival-grade paper.
* **Universal channels:** fully configurable channels (Adventure, Astronomy, Calendar, Email, History, Journal Prompt, Maze, News, Print Endpoint, QR Code, Quotes, RSS, Sudoku, System Monitor, Text / Note, Weather, Webhook)

For more information and build photos, visit [travismiller.design/paper-console](https://travismiller.design/paper-console).

![PC-1 Front View](images/pc-1_front2.jpg)
*Original hardware prototype. This fork's ESP32-S3 wiring is in [Printer and Device](#5-printer-and-device).*

---

## 1. Quick Start (Software)

Run locally without hardware using a Unix-like shell (WSL on Windows). With
Nix and flakes enabled:

```bash
nix develop
just frontend-install           # First run only
just dev-console                # API :8000 + settings UI :5173; console receipts
```

Log in at http://localhost:5173 with the development password `123456789`
(`dev_password` in the `justfile`; an exported `PC1_DEVICE_PASSWORD` overrides
it). `just dev-usb` prints to a USB printer and `just dev-esp32` prints through
the ESP32 relay instead. Ctrl+C stops both servers.

See [Print outputs](#print-outputs) for USB printing and the ESP32 relay
simulator. API docs are available at [http://localhost:8000/docs](http://localhost:8000/docs).

### First Use (This Fork)

The container does not manage WiFi, run a captive portal, or print setup
receipts; the ESP32-S3 handles network onboarding.

1. Start the server (or [container](#container-deployment)) with
   `PC1_RELAY_TOKEN` and optionally `PC1_DEVICE_PASSWORD` set.
2. Open the settings UI and log in with that password.
3. Flash the relay (`just fw-flash`), join its `PaperConsole-XXXXXX` WiFi network
   and enter your WiFi, the server URL and the same relay token
   ([setup](#esp32-s3-relay-firmware-firmware)).
4. Configure channels, modules, and schedules.

### Using the controls

The knob selects one of 8 channels (one detent per step, wrapping 8 → 1;
always channel 1 after power-up). The OLED shows the channel's name, or its
number if unnamed, for 3 seconds after each turn, then returns to the clock.

| Action | Result |
| --- | --- |
| Press | Print the selected channel. While a menu is open: choose the option under the knob. |
| Hold 5 s while printing | Cancel the receipt (it stops at the next 64-row band, up to ~3 s). |
| Hold 5 s, then release | A short paper feed at 5 s means "release now"; then the **Quick Actions** menu prints and opens. |
| Hold 10 s, then clockwise, anticlockwise, click | Erase the relay's WiFi/server/token settings (see [reset sequence](#esp32-s3-relay-firmware-firmware)). |

**Menus** (Quick Actions; "Select app" on a channel with several interactive
modules; Adventure choices) print their options and stay open until you choose
one. While open, the OLED shows the menu title and the option under the knob
("Nothing here" on empty positions, which do nothing when pressed); the dots
along the bottom mark which positions are options. Position 8 is always Cancel
or Exit.

- Quick Actions: `1` Table of contents (channel list), `2` System monitor,
  `8` Cancel. Closes silently after 2 minutes without a choice.
- Holding 5 s while a menu is open reopens Quick Actions instead.

Without hardware, `just device ...` sends the same input events (see
[Developing against the protocol](#developing-against-the-protocol)).

To start over, stop the stack and clear the **dedicated host directory** mounted
as `/data`, then start it again. This removes saved settings, Adventure progress
and a generated password (which will be regenerated on startup). An explicitly
configured `PC1_DEVICE_PASSWORD` in `.env` remains in effect. Because `/data` is
a bind mount, `docker compose down -v` alone does not remove these files. Keep
the host directory writable by the configured PUID/PGID when recreating it.

---

## 2. Software Installation

### Development Environment

Project commands assume a Unix-like shell environment. On Windows, use WSL.

#### Nix development shell

With Nix installed and the `nix-command` and `flakes` features enabled:

```bash
nix develop
just frontend-install       # First run only

# Backend + settings UI together, one recipe per print output:
just dev-console            # receipts logged to the console
just dev-usb                # USB printer (see below)
just dev-esp32              # ESP32 relay; listens on 0.0.0.0
just dev-console --bind 0.0.0.0   # reachable from other devices (e.g. over SSH)

# Or run them separately (backend defaults to console output):
just backend
just frontend               # in another terminal

# In another terminal: act as the ESP32's controls (development relay token)
just device position 5
just device press

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

The `dev-*` recipes and `just backend` default to the development-only login
password `123456789` and relay token `12345678901234567890` (`dev_password` /
`dev_relay_token` in the `justfile`), so no `.device_password` file is generated
and `just device` / `just relay-sim` work with every output. Exported `PC1_DEVICE_PASSWORD` / `PC1_RELAY_TOKEN` values take
precedence. Never use these values in a deployment. The `dev-*` recipes use
ports 8000 and 5173. `dev-console` and `dev-usb` listen on `127.0.0.1` and
`dev-esp32` on `0.0.0.0`; `--bind ADDR` changes the address for both servers
(e.g. `--bind 0.0.0.0` when developing on this machine over SSH, then open
`http://<host-ip>:5173`). Anyone on the network can then reach the UI, which
uses the well-known development password, and the host firewall must allow
ports 5173/8000. They also write the
backend output to `logs/dev-<output>.log` (ignored by git, recreated on each
start), so you can follow it from another terminal, e.g.
`tail -f logs/dev-console.log`.
The server does not administer its host: no WiFi, SSH, OS password, clock, reboot,
or in-place updates. It reads the host/container clock without changing it, and
the timezone in settings (not the OS timezone) controls receipts and schedules.

#### Print outputs

Every receipt is rendered by the same renderer into one 384-dot-wide bitmap, then
handed to the output selected by `PC1_PRINTER_OUTPUT`:

The ESP32 UART output downloads 64-row bitmap bands into printer RAM before
printing them (`GS *` / `GS /`), avoiding the stretch and pale dithering seen
with streaming raster commands at 9600 baud. Native USB retains its 24-row
`GS v 0` format. See [Printer UART](#printer-uart) for the format and
calibration results.

| Value | Use | Behavior |
| --- | --- | --- |
| `esp32` | Production (default) | Pushes jobs to the ESP32-S3 relay over HTTP ([protocol](#6-relay-protocol-container--esp32)). Needs `PC1_RELAY_TOKEN` and heartbeats from the device; `PC1_RELAY_URL` optionally pins the device URL. Develop against `just relay-sim`. |
| `console` | Development (`just backend` default) | Logs `[PRINT]` text lines plus a `[RECEIPT]` line with size and byte count. Set `PC1_PRINT_CAPTURE_DIR` to also save each receipt as PNG. |
| `usb` | Development only | Prints directly to a native USB printer-class device (such as the Gprinter GP-58) through libusb. If none is connected, tries a USB-to-serial (TTL) adapter. Set `PC1_PRINTER_PORT` to select a serial adapter explicitly (`/dev/serial/by-id/*`, `/dev/ttyUSB*`, `/dev/ttyACM*`; baud rate defaults to 9600). For multiple native printers, select one with `PC1_PRINTER_USB_VENDOR_ID` and `PC1_PRINTER_USB_PRODUCT_ID` (hex). USB printer-class devices do not use the UART baud setting. |

```bash
PC1_PRINT_CAPTURE_DIR=testing/artifacts/live just backend   # console + PNGs
PC1_PRINTER_OUTPUT=usb just backend                         # real printer over USB
```

To test a printer with a **built-in USB connector** (no serial adapter):

1. Confirm the device is a USB printer: `nix shell nixpkgs#usbutils --command lsusb`.
   A GP-58 shows `0fe6:811e` and a Printer interface. Linux may not create
   `/dev/usb/lp0`; the native USB output talks to its bulk endpoint directly.
2. Allow your user to open that USB device for this session. Find its bus and
   device numbers in `lsusb`, then grant access to that specific node. For
   example, bus 001 device 020 and user `aaryan`:
   `sudo setfacl -m u:aaryan:rw /dev/bus/usb/001/020`.
   Confirm with `getfacl -p /dev/bus/usb/001/020`. A replug may change the
   device number and remove this temporary permission. Only one process can
   claim the native USB interface at a time; stop another backend before
   probing or printing from a separate process.
3. Inside `nix develop`: `just frontend-install` (first run only), then
   `just dev-usb`. Confirm `http://localhost:8000/api/printer/status` reports
   `"ready":true`.
4. Open `http://localhost:5173` and log in with the development password
   `123456789`.
5. Use the **print** button on the Sudoku module (or channel 5), or request it
   directly with:

   ```bash
   curl -X POST -H "X-PC1-Device-Password: 123456789" \
     http://localhost:8000/debug/print-module/default-sudoku-001
   ```

The backend sends the same 384-dot receipt bitmap and ESC/POS raster bytes as
the relay. On this native USB interface, the GP-58's UART baud settings from
the self-test do not apply. If the status says unavailable, check the backend
warning and USB-device permissions before sending another job.
After the bitmap, one `ESC J` feed advances five lines (120 dots) for tear
clearance; the former two-command seven-line feed wasted paper.

When the output is unavailable (ESP32 offline, USB adapter unplugged), prints are
skipped and logged: UI/API requests get `503`, and scheduled, email and Slack
prints are dropped. Nothing is queued. `/api/health` reports the printer state
but does not mark the server degraded for it. An unknown `PC1_PRINTER_OUTPUT`
value stops startup.

The flake provides `just`, Python 3.14.7, pip 26.2.1, libusb and the application/test
dependencies, Node.js 26.12.0, npm 12.2.0, and launcher utilities on Linux
(x86-64 and ARM64) and Apple Silicon macOS. Current nixpkgs no longer supports
Intel macOS.
The Apple Silicon shell is declared but currently blocked by nixpkgs marking
the Slack build dependency `jaraco-path` broken on Darwin; this update was
built and tested on x86-64 Linux. ARM64 Linux passes flake evaluation.
`flake.lock` pins the Nix package set; frontend dependencies use
`web/package-lock.json`. The first shell entry downloads the Nix dependencies;
`npm ci` separately installs the frontend dependencies.

The package set follows `nixpkgs-unstable`, pinned by `flake.lock`. npm and pip
have explicit overrides because their latest releases are newer than the copies
in that snapshot. Refresh with `nix flake update nixpkgs`, review those overrides,
and keep the Node/Python/npm/pip versions in `Dockerfile` aligned. Python 3.15.0
was tried during the October 2026 update, but its Nix dependency closure failed
in tkinter's upstream tests; 3.14.7 is the validated version for now.

The recipes use Python from the Nix shell. Enter it with `nix develop`, then
run `just test`, `just icons`, or `just backend`. If Python requirements change,
update the package list in `flake.nix` as well.

The settings UI screenshot helper has an additional Playwright/browser setup
requirement; browser binaries are not included in this development shell.

### Container deployment

The multi-stage `Dockerfile` builds the React UI with Node.js 26.12.0/npm 12.2.0
and runs FastAPI on Python 3.14.7 (Debian Trixie, pip 26.2.1)
as UID/GID **1000:1000** (one worker, no reload). It serves the built UI and API
on the same configurable port. The application state lives in `PC1_DATA_DIR=/data`:
`config.json`, `config.json.bak`, the generated `.device_password`, and
`adventure_saves/`. Bundled stories and offline databases remain inside the
image. Container updates replace the image without changing the bind mount.
Both Compose services run with a read-only root filesystem and a temporary
`/tmp`; persistent application files can only be written under `/data`.

The Compose files use an **absolute host bind mount**:

```yaml
volumes:
  - /path/to/data:/data
```

Replace `/path/to/data` in `docker-compose.yaml` or `portainer-compose.yaml`
with a real absolute directory on your Docker host. The container must be able
to write it, so both Compose files run as `user: "${PUID}:${PGID}"` (default
`1000:1000`). Set `PUID`/`PGID` in `.env` (or Portainer's stack environment) to
the owner of the data directory:

```bash
mkdir -p /srv/paper-console
id -u; id -g      # e.g. 1000 and 100 on NixOS -> PUID=1000, PGID=100
# Edit docker-compose.yaml: replace /path/to/data with /srv/paper-console
docker compose -f docker-compose.yaml up -d
```

Under **rootless Podman** your host user is container root, and container UID
1000 maps to a subordinate UID that cannot write your directory; use
`PUID=0` and `PGID=0` there (still unprivileged on the host). If migrating local
settings, copy `config.json` and any `app/data/adventure_saves/` files there
**before** starting the container. Do not put credentials in the image.

**Docker Compose:**

```bash
cp .env.example .env
# Edit .env: set PC1_RELAY_TOKEN for ESP32 output (or PC1_PRINTER_OUTPUT=console).
# Edit docker-compose.yaml: replace /path/to/data with your host directory.
docker compose -f docker-compose.yaml up -d --build
docker compose -f docker-compose.yaml logs -f paper-console
```

The UI and API are at `http://<docker-host>:${PC1_HOST_PORT:-8000}`. The
Dockerfile's health check probes `/api/health`; use
`docker compose -f docker-compose.yaml ps` to inspect it. On first boot, if
`PC1_DEVICE_PASSWORD` is empty, the generated password appears once in the
container log and persists at `/data/.device_password`. Alternatively, set
`PC1_DEVICE_PASSWORD` in `.env`. The server clock comes from the container;
the app timezone in General Settings controls receipt formatting and schedules.

**Portainer:** Build `paper-console:local` on the same Docker host with
`docker build -t paper-console:local .`, or publish an image and set `PC1_IMAGE`
in the stack environment. Paste `portainer-compose.yaml` into a stack, replace
`/path/to/data` with the host directory, and set `PC1_RELAY_TOKEN`, the port
variables, and optionally `PC1_DEVICE_PASSWORD` in Portainer's stack environment.
The Portainer file references an image and does not depend on Portainer building
source code.

`PC1_PORT` changes the listener inside the container; `PC1_HOST_PORT` changes
the mapped host port. The container needs outbound LAN access to the ESP32,
and the ESP32 must be able to reach the host's mapped port to send heartbeats
and input events. Set `PC1_RELAY_URL` if its advertised callback address is not
reachable from the container. For a dry run without firmware, set
`PC1_PRINTER_OUTPUT=console`, then use the UI print button or send an authenticated
`POST /action/print-channel/5`. Native USB printing is for local development;
the production container does not pass through USB devices.

---

## 3. Configuration

Configuration is handled via the **Web UI** at `http://localhost:5173` in development,
or the server address after running `just frontend-build`.

### Global Settings
* **Location:** City name, Latitude, Longitude, Timezone (with search functionality)
* **Time Format:** 12-hour (AM/PM) or 24-hour format

### Channel System
* **8 Channel Positions:** Each position (1-8) is a selectable channel reported by the ESP32 input protocol
* **Channel Names:** Click the "Add name" field beside a channel number to label
  it (e.g. "Work update"; single line, up to 24 characters; Enter or clicking
  away saves, Escape cancels, empty clears). Names move with the channel when
  swapped, appear on the Quick Actions channel list, are included in settings
  export/import, are set with `PUT /api/channels/{position}/name`
  (`{"name": "..."}`), and are shown on the relay's OLED and status page.
* **Modular System:** Channels are containers. You can assign **multiple modules** to a single channel (e.g., "News" followed by "Weather" followed by "Sudoku")
* **Reordering:** 
  * **Channels:** Use Up/Down arrows next to Channel title to swap entire channels
  * **Modules:** Use arrow buttons within a channel card to change print order
* **Scheduled Printing:** See [Scheduling](#scheduling).

### Scheduling

Click the clock icon on a channel to print it automatically. Each channel holds
`schedule_rules`, e.g. `[{"expression": "30 14 * * *", "enabled": true}]`, using
5-field cron (`minute hour day month weekday`, Sunday = 0):

| Expression | Meaning |
| --- | --- |
| `30 14 * * *` | Every day at 14:30 |
| `0 9 * * 1-5` | Weekdays at 09:00 |
| `0 9 * * 0,6` | Weekends at 09:00 |
| `0 8 1 * *` | Day 1 of each month at 08:00 |
| `0 8,12,18 * * *` | Three times daily |

- Rules always run in the **timezone from General Settings**; per-rule timezones
  are not supported (a pasted `CRON_TZ=`/`TZ=` prefix is stripped).
- The scheduler (`scheduler_loop()` in `app/main.py`) checks every 10 s but
  evaluates each minute once; a channel prints at most once per matching minute
  even if rules overlap. Invalid rules are skipped with a warning. A due print
  is skipped while the printer is busy.
- Weather modules on scheduled channels are fetched 2–4 minutes early so the
  print is not delayed by a slow API.
- The schedule editor (`web/src/components/ScheduleModal.jsx`) builds the cron
  expression from a frequency and time; "Advanced (cron expression)" allows raw
  editing with validation, and sub-hourly schedules show a paper-use warning.
  Descriptions follow the 12h/24h setting and are never stored.
- API: `POST /api/channels/{position}/schedule` with
  `{"rules": [{"expression": "30 9 * * *", "enabled": true}]}`; invalid
  expressions return 400. The legacy `["09:30", "14:45"]` form and old
  `schedule` lists are converted to cron rules (the first edit rewrites them).
  Helpers live in `app/schedule_utils.py`; tests in `testing/test_schedule.py`
  and `testing/test_scheduler_integration.py`.

### Module Types

Each module is an independent instance with its own configuration. See [Architecture & Modules](#4-architecture--modules) for detailed information.

Available modules: **News API**, **RSS Feeds**, **Weather**, **Email Inbox**, **Sudoku**, **Maze**, **Adventure**, **Astronomy**, **Calendar**, **Webhook**, **Print Endpoint**, **QR Code**, **Text / Note**, **Quotes**, **Journal Prompt**, **History**, **System Monitor**, **Word of the Day**

### Settings Storage
* **Settings File:** `config.json` (auto-saved, gitignored)
* **Reset:** "Reset All Settings" button restores factory defaults

### Printer Status API

The ESP32 provides public `GET /health` (schema version 1) with firmware version,
uptime, WiFi/server-link state, UART readiness, active job and last job outcome.
Its web page shows the same summary. General Settings → Printer Settings shows
the device's health from authenticated heartbeats, marking telemetry stale after
20 seconds and the relay offline after 30 seconds. Older firmware shows health
unavailable. Failed UI polls also mark retained status stale.

**Physical printer status is unknown:** UART readiness/completion does not prove
power, paper or successful physical printing. No unvalidated status-query bytes
are sent to the printer. Firmware health contains no credentials or configured
server URL. See [Device health](#device-health) for the fields.

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

* **Pytest:** `just test`
* **Icons:** `just icons`
* **Print snapshots:** `python testing/render_all_prints.py --output-dir testing/artifacts/print`
* **Settings UI snapshots:** `python testing/render_settings_ui.py --output-dir testing/artifacts/ui`

Run these inside `nix develop`. They need no physical controls or printer.

### Security & Network Environment Variables
For production deployments, configure these environment variables:

* **`PC1_DEVICE_PASSWORD`**: Server password (min. 8 characters) for settings login and the `X-PC1-Device-Password` API header.
* **`PC1_DEVICE_PASSWORD_FILE`**: Alternative to the above: path to a file containing the password (e.g. a Docker secret).
  If neither is set, a random password is generated on first start, saved to `.device_password` (mode 0600; override the path with `PC1_GENERATED_PASSWORD_FILE`), and printed once in the server log.
  The password is an application credential only; it is not synced to OS accounts, and changing it means changing the environment/secret and restarting.
* **`PC1_RELAY_URL`**: Optional fixed base URL of the ESP32 (otherwise the `callback_url` from its heartbeat is used).
* **`PC1_RELAY_TOKEN`** / **`PC1_RELAY_TOKEN_FILE`**: Shared bearer token for the ESP32 relay (at least 16 characters), used in both directions. Without it, `/api/device/events` answers 503. See [Relay Protocol](#6-relay-protocol-container--esp32).
* **`PC1_SESSION_SECRET`**: Optional key for signing login cookies. Defaults to the password, so changing the password logs out existing sessions.
* **Settings Login Sessions**: The web UI can remember a browser with a signed `HttpOnly` session cookie so users do not need to re-enter the password on every visit.
* **`PC1_CORS_ORIGINS`**: Comma-separated CORS origins (default is local/dev origins only).
* **`PC1_LOG_LEVEL`**: Backend log level (default: `WARNING` on device builds).

Container images are built by `.github/workflows/docker-image.yml`; deploy by
replacing the image and keeping the `/data` bind mount.

---

## 4. Architecture & Modules

### Tech Stack
* **OS:** Linux server (container) or development host
* **Backend:** Python 3.14 + FastAPI
* **Device:** Seeed XIAO ESP32S3, PlatformIO/Arduino (`firmware/`)
* **Frontend:** React + Vite + Tailwind CSS v4

### Directory Structure
```
paper-console/
├── app/
│   ├── main.py                 # FastAPI app, input actions, print orchestration
│   ├── auth.py                 # Device-password auth and session helpers
│   ├── config.py               # Settings, channels, schedules, module models
│   ├── device_password.py      # Server password (env, secret file, or generated)
│   ├── paths.py                # Persistent application data directory
│   ├── version.py              # Image or development checkout version
│   ├── hardware.py             # Printer reservation and channel position
│   ├── location_lookup.py      # City/timezone lookup helpers
│   ├── module_registry.py      # @register_module metadata and validation
│   ├── selection_mode.py       # Menus (selection mode) and their display labels
│   ├── device_input.py         # ESP32 input events: position + button gestures
│   ├── relay_auth.py           # Shared container/ESP32 bearer token
│   ├── relay_link.py           # ESP32 heartbeat, link status, job status, menu push
│   ├── qr_payloads.py          # QR content encoders (WiFi/contact QR modules)
│   ├── utils.py
│   ├── data/                   # Bundled offline content and lookup datasets
│   ├── drivers/
│   │   ├── printer_raster.py   # Receipt renderer (bitmap for every output)
│   │   ├── print_outputs.py    # Output selection and console capture
│   │   ├── usb_serial_output.py # USB printer output (dev only)
│   │   ├── usb_bulk_printer.py # Native USB printer-class endpoint (PyUSB)
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
├── firmware/                   # XIAO ESP32S3 relay (PlatformIO)
│   ├── platformio.ini          # Board, pins and build flags
│   └── src/                    # WiFi/portal, relay jobs, inputs, OLED, health
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
│   ├── device_event.py         # Local ESP32 input-event client
│   └── artifacts/
├── icons/                      # Icon source assets
├── images/                     # Product photos used in the README
├── .github/workflows/          # docker-image.yml: builds and publishes the image
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

**System Monitor:** Application version, printer output and readiness, ESP32 link and recent job, and `/data` usage.

**Word of the Day:** Merriam Webster's word of the day

**Crossword:** Easy / Medium / Hard; generated crossword puzzle printed as bitmap grid.

**Word Search:** Easy / Hard; generated word search puzzle printed as bitmap grid.

---

## 5. Printer and Device

The server never accesses GPIO or provisions a host serial port. For development,
use `console`, connect a printer over native USB, or use a USB-to-TTL serial
adapter with `PC1_PRINTER_OUTPUT=usb`. For the ESP32 relay protocol, serial framing and
electrical considerations, see [Relay Protocol](#6-relay-protocol-container--esp32)
and [Printer UART](#printer-uart).

### ESP32-S3 relay firmware (`firmware/`)

PlatformIO/Arduino firmware for the **Seeed XIAO ESP32S3**, built and flashed
from the Nix shell (`pio` and `esptool` are included):

```bash
just fw-build                # compile
just fw-flash                # flash over USB (/dev/ttyACM0; or `just fw-flash /dev/ttyACM1`)
just fw-monitor              # USB log
```

Flashing normally needs no button presses; if the port does not respond, hold
**BOOT** while plugging in the board and retry.

| Printer (CSN-A2 TTL) | XIAO ESP32S3 |
| --- | --- |
| RX | TX — D6 / GPIO43 |
| TX | RX — D7 / GPIO44 (not needed for printing; ESP32 pins are **not 5 V tolerant**) |
| GND | GND (shared with the printer's own 5 V supply) |

**Controls:** DFRobot SEN0235 (EC11 rotary encoder with push button), powered
from **3V3** (not 5V: its pull-ups go to VCC):

| Encoder module | XIAO ESP32S3 |
| --- | --- |
| VCC / GND | 3V3 / GND |
| A / B | D0 (GPIO1) / D1 (GPIO2) |
| C (button, low when pressed) | D3 (GPIO4) |

One detent is one channel step (clockwise up, wrapping 8 → 1). The position
starts at **1 on every boot** (it is not saved) and is sent whenever the link to
the server comes up. The button reports `press`, `hold` (5 s) and
`hold_release`. The relay fetches channel names from the server whenever they
change (a hash in each heartbeat reply) and shows the selected channel and its
name on its status page and the OLED.

**Display:** SSD1306 128×64 I²C OLED (0.96", address 0x3C/0x3D), optional:

| OLED module | XIAO ESP32S3 |
| --- | --- |
| VCC / GND | 3V3 / GND |
| SDA / SCL | D4 (GPIO5) / D5 (GPIO6) |

It shows the channel name (or number if unnamed) for 3 s after each knob turn,
the open menu and the option under the knob, the setup network while the
captive portal runs, printing, and no-WiFi / server-offline states; otherwise
a clock with WiFi signal bars top right. The
clock takes UTC from NTP (`pool.ntp.org`, so the network needs internet access)
and the timezone offset and 12h/24h format from the server's settings via the
heartbeat reply. The reset sequence steps also appear on screen. The idle clock
shifts a pixel or two each minute and the screen dims after a minute without
input. Mounted upside down: set `PC1_OLED_ROTATION=2` in
`firmware/platformio.ini`. Without a display the relay works as before.

**Encoder reset sequence** (same as holding BOOT 5 s: erases WiFi/server/token
and reopens setup): hold the knob button **10 s** until the XIAO's user LED
(next to the USB-C connector) lights, release, turn **one detent clockwise**, then **one anticlockwise**
(the LED blinks for each accepted step), then **click**. A wrong turn, an early
press or 10 s without the next step cancels it (LED off). None of these inputs
change the channel or print; at 5 s the usual paper-feed cue still appears. Events go to
`POST /api/device/events` one at a time, retried with the same `event_id`, and
dropped after 5 s rather than replayed late. `just fw-monitor` shows each
input (`[input] ...`) and the server's answer (`[event] ... -> printing_channel`);
`http://<relay>/ui/inputs` shows the current state.

**Setup:** an unconfigured relay opens the open WiFi network
`PaperConsole-XXXXXX`; joining it shows the setup page (or open
`http://192.168.4.1/`). Enter the WiFi network, the server URL as reachable from
the relay (e.g. `http://192.168.1.20:8000`) and the same `PC1_RELAY_TOKEN` as
the server. The network list comes from a scan made before the setup network
starts; **Scan** refreshes it with a short background scan. After a restart the
page is at `http://paper-console.local/` (mDNS; one relay per network) or
`http://<relay-ip>/`, showing the link and printer state, with a
**Print test page** button that prints locally (wiring check without the
server). The page has no login: anyone on the network can change the relay's
settings. The relay token only authenticates requests between the server and
the relay; the page accepts a new one but never shows it. Holding **BOOT** for 5 s
(or the encoder reset sequence above) erases the settings and reopens the
setup network. If the network is
unavailable for 30 s, the setup network comes up again until it reconnects.

The relay sends heartbeats to the server, accepts pushed jobs and writes the
records to the printer at 9600 baud, exactly like `testing/relay_simulator.py`.
For development, the server must listen on the LAN and the host firewall must
allow its port. `just dev-esp32` binds `0.0.0.0:8000` and uses the development
relay token `12345678901234567890` unless `PC1_RELAY_TOKEN` is exported; the
relay must be configured with the same token:

```bash
just dev-esp32                                   # backend on the LAN + settings UI
sudo nixos-firewall-tool open tcp 8000           # NixOS; temporary until reboot
just device position 3 && just device press      # print channel 3 via the relay
```

---

### Known limitations

These were measured on the hardware and accepted:

- **Throughput:** the printer UART runs at 9600 baud (~960 bytes/s). A 64-row
  band is ~3 KB (~3.2 s), so a 500-row receipt takes ~26 s to send. Heat and
  density settings are left at the printer's defaults.
- **Band seams:** receipts are sent as 64-row bitmap bands; the short pause
  between bands can leave faint seams in dithered/grey areas (solid black is
  unaffected).
- **Cancel latency:** cancelling stops at the next band boundary, up to ~3 s
  after the 5 s hold.
- **Physical printer state is unknown:** paper-out, power and busy signals are
  not wired or queried. "Ready" and a completed job mean the bytes left the
  UART, not that paper was printed.
- **No offline queue:** when the relay is unreachable, prints are skipped and
  logged, never replayed later. A job interrupted by a relay restart fails.
- **One relay per network** (mDNS name `paper-console.local`) and one Uvicorn
  worker (scheduling and printer reservations are process-local).
- **Clock:** needs internet access for NTP; the OLED shows `--:--` until NTP
  and the first heartbeat have arrived. Channel names and menus use Latin
  fonts; other characters show as `?`.
- **Relay settings page has no login:** anyone on the network can change WiFi,
  server and token (the token is never displayed).

---

## 6. Relay Protocol (Container ↔ ESP32)

This is the contract between the container and the relay. The container side is
`app/relay_link.py`, `app/device_input.py` and
`app/drivers/esp32_relay_output.py`; the device side is `firmware/` and its
reference implementation `testing/relay_simulator.py`, which the container tests
run against. Keep all three and this section in step.

```text
Button / rotary encoder -> ESP32-S3 --HTTP input event--> container
CSN-A2 printer <- TTL UART <- ESP32-S3 <--HTTP print job-- container
```

The **container** owns channel selection, what gestures mean, menus and
interactive modules, scheduling, content and CSN-A2 command generation. The
**firmware** owns input sampling/debouncing, WiFi provisioning, link reporting,
bounded buffering and UART pacing; it never renders receipts or knows module
logic. Scheduled, email and Slack prints use the same container-initiated push.
Both directions must be reachable on the LAN (Docker port mapping for
device → container; outbound LAN access for container → device).

| Direction | Requests |
| --- | --- |
| Device → container | `POST /api/device/heartbeat`, `POST /api/device/events`, `GET /api/device/channels`, `POST /api/device/jobs/{id}/status` |
| Container → device | `POST /jobs`, `PUT /jobs/{id}/chunks/{n}`, `POST /jobs/{id}/cancel`, `GET /jobs/{id}`, `POST /menu` |
| Public on the device | `GET /health`, the settings page (`/`, `/ui/*`) |

### Authentication

One shared bearer token, `Authorization: Bearer <token>`, in both directions.
The container reads `PC1_RELAY_TOKEN` or `PC1_RELAY_TOKEN_FILE` (at least 16
characters, no whitespace; an unusable configured token stops startup). Without
one, device endpoints answer `503`. The relay gets the same token in its setup
page. The token is separate from the settings password. Generate one with
`openssl rand -hex 24`. The device answers `401` to a missing or wrong token.

### Input events (device → container)

`POST /api/device/events`, one at a time:

```json
{"event_id": "a3f9c2-41", "type": "position", "position": 5}
{"event_id": "a3f9c2-42", "type": "button", "gesture": "press", "press_id": "a3f9c2-17", "position": 5}
```

| Field | Rules |
| --- | --- |
| `event_id` | 1–64 chars, unique **across reboots** (random boot id + counter). |
| `type` | `position` (absolute 1–8, sent whenever it changes) or `button`. |
| `position` | Required for `position`; optional but recommended on `button` (applied first, so a lost position event cannot misdirect a press). |
| `gesture` | `press` (released before 5 s), `hold` (5 s reached, sent once), `hold_release` (released after `hold`), `hold_cancel` (the device kept the press for a local action, e.g. its reset sequence at 10 s; never followed by `hold_release`). |
| `press_id` | String or integer, 1–64 chars; the same for every gesture of one press. |

Unknown fields are ignored; malformed events get `422`.

| Gesture | Container state | Result |
| --- | --- | --- |
| `press` | Menu open | `selection`, or `selection_ignored` for a position that is not an option (nothing prints) |
| `press` | Otherwise | `printing_channel` |
| `hold` | Printing | `cancel_requested`: stops at the next band boundary; the rest of the press is ignored |
| `hold` | Idle | `hold_ready`: short paper feed ("release now") |
| `hold_release` | After `hold_ready` | `quick_actions`: prints and opens Quick Actions |
| `hold_cancel` | Any | `hold_cancelled`: releases the armed hold; a later `hold_release` is ignored |

Other results (all HTTP `200`): `position_set`; `busy` (another job holds the
printer, or a press within 3 s of the last print; nothing is queued);
`printer_unavailable`; `ignored_consumed` (`hold_release` of a press that
cancelled a print or was `hold_cancel`led); `not_ready` (server starting);
`error` (logged). Response:
`{"event_id": "...", "result": "printing_channel", "position": 5, "duplicate": false}`,
returned as soon as the gesture is interpreted.

Delivery rules: send in order and wait for each response; on a timeout retry the
**same** `event_id` (the container remembers the last 256 and answers with the
original result and `"duplicate": true`); drop events older than a few seconds
instead of replaying them (the reference firmware drops after 5 s); on `401`/`503`
show the error and back off. Debouncing and the 5 s threshold are firmware jobs.

### Heartbeat (device → container)

`POST /api/device/heartbeat` every `heartbeat_interval_s`, and immediately after
boot, WiFi reconnect and any `printer.state` change:

```json
{"device_id": "pc1-a1b2c3", "boot_id": "7f3e91c0d2aa", "firmware": "0.1.0",
 "callback_url": "http://192.168.1.50",
 "printer": {"state": "ready", "detail": null, "width_dots": 384, "baudrate": 9600},
 "limits": {"max_chunk_bytes": 8192, "max_job_bytes": 1048576},
 "active_job_id": null, "health": {"schema_version": 1, "...": "..."}}
```

| Field | Rules |
| --- | --- |
| `device_id` / `boot_id` | 1–64 chars; `boot_id` is new on every boot (a change means the device restarted). |
| `callback_url` | Device base URL as reachable **from the container** (http/https, no credentials/query/fragment). `PC1_RELAY_URL` can pin it instead. |
| `printer.state` | `ready`, `busy`, `paper_out`, `error` or `offline`; only `ready`/`busy` accept jobs. `detail` ≤200 chars. |
| `printer.width_dots` / `baudrate` | 8–2048 (wider receipts are rejected) / 1200–921600 (sizes job timeouts). |
| `limits.max_chunk_bytes` | 256 B–1 MiB. A framed 64-row band is 3,081 bytes at 384 dots; smaller limits shorten bands in multiples of 8 rows (minimum 393 bytes). |
| `limits.max_job_bytes` | 1 KiB–64 MiB. |
| `active_job_id` | Job being processed, if any. |
| `health` | Optional [health object](#device-health). |

Response:

```json
{"heartbeat_interval_s": 10, "heartbeat_timeout_s": 30,
 "channels_hash": "3f1c9a0b7d2e4c61",
 "clock": {"utc_offset_s": 19800, "time_format": "12h"},
 "menu": {"seq": 1792345679000000, "active": false}}
```

The container considers the device offline after `heartbeat_timeout_s` without a
heartbeat or as soon as a push fails; the next heartbeat brings it back.
`clock` is the configured timezone's current UTC offset (DST applied) and the
12h/24h preference; the device takes UTC from NTP and adds the offset, so
timezone and DST changes arrive within one heartbeat.

### Channel names (device ← container)

`channels_hash` is a 16-hex hash of the eight names in order; it changes only
when a name changes. When it differs from the device's copy (or after boot),
fetch `GET /api/device/channels`:

```json
{"hash": "3f1c9a0b7d2e4c61",
 "channels": [{"position": 1, "name": ""}, {"position": 2, "name": "Work update"}, ...]}
```

Store the names with **this** `hash` (a name may change between the two
requests). On failure keep the old copy and retry next heartbeat. Names are
UTF-8, ≤24 characters, `""` = unnamed; RAM only on the device.

### Menu display (container → device)

While a menu is open a press chooses an option instead of printing the channel.
The container pushes the state whenever a menu opens or closes:

```json
POST /menu
{"seq": 1792345678901234, "active": true, "title": "Quick actions",
 "options": [{"position": 1, "label": "Table of contents"},
             {"position": 2, "label": "System monitor"},
             {"position": 8, "label": "Cancel"}]}
```

- `options` lists only positions that act; others are `selection_ignored`.
  Titles ≤24 characters, labels ≤48 (UTF-8).
- `seq` increases on every change, also across container restarts (wall-clock
  microseconds). **Keep a state only if its `seq` is higher than the one held.**
- The push is best effort (short timeout, no retry); every heartbeat reply
  carries the same object as `menu`, so the device catches up within one
  heartbeat or after rebooting. The reference firmware answers
  `{"ok": true, "applied": <bool>}`; firmware without a display may `404`.
- RAM only on both sides. A menu opens after its receipt has printed; Adventure
  closes its menu while printing the next node.

### Print jobs (container → device)

A receipt becomes **records**, each self-contained printer data: the `FS .`
preamble, one record per 64-row bitmap band (see [Printer UART](#printer-uart)),
and the cutter feed (`ESC J`). The device writes records to the UART
**unchanged and in order** and never parses them. Cancellation happens only
**between** records. Tactile feeds (`kind: "feed"`) are tiny jobs the container
does not wait for.

**1. Create:** `POST /jobs`
`{"job_id": "3f2a…", "kind": "receipt", "chunks": 3, "records": 24, "bytes": 25184}`
(`bytes` = all chunk bodies; `job_id` 1–64 chars of `[A-Za-z0-9_-]`).

| Response | When |
| --- | --- |
| `201` + status | Created. |
| `200` + status | Same id exists; never start it twice. |
| `409` | Another job is active (the container retries for 3 s). |
| `413` / `503` / `400` | Too large / printer not ready / malformed. |

**2. Chunks:** `PUT /jobs/{id}/chunks/{index}`, `application/octet-stream`,
header `X-PC1-Final-Chunk: 0|1`, body = `[2-byte big-endian length][record]`
repeated. Sent in order from 0; records never span chunks; bodies ≤
`max_chunk_bytes`. Printing may start with the first chunk.

| Response | When |
| --- | --- |
| `200` | Accepted; a resent index returns `"duplicate": true` and is not printed again. |
| `429` + `Retry-After-Ms` | Buffer full; the container backs off (≤2 s each, 15 s total) and resends. Use this instead of blocking while the UART drains. |
| `409` / `404` | Out of order or job finished / unknown job (e.g. after a reboot; the job fails). |
| `413` / `400` | Body too large / bad framing. |

**3. Status:** device → `POST /api/device/jobs/{id}/status`
`{"state": "printing", "boot_id": "...", "records_written": 1, "bytes_written": 2}`.
States: `receiving`, `printing` (report once when writing starts),
`completed` (all records written and the UART drained: delivery, **not** proof
of paper), `cancelled` (skip the cutter feed), `failed` (with `error` ≤300
chars). Final states never change; `404` means stop reporting that job.

**4. Cancel:** `POST /jobs/{id}/cancel`: stop after the current record, report
`cancelled`, answer `200` with the status even if already finished.

**5. Poll:** `GET /jobs/{id}` returns the status body or `404`. The container
polls every 5 s in case a callback was lost; keep the last few finished jobs.

Rules:

- **One job at a time.** The container holds its print reservation until a
  final state. The device must keep serving events, heartbeats and status while
  printing (UART writer separate from the HTTP server).
- **Timeouts:** 30 s plus twice the theoretical UART time
  (`bytes × 10 / baudrate`), then the job fails and a cancel is sent.
- **No replay:** a reboot mid-job (new `boot_id`) fails the job and nothing is
  resent; the device never resumes jobs. Status reports with another `boot_id`
  are ignored.
- **Offline:** prints are skipped, not queued (UI/API `503`; scheduled, email and
  Slack prints logged and dropped). The first print after a silent disappearance
  fails when its push fails.

### Device health

`GET /health` on the relay is public (HTTP 200, `Cache-Control: no-store`) and
reads cached state only. Schema 1 fields: `schema_version`, `device_id`,
`boot_id`, `firmware`, `uptime_s` (64-bit timer), `status`, `wifi_connected`,
`server_state` (`connected`/`disconnected`/`unknown`), `server_detail`,
`server_last_ok_s` (-1 = never), `uart_ready`, `physical_printer` (always
`unknown`), `active_job_id`/`_state`, `last_job_id`/`_state`/`_error` (RAM only),
and optional `display` (`ok`/`absent`). Empty strings mean none. No token, WiFi
password, SSID or server URL is included.

`status` priority: UART/writer unavailable → `error`; WiFi or server link down
(no acknowledgement for 30 s) → `disconnected`; active job → `printing`; last
job failed → `error`; otherwise `ready`. The same object appears in `/ui/status`
and in heartbeats. The container's `GET /api/printer/link` (admin) adds
`health_stale` (report older than 20 s, missing heartbeats or offline link);
old firmware reports `health: null`.

### Printer UART

| Item | ESP32-S3 to CSN-A2 |
| --- | --- |
| UART | Hardware UART1, **9600 8N1**, no flow control; not the USB log console. |
| TX / RX | XIAO TX (GPIO43) → printer RX; printer TX → GPIO44 (optional; GPIO is **not 5 V tolerant**). |
| Busy (DTR) | Not wired or read; paper-out and power are unknown to the relay. |
| Power | Common ground; separate printer supply rated for ≥4–5 A peaks. |
| Paper | 58 mm, 384 dots at 203 dpi (48 bytes per row). |

On boot (never on container start) the firmware initialises the printer once:
`00 00 00 00 00` (flush a partial command, wait 100 ms), `1B 40` (`ESC @`,
wait 300 ms), `1C 2E` (`FS .`, cancel double-byte mode), `1B 52 00`
(USA charset), `1B 74 00` (PC437).

The container rotates the 1-bit receipt 180° for the mounting direction and
sends independent **64-row downloaded bitmap bands**, each one record:
`1D 2A 30 08 <3072 bytes> 1D 2F 00` (`GS * x=48 y=8` in 8-dot units,
column-major, MSB first, set bit = black; then `GS / 0` to print at normal
size). The printer receives each band into RAM before printing it. The last
band is padded with up to seven white rows. The relay writes in ~128-byte
slices and waits for the UART to drain at each band boundary. A short UART write
fails the job with its byte count. After a successful receipt the container
sends `ESC J 120` (five lines) for tear clearance; tactile blips use `ESC J 2`.

Why 64-row bands: streaming `GS v 0` raster at 9600 baud stretched images
vertically and lightened dithering, while the same bitmap over fast native USB
stayed square. Slow delivery is the trigger (the printer's internal cause is
unknown). Downloaded 24/48/64-row bands printed correctly over slow USB and
64-row bands over the real UART (calibration squares measured 24×24 mm on both);
128- and 256-row bands printed garbage. Heat and density stay at defaults. Native
USB development output keeps 24-row `GS v 0` strips.
`testing/print_raster_calibration.py --encoding downloaded --downloaded-band-rows 64`
reprints the calibration pattern (`--output usb --usb-bytes-per-second 960` or
`--output esp32`).

### Developing against the protocol

```bash
just dev-esp32                                 # terminal 1: backend + settings UI
just relay-sim                                 # terminal 2: simulated ESP32 at 9600 baud
just device position 5 && just device press    # terminal 3: the device's controls
just device quick-actions                      # hold, then hold_release
just device hold                               # during a print: cancel
just device hold-cancel                        # hold, then hold_cancel
```

The recipes share the development relay token `12345678901234567890` unless
`PC1_RELAY_TOKEN` is exported. The simulator saves each received receipt, decoded
from the raster bytes, as a PNG under `testing/artifacts/relay/`; `--baud 0`
prints instantly, and `--port`/`--advertise-url` help if the container cannot
reach `127.0.0.1:8090`. It also logs channel names and menus it receives.

### Hardware validation

On 2026-10-18 the complete system passed hardware acceptance on the XIAO
ESP32S3, CSN-A2 printer and production container: boot/channel sync, channel
names, health and stale-status handling, prints from the UI/button/schedule/test
page, long-receipt responsiveness, hold-to-cancel, backend/WiFi/relay restarts
without duplicate or replayed output, container recreation with persisted
`/data`, the encoder and BOOT resets, the OLED screens and menus. Duplicate
creates/chunks/events, backpressure and lost callbacks are covered by the
simulator tests. Re-check affected items on hardware after firmware or protocol
changes.

---

## 7. Troubleshooting

### Printer Issues
* **Status dot:** The circle beside the settings title is green when the ESP32 is
  online and ready, amber while printing, when heartbeats are late or with
  development `console` output, and red when the relay or printer output is
  unavailable. Hover/tap it for a one-line reason. Green means the relay accepts
  jobs; it does not prove paper is loaded. For details use the backend log,
  the ESP32 page (`http://paper-console.local/`) or `just fw-monitor`.
* **Relay offline:** Check `GET /api/printer/link` or Printer Connection in General
  Settings, the relay's `http://<relay-ip>/health`, and `just fw-monitor`. Start
  `just relay-sim` to exercise the HTTP relay without firmware.
* **USB printer not found:** For native USB, check `lsusb`, device permissions
  (`/dev/bus/usb/BUS/DEVICE`) and any permission error in the backend log. For
  a USB-to-serial adapter, check `PC1_PRINTER_PORT` and serial access permissions
  (usually the `dialout` group).
* **No output in development:** Start with `PC1_PRINTER_OUTPUT=console just backend`
  and watch the `[PRINT]` and `[RECEIPT]` log entries.

### Relay and OLED
* **Relay log:** `just fw-monitor` shows `[wifi]`, `[link]`, `[input]`,
  `[event]`, `[menu]`, `[display]` and `[time]` lines.
* **OLED blank / "no SSD1306":** the boot log scans the bus. "With SDA/SCL
  swapped: 0x3c" means the two wires are crossed (SDA must go to D4, SCL to
  D5); the firmware then uses them as wired. Upside down: `PC1_OLED_ROTATION=2`.
  A 1.3" module is usually an SH1106, not an SSD1306, and needs a different
  driver.
* **Clock shows `--:--`:** the relay has no NTP time (no internet access) or no
  heartbeat reply from the server yet.
* **Knob shows channel names but the press picks a menu option:** the relay
  missed a menu update; it catches up within one heartbeat (10 s). Check
  `[menu]` lines in the relay log.
* **Link shows "server rejected the relay token (401)":** the relay and
  `PC1_RELAY_TOKEN` differ; re-enter the token on the relay page.

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
