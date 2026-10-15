# PC-1 (Paper Console 1)

**A small, self-hosted thermal printer for news, weather, puzzles, email, and more.**

> This fork is migrating to a container + ESP32-S3 architecture. The container
> handles application logic and pushes print commands over HTTP; the ESP32 will
> provide WiFi setup/link-status UI, relay physical inputs over HTTP, and drive a
> CSN-A2 printer over TTL UART. See the [migration plan](docs/container-esp32-plan.md)
> for implementation progress. ESP32 transport/firmware is not implemented yet.

---

* **No screens:** output is physical thermal paper.
* **No subscriptions:** user-owned API keys or local algorithms.
* **Quality materials:** walnut, brass, and archival-grade paper.
* **Universal channels:** fully configurable channels (Adventure, Astronomy, Calendar, Email, History, Journal Prompt, Maze, News, Print Endpoint, QR Code, Quotes, RSS, Sudoku, System Monitor, Text / Note, Weather, Webhook)

For more information and build photos, visit [travismiller.design/paper-console](https://travismiller.design/paper-console).

![PC-1 Front View](images/pc-1_front2.jpg)
*Front view showing the brass rotary dial, push button, and thermal paper output*

---

## 1. Quick Start (Software)

Run the entire system on your PC without hardware to test logic and see "printer" output in the terminal.
Use a Unix-like shell for project commands: Linux, macOS, or WSL on Windows

**Backend:**

```bash
./run.sh

# Or run directly
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

* **API Docs:** 
  * Local: [http://localhost:8000/docs](http://localhost:8000/docs)
* **Mock Output:** Watch your Terminal window. The "Printer" writes text there.

**Frontend (Settings UI):**
```bash
cd web
npm install
npm run dev
```
* **URL:** [http://localhost:5173](http://localhost:5173)

**Tests:**
```bash
./testing/run_tests.sh
```

### First Use (This Fork)

The container does not manage WiFi, run a captive portal, or print setup
receipts. Network onboarding moves to the ESP32-S3 firmware (captive portal and
link-status UI), which is not implemented yet.

1. Start the server with `PC1_DEVICE_PASSWORD` set (see the development section below).
2. Open the settings UI and log in with that password.
3. Configure channels, modules, and schedules.

**Physical controls** (relayed by the ESP32 once implemented; currently mock/GPIO):

- Turn the dial to select a channel (1-8); press the button to print it.
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

# Backend (mock hardware on non-Pi hosts)
export PC1_DEVICE_PASSWORD='choose-your-local-server-password'
just backend

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

The flake provides `just`, Python 3.12, the application/test dependencies, Node.js 22
(including npm), and launcher utilities on Linux and macOS, for x86-64 and ARM64.
`flake.lock` pins the Nix package set; frontend dependencies use
`web/package-lock.json`. The first shell entry downloads the Nix dependencies;
`npm ci` separately installs the frontend dependencies.

The recipes use Python from the shell rather than
`testing/run_tests.sh`, which creates a pip-managed `.venv`. Likewise, `run.sh`
prefers an existing `.venv`, so the backend recipe ensures it uses the
Nix-provided Python. Enter the shell without an activated virtual environment.
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
   This installs the app dependencies plus test tooling. It intentionally excludes Raspberry Pi-only GPIO packages.

2. **Run the Server:**
   ```bash
   ./run.sh
   # Or: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
   ```

### Docker (Software-Only / Mock Printer)

If you want to run PC-1 without Raspberry Pi hardware, you can also run the app in Docker and use the built-in terminal "printer" output for testing.

1. **Create a `Dockerfile` in the repo root:**
   ```Dockerfile
   FROM python:3.12-bookworm

   WORKDIR /paper-console

   COPY . .

   RUN apt update && apt install -y sudo npm nodejs network-manager

   RUN pip install Pillow
   RUN pip install fastapi uvicorn[standard] pydantic requests
   RUN pip install feedparser icalendar pytz python-dateutil astral beautifulsoup4
   RUN pip install pytest

   RUN python3 -m venv .venv; ./.venv/bin/python -m pip install -r requirements-dev.txt

   ENTRYPOINT ["./run.sh"]
   ```

2. **Build the image:**
   ```bash
   docker buildx build -t local/pc-1 .
   ```

3. **Run the container:**
   ```bash
   docker run --rm -it -p 8000:8000 local/pc-1
   ```

4. **Open the app:**
   Visit `http://localhost:8000` in your browser.

5. **Set the server password:**
   Pass `-e PC1_DEVICE_PASSWORD=...` to `docker run` and use it to log in. (Proper
   container packaging is planned in Step 4 of the migration plan.)

6. **Use the mock printer:**
   When you print a channel from the web UI, the output appears in the terminal running the container.

### Raspberry Pi Deployment

1. **Install Python Dependencies:**
   ```bash
   ./.venv/bin/python -m pip install -r requirements-pi.txt
   ```

2. **Run Setup Script:**
   ```bash
   cd ~/paper-console
   chmod +x scripts/setup_pi.sh
   sudo scripts/setup_pi.sh
   ```
   The script will:
   - Set hostname (default: `pc-1`)
   - Install Nginx (web proxy) and Avahi (mDNS)
   - Configure systemd service
   - Add user to `lp` group for printer access

3. **Access the Device:**
   Open your browser and go to `http://pc-1.local` (or your chosen hostname).

---

## 3. Configuration

Configuration is handled entirely via the **Web UI** at `http://pc-1.local` (or `http://localhost:8000`).

### Global Settings
* **Location:** City name, Latitude, Longitude, Timezone (with search functionality)
* **Time Format:** 12-hour (AM/PM) or 24-hour format

### Channel System
* **8 Channel Positions:** Each position (1-8) represents a slot on the rotary dial
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
For non-Pi development, use the local test and snapshot workflows:

* **Pytest:** `./testing/run_tests.sh`
* **Print snapshots:** `./.venv/bin/python testing/render_all_prints.py --output-dir testing/artifacts/print`
* **Settings UI snapshots:** `./.venv/bin/python testing/render_settings_ui.py --output-dir testing/artifacts/ui`

These workflows use mock/local paths and do not require Raspberry Pi GPIO packages.

### Security & Network Environment Variables
For production deployments, configure these environment variables:

* **`PC1_DEVICE_PASSWORD`**: Server password (min. 8 characters) for settings login and the `X-PC1-Device-Password` API header.
* **`PC1_DEVICE_PASSWORD_FILE`**: Alternative to the above: path to a file containing the password (e.g. a Docker secret).
  If neither is set, a random password is generated on first start, saved to `.device_password` (mode 0600; override the path with `PC1_GENERATED_PASSWORD_FILE`), and printed once in the server log.
  The password is an application credential only; it is not synced to OS accounts, and changing it means changing the environment/secret and restarting.
* **`PC1_SESSION_SECRET`**: Optional key for signing login cookies. Defaults to the password, so changing the password logs out existing sessions.
* **Settings Login Sessions**: The web UI can remember a browser with a signed `HttpOnly` session cookie so users do not need to re-enter the password on every visit.
* **`PC1_CORS_ORIGINS`**: Comma-separated CORS origins (default is local/dev origins only).
* **`PC1_LOG_LEVEL`**: Backend log level (default: `WARNING` on device builds).
* **`UVICORN_LOG_LEVEL`**: Uvicorn log level for `run.sh` (default: `warning`).
* **`UVICORN_ACCESS_LOG`**: Set to `1` to enable HTTP access logs (default: `0` for lower log volume).

### Production Release Artifacts

For non-git production units, build versioned release artifacts with:

```bash
./.venv/bin/python scripts/release_build.py --version v1.2.3 --build-web
```

In-place OTA updates were removed from the server in this fork; updates will ship
as container images (Step 4). These scripts are legacy Pi tooling slated for removal
in Step 5. See `scripts/RELEASE.md` for the current stable and beta release paths, including the tag-driven GitHub publish workflow, the recommended `main` and `beta` branch lanes, and hotfix guidance for stable OTA releases.

---

## 4. Architecture & Modules

### Tech Stack
* **OS:** Raspberry Pi OS Lite (64-bit)
* **Backend:** Python 3.12 + FastAPI
* **Frontend:** React + Vite + Tailwind CSS v4

### Directory Structure
```
paper-console/
├── app/
│   ├── main.py                 # FastAPI app, dial/button loop, print orchestration
│   ├── auth.py                 # Device-password auth and session helpers
│   ├── config.py               # Settings, channels, schedules, module models
│   ├── device_password.py      # Server password (env, secret file, or generated)
│   ├── factory_reset.py        # Application settings reset (no host changes)
│   ├── hardware.py             # Printer + dial/button singletons
│   ├── location_lookup.py      # City/timezone lookup helpers
│   ├── module_registry.py      # @register_module metadata and validation
│   ├── selection_mode.py       # Dial-driven interactive selection flows
│   ├── qr_payloads.py          # QR content encoders (WiFi/contact QR modules)
│   ├── utils.py
│   ├── data/                   # Bundled offline content and lookup datasets
│   ├── drivers/
│   │   ├── printer_serial.py   # Thermal printer (hardware)
│   │   ├── printer_mock.py     # Console / capture (dev)
│   │   ├── dial_gpio.py / dial_mock.py
│   │   ├── button_gpio.py / button_mock.py
│   │   └── gpio_ioctl.py
│   ├── modules/                # Printable module implementations
│   │   ├── adventure.py        # Interactive CYOA (dial)
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
│   ├── setup_pi.sh
│   ├── release_build.py
│   ├── prepare_golden_image.sh
│   ├── deploy_automated.py
│   ├── validate_icons.py
│   └── RELEASE.md / DEPLOYMENT.md
├── testing/                    # Pytest, print/UI render helpers, snapshot galleries
│   ├── render_all_prints.py
│   ├── render_settings_ui.py
│   ├── console_raster_preview.py
│   ├── run_tests.sh
│   └── artifacts/
├── development/                # BOM, internal docs, supporting scripts
├── icons/                      # Icon source assets
├── images/                     # Product photos used in the README
├── raspberry_pi/               # Misc device imaging/install artifacts
├── .github/workflows/
├── .env.example
├── .deploy_config.example
├── AGENTS.md
├── config.json                 # Local settings (gitignored in normal use)
├── deploy.sh
├── run.sh
├── requirements.txt
├── requirements-dev.txt
├── requirements-pi.txt
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

## 5. Hardware Setup

### Prerequisites
1. **Raspberry Pi Zero 2 W** with Raspberry Pi OS Lite installed
2. **Thermal Printer** (QR204/CSN-A2 or compatible 58mm TTL thermal printer. 30mm diameter paper roll maximum)
3. **1-Pole 8-Position Rotary Switch**
4. **Momentary Push Button** (x1)
5. **Power Supply:** 5V 5A Power Supply (Barrel Jack) -> Terminal Adapter

### Complete Wiring Tables (Component-Based)

This layout organizes connections into logical groups (blocks) for easier assembly with standard connectors (DuPont or JST).

#### 1. Power & Printer Data Block (Top)
*Use a 4-pin or 5-pin connector block.*

| Pin | GPIO | Function | Wire To |
| :---: | :---: | :--- | :--- |
| **2** | 5V | **Power Input (+)** | External PSU 5V (+) |
| **6** | GND | **Power Input (-)** | External PSU GND (-) |
| **8** | 14 | **TX (Data)** | Printer RX |
| **10** | 15 | **RX (Data)** | Printer TX |
| **12** | 18 | **DTR (Flow)** | Printer DTR |
| **14** | GND | **Signal GND** | Printer GND |

#### 2. Main Button Block (Middle)
*Use a 2-pin connector.*

| Pin | GPIO | Function | Wire To |
| :---: | :---: | :--- | :--- |
| **20** | GND | **Button GND** | Button Pin 1 |
| **22** | 25 | **Button Signal** | Button Pin 2 |

#### 3. Rotary Dial Block (Bottom)
*Use a 12-pin (2x6) connector block.*

| Pin (Left) | GPIO | Function | | Pin (Right) | GPIO | Function |
| :---: | :---: | :--- | :--- | :---: | :---: | :--- |
| **29** | 5 | **Pos 1** | \| | **30** | GND | *Unused* |
| **31** | 6 | **Pos 2** | \| | **32** | 12 | *Unused* |
| **33** | 13 | **Pos 3** | \| | **34** | GND | *Unused* |
| **35** | 19 | **Pos 4** | \| | **36** | 16 | **Pos 6** |
| **37** | 26 | **Pos 5** | \| | **38** | 20 | **Pos 7** |
| **39** | GND | **Common** | \| | **40** | 21 | **Pos 8** |

### Visual Pin Layout
Legend: `[X]` = Used, `[ ]` = Empty. The header is 2 pins wide.

```
      (Pin 1) [ ] [X] (Pin 2)  --> Power 5V (+)
      (Pin 3) [ ] [ ] (Pin 4)
      (Pin 5) [ ] [X] (Pin 6)  --> Power GND (-)
      (Pin 7) [ ] [X] (Pin 8)  --> Printer TX
      (Pin 9) [ ] [X] (Pin 10) --> Printer RX
     (Pin 11) [ ] [X] (Pin 12) --> Printer DTR
     (Pin 13) [ ] [X] (Pin 14) --> Printer GND
     (Pin 15) [ ] [ ] (Pin 16)
     (Pin 17) [ ] [ ] (Pin 18)
     (Pin 19) [ ] [X] (Pin 20) --> Button GND
     (Pin 21) [ ] [X] (Pin 22) --> Button SIG
     (Pin 23) [ ] [ ] (Pin 24)
     (Pin 25) [ ] [ ] (Pin 26)
     (Pin 27) [ ] [ ] (Pin 28)
     (Pin 29) [X] [ ] (Pin 30) --> Dial 1
     (Pin 31) [X] [ ] (Pin 32) --> Dial 2
     (Pin 33) [X] [ ] (Pin 34) --> Dial 3
     (Pin 35) [X] [X] (Pin 36) --> Dial 4 | Dial 6
     (Pin 37) [X] [X] (Pin 38) --> Dial 5 | Dial 7
     (Pin 39) [X] [X] (Pin 40) --> Dial COM| Dial 8
```

### Thermal Printer

**TTL Serial Connection:**
1. Wire according to table above
2. **Serial port is automatically configured** by `scripts/setup_pi.sh` (disables console, enables hardware)
3. Device appears as `/dev/serial0` after setup

### Power Supply

**Recommended Setup (Split Power):**
1.  **Source:** Use a 5V 5A power supply (Barrel Jack).
2.  **Distribution:** Use a custom PCB HAT or a terminal splitter to feed power to two places simultaneously:
    *   **To Pi:** Connect to GPIO Pins 2 (5V) and 6 (GND).
    *   **To Printer:** Connect to the printer's power input cable.
3.  **Warning:** Do **not** plug the power supply into the Pi's USB port and try to power the printer from the Pi's GPIO pins. The printer draws too much current (up to 4A) and will crash the Pi.

---

## 6. Troubleshooting

### Printer Issues
* **Device Not Found:**
  * Check if `/dev/serial0` exists: `ls -l /dev/serial*`
  * If missing, run setup script: `sudo scripts/setup_pi.sh` (configures serial automatically)
  * Or manually: `sudo raspi-config` → Interface Options → Serial Port → Enable hardware, Disable console
* **Permission Denied:**
  * Ensure user is in `dialout` group: `groups`
  * Add user: `sudo usermod -a -G dialout $USER` (then log out/in)
* **Nothing Prints:**
  * Verify power supply is adequate (5A minimum)
  * Confirm printer is connected to correct GPIO pins using the wiring table above
  * Make sure the printer is powered on (indicator lights as expected)
  * Check for error/status lights or messages on the printer itself (if available)
  * Test the printer with a simple echo command: `echo "test" > /dev/serial0`
  * Check service logs: `sudo journalctl -u pc-1.service -f`
  * Confirm no other application is holding the serial port open (use `lsof /dev/serial0`)

### Service Issues
  * **Restart Loop:**
  * Service uses `run.sh` to handle port conflicts automatically
  * Restart: `sudo systemctl restart pc-1.service`
  * Check logs: `sudo journalctl -u pc-1.service -f`
  * **Port Already in Use:**
  * `run.sh` automatically kills zombie processes
  * If persistent, reboot: `sudo reboot`
  * **Log Storage:**
  * Setup script configures journald in RAM (`Storage=volatile`) with a bounded budget (`RuntimeMaxUse=16M`).
  * This minimizes SD writes and prevents long-term log growth on disk.
  * Setup script also installs `pc1-storage-guard.timer` (runs every 6 hours) to trim journals/cache if root usage rises.
  * Core dumps are disabled (`/etc/systemd/coredump.conf.d/pc-1.conf`) to prevent multi-megabyte crash artifacts.
  * Check current journal usage: `journalctl --disk-usage`
  * Check storage guard timer: `systemctl status pc1-storage-guard.timer`
  * Run storage guard immediately: `sudo systemctl start pc1-storage-guard.service`
  * Temporarily enable more logs for debugging by setting:
    * `PC1_LOG_LEVEL=INFO`
    * `UVICORN_ACCESS_LOG=1`

### Module-Specific Issues
* **NewsAPI Returns 0 Articles:** Check if API key is valid and on free tier
* **RSS Feeds Not Working:** Ensure RSS feed URLs are valid and accessible
* **Email Auth Failed:** Use a **Google App Password**, not your main password. Ensure 2FA is enabled
* **Calendar Not Showing Events:** Verify iCal URL is correct. Check that events exist in the date range
* **Weather Wrong:** Check `config.json` Lat/Long via UI location search feature

---
