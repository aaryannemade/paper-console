# PC-1 (Paper Console 1)

**A small, self-hosted thermal printer for news, weather, puzzles, email, and more.**

> This fork is migrating to a container + ESP32-S3 architecture. The server
> handles application logic and pushes print commands over HTTP. The ESP32-S3
> firmware will provision WiFi, relay physical inputs, and drive a CSN-A2 printer
> over TTL UART. The server-side relay works with the simulator and the server
> has a container image; firmware is still planned for Step 6. See the
> [migration plan](docs/container-esp32-plan.md).

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
  - `8` Cancel
- Quick Actions are single-shot and auto-exit after 2 minutes without a choice.

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

The proposed container/ESP32-S3 migration is documented in
[the migration plan](docs/container-esp32-plan.md).

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
`GS v 0` format. See [printer UART reference](docs/printer-uart-reference.md)
for calibration results and remaining band-boundary seams.

| Value | Use | Behavior |
| --- | --- | --- |
| `esp32` | Production (default) | Pushes jobs to the ESP32-S3 relay over HTTP ([protocol](docs/relay-protocol.md)). Needs `PC1_RELAY_TOKEN` and heartbeats from the device; `PC1_RELAY_URL` optionally pins the device URL. Develop against `just relay-sim`. |
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

### Container deployment (Step 4)

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
  export/import, and are set with `PUT /api/channels/{position}/name`
  (`{"name": "..."}`). They are intended for a future ESP32 display.
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
* **`PC1_RELAY_TOKEN`** / **`PC1_RELAY_TOKEN_FILE`**: Shared bearer token for the ESP32 relay (at least 16 characters), used in both directions. Without it, `/api/device/events` answers 503. See [docs/relay-protocol.md](docs/relay-protocol.md).
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
│   ├── paths.py                # Persistent application data directory
│   ├── version.py              # Image or development checkout version
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
│   ├── device_event.py         # Local ESP32 input-event client
│   └── artifacts/
├── docs/                       # Relay protocol and container/ESP32 plan
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
electrical considerations, see [printer UART reference](docs/printer-uart-reference.md)
and [relay protocol](docs/relay-protocol.md).

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
name on its status page; they are kept for a future display.

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

## 6. Troubleshooting

### Printer Issues
* **Status dot:** The circle beside the settings title is green when the ESP32 is
  online and ready, amber while printing, when heartbeats are late or with
  development `console` output, and red when the relay or printer output is
  unavailable. Hover/tap it for a one-line reason. Green means the relay accepts
  jobs; it does not prove paper is loaded. For details use the backend log,
  the ESP32 page (`http://paper-console.local/`) or `just fw-monitor`.
* **Relay offline:** Check `GET /api/printer/link` or Printer Connection in General
  Settings. Start `just relay-sim` to exercise the HTTP relay without firmware.
* **USB printer not found:** For native USB, check `lsusb`, device permissions
  (`/dev/bus/usb/BUS/DEVICE`) and any permission error in the backend log. For
  a USB-to-serial adapter, check `PC1_PRINTER_PORT` and serial access permissions
  (usually the `dialout` group).
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
