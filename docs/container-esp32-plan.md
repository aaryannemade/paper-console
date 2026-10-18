# Container + ESP32-S3 migration plan

Status: Steps 1–5 (container side) and the core Step 6 firmware are implemented.
WiFi printing, physical encoder/button input and corrected raster geometry have
been verified on the XIAO ESP32S3. Step 6 hardware acceptance is still partial;
Steps 7, 9 and 10 (OLED channel display, device health and the encoder reset
sequence) are implemented and passed hardware acceptance. Firmware is flashed
from the terminal (`just fw-flash`); the browser flasher (former Step 8) was
dropped. Step 11 closes out hardware/container validation.
Printer UART details are in [printer-uart-reference.md](printer-uart-reference.md).

## Target

- One container serves the HTTP API and built settings UI on a configurable port.
- The container owns configuration, module execution, scheduling, and receipt rendering.
- An ESP32-S3 sends physical input events to the container over HTTP. The container
  interprets button/rotary events and pushes print commands to the ESP32 over HTTP.
- The CSN-A2 printer is connected to the ESP32-S3 UART (TTL serial).
- The ESP32 hosts its own captive portal and web UI for WiFi setup, container
  connection configuration, and link status. These are firmware responsibilities.
- Network provisioning belongs to the container host and ESP32 firmware, not the backend.
- Local development uses the same application with mock/capture output.
- No Raspberry Pi detection, GPIO access, host WiFi changes, sudo, or host reboot/update operations.

## Confirmed HTTP relay architecture

```text
Button / rotary encoder -> ESP32-S3 --HTTP input event--> container
CSN-A2 printer <- TTL UART <- ESP32-S3 <--HTTP print job-- container
```

The container owns channel selection, button-action interpretation, interactive
modules, scheduling, content generation and CSN-A2 print-command generation.
Firmware owns input sampling/debouncing, provisioning, link reporting, bounded
transport buffering and UART pacing/flow control. It does not render receipts or
implement channel/module logic. It maps incremental encoder detents to a local
1–8 position and reports that position to the container; module names and menu
semantics remain container-owned. A local hardware reboot is a firmware action.

1. ESP32 joins WiFi using credentials configured in its captive portal/web UI.
2. ESP32 registers its device identity and HTTP callback address with the configured
   container URL and maintains a heartbeat. The UI shows connection state derived
   from actual communication, not just whether WiFi is associated.
3. ESP32 posts an input event with an event ID and sequence information. The
   container acknowledges promptly and interprets it using server-side state.
4. For a print action, the container renders and pushes a print job to the ESP32's
   HTTP endpoint. This is a separate outbound request, not a receipt download
   triggered by polling or a long-lived input-event response.
5. ESP32 relays the payload to the CSN-A2 UART and reports job status to the server.
   Acknowledging HTTP acceptance is distinct from completing UART delivery, and
   UART delivery alone is not proof that paper physically printed.

Scheduled/email/Slack prints use the same server-initiated push without an input
event. Both endpoints must be reachable on the LAN; Docker port mapping provides
ESP32-to-server access, and the container needs outbound access to the ESP32.

The container-side protocol specifies shared credentials, job IDs, duplicate
event/chunk handling, bounded chunks, printer width and serial settings,
busy/cancel/error responses, heartbeat expiry and restart reconciliation in
[relay-protocol.md](relay-protocol.md). Do not replay uncertain UART delivery.
Keep the ESP32 HTTP server responsive during UART transfer so input/heartbeat/status
traffic does not deadlock while the container is pushing a receipt.

## Implementation status and next milestones

| Area | Main locations | Status / next work |
| --- | --- | --- |
| Container packaging | `Dockerfile`, `docker-compose.yaml`, `portainer-compose.yaml`, `app/paths.py` | Multi-stage image, mounted data directory, non-root runtime and health check (Step 4 complete). |
| ESP32 core (Step 6) | `firmware/`, [relay-protocol.md](relay-protocol.md), [printer-uart-reference.md](printer-uart-reference.md) | Implemented; physical prints/input verified. Failure/recovery, cancellation and scheduling acceptance still pending. |
| Channel indication (Step 7) | `firmware/src/display.cpp`, `timekeeping.cpp`, container channel metadata | Channel names done end to end. SSD1306 OLED screens implemented (channel, setup AP, printing, disconnected, idle clock + WiFi signal); passed hardware acceptance 2026-10-18. |
| Device health (Step 9) | Firmware HTTP API/UI, container link UI | Implemented; passed hardware acceptance 2026-10-18. |
| Encoder reset sequence (Step 10) | `firmware/src/inputs.cpp`, `hold_cancel` gesture | Implemented (hold 10 s, clockwise, anticlockwise, click = erase settings); passed hardware acceptance 2026-10-18. |
| Final acceptance (Step 11) | Real ESP32/printer, container deployment, protocol checks | Validate failure/recovery, scheduling, menus and all new device features. |

## Step 1 — Remove WiFi provisioning end to end

Implement in two reviewable increments:

- **1a — Server startup boundary (implemented):** remove automatic WiFi maintenance/AP recovery
  and boot-time receipts; remove the WiFi-based authentication bypass. Server
  restarts must not print or provision the network. Document explicit development
  login credentials and test startup/shutdown against forbidden provisioning calls.
- **1b — Remove remaining management surfaces (implemented):** remove the WiFi API, captive
  routes, frontend onboarding/status dependencies, quick actions and reset hooks;
  extract the generic WiFi QR generator before removing the manager. User-authored
  WiFi QR codes are content, not host provisioning, and should remain supported.

- Remove startup powersave repair and WiFi startup/recovery tasks.
- Remove WiFi router registration, captive portal detection/redirects, and setup
  receipts that advertise an AP or `pc-1.local`.
- Remove WiFi setup screens, AP dialogs, status polling and their frontend imports.
- Remove host-specific reset hooks; restoring defaults is done by clearing the
  stopped container's dedicated `/data` bind mount.
- Delete the WiFi manager/helper once all application references are removed.
- Adapt tests and UI snapshot fixtures to the new application startup flow.

Acceptance: startup/shutdown without sudo prompts or network management commands;
UI opens directly to application login/settings; no AP retry tasks or misleading
WiFi receipts; channel printing and scheduling still work with mock output.

## Step 2 — Remove host administration (implemented)

- Remove endpoints/UI for SSH, OS credentials, host reboot, system clock writes,
  Pi UART setup and in-place OTA installation.
- Keep an explicit application credential, independent of host identity/passwords.
- Retain timezone as application configuration for receipt formatting and schedules;
  use the host-provided clock without modifying it.
- Remove the application reset API/UI/gesture; the operator clears the stopped
  container's `/data` bind mount to restore defaults. Describe app state in the
  system-monitor output and health/status responses.

Acceptance: application lifecycle and supported APIs do not call privileged host
tools; login/logout, settings persistence, timezone formatting and scheduling
have focused regression coverage.

## Step 3 — Replace local hardware with an HTTP-controlled print service (container side)

Container-only. The ESP32 firmware is Step 6; until then the container side is
exercised with mock outputs, a USB printer on the development machine, and a device
simulator that implements the firmware's half of the protocol.

Decisions:

- Print output is selected with `PC1_PRINTER_OUTPUT`: `esp32` (default; production),
  `console` (development log, the `just backend` default) or `usb` (development only:
  a CSN-A2 on a USB-serial adapter, port from `PC1_PRINTER_PORT` or auto-detected).
  All three share the same renderer, so layout and length behave identically.
- The container generates the CSN-A2 bytes (raster strips, feeds). The ESP32 only
  relays and paces them over UART, as the architecture section states.
- The ESP32 reports an absolute channel position 1–8 and button gestures. How the
  physical control (a rotary encoder with push switch) produces these is a firmware
  concern (Step 6).
- Offline or busy ESP32: the print is skipped and logged, with an error to the
  caller. No queue and no replay on reconnect.
- One device, authenticated in both directions with a shared bearer token
  (`PC1_RELAY_TOKEN`), separate from the settings password.

Increments:

- **3a — Split rendering from output (implemented).** Move the renderer out of the serial driver
  into a transport-free raster printer; move CSN-A2 byte encoding into a pure
  module; add `console`, `usb` and `esp32` outputs (`esp32` reports unavailable
  until 3c). Remove Raspberry Pi detection, the GPIO busy pin and `/dev/serial0`
  handling. Console mode keeps the `[PRINT]` log and can save each receipt as PNG
  (`PC1_PRINT_CAPTURE_DIR`). Keep the full-print snapshot workflow working.
- **3b — HTTP input events (implemented).** Add an authenticated input-event endpoint (position
  and button gestures, event IDs, duplicate rejection) that drives the existing
  channel print, quick actions, cancel, Adventure and app-picker
  selection. Keep the current dial position server-side. Then remove the GPIO
  button/dial drivers, ioctl helper and their callbacks.
- **3c — ESP32 relay transport (implemented).** Write `docs/relay-protocol.md`: registration and
  heartbeat, job push with job IDs and bounded chunks, job status callbacks, cancel,
  busy/offline/error responses, heartbeat expiry and restart reconciliation.
  Implement the `esp32` output and the device endpoints, show link status in the
  settings UI, and add `testing/relay_simulator.py`, which decodes received jobs back
  to PNG. Test scheduled and input-triggered prints end to end against the simulator.

Acceptance: HTTP triggers work without local hardware; concurrent requests and
retries have defined behavior; full receipt snapshots preserve layout; console and
USB outputs work in development; the simulator passes the protocol tests that the
firmware will have to pass in Step 6.

## Step 4 — Container packaging and persistence (implemented)

- Add a multi-stage Dockerfile: build the UI with locked npm dependencies, then
  serve its static build and API from the Python runtime.
- Run as a non-root user, bind `0.0.0.0`, expose a documented configurable port,
  and use a single application worker initially (scheduler and reservations are
  currently process-local).
- Consolidate writable configuration, credentials, first-run markers and any job
  state under a configurable mounted data directory.
- Add Compose configuration with port mapping, persistent volume, configuration
  examples and an HTTP health check. Container updates replace the image.
- Use direct Uvicorn startup without reload; the development `just backend`
  recipe uses reload and is not the container entrypoint.

Acceptance: build/run without privileged mode or host network management; UI/API
available through the mapped port; settings survive container recreation; graceful
shutdown; smoke test a mock print via HTTP from outside the container.

## Step 5 — Remove obsolete deployment material and finalize docs (implemented before Step 4)

- Remove Pi requirements, provisioning/imaging/deployment scripts, device OTA
  artifacts/workflows and hardware-only tests now that the server-side relay and
  simulator cover the old runtime path.
- Update README, agent instructions, dependency lists and snapshot helpers for
  container development/deployment and the ESP32 protocol.
- Add CI checks for backend tests and frontend lint/build; add container smoke
  tests once Step 4 provides an image.
- Verify no remaining runtime paths administer the host through Pi files, GPIO,
  `nmcli`, `sudo`, `systemctl`, `raspi-config` or `chpasswd`. Application settings
  and credentials remain writable pending the Step 4 mounted-data directory.

## Step 6 — ESP32-S3 firmware (core implemented; acceptance partial)

Implemented:

- Nix development shell includes PlatformIO and esptool; `just fw-build`,
  `fw-flash`, `fw-monitor` and `fw-erase` cover the device workflow. The monitor
  uses an absolute project path to avoid PlatformIO's relative-path decoder bug.
- PlatformIO/Arduino firmware for **Seeed XIAO ESP32S3**, with 8 MB flash/PSRAM
  detected on the test board. Printer TX is D6/GPIO43, RX is D7/GPIO44, at
  **9600 8N1**, with a shared ground and independently powered printer.
- Captive setup AP `PaperConsole-XXXXXX` at `192.168.4.1`; WiFi credentials,
  server URL and relay token persist in NVS. Network mode advertises
  `http://paper-console.local/`. BOOT held 5 s currently erases device settings.
- Scans run before opening the AP; refresh scans run in the background with
  short dwell times and merge a cache. Found networks are shown as tappable
  entries instead of relying on a captive browser's datalist support.
- The settings page is deliberately open to the LAN/setup network. The relay
  token is write-only in this page and authenticates server↔device traffic only;
  it is not an administrator password.
- Heartbeat, authenticated job creation/chunks/status/cancel, deduplication and
  bounded buffering implement the relay contract. Independent UART, network
  and input tasks keep HTTP and inputs responsive during a receipt.
- USB logging uses a bounded nonzero timeout and avoids holding status locks
  while writing, fixing hangs when the cable is connected but no monitor reads it.
- DFRobot SEN0235 EC11 module: VCC=3V3, A=D0/GPIO1, B=D1/GPIO2,
  C=D3/GPIO4. Physical tests confirmed active-low button and four quadrature
  transitions per detent. Clockwise increments; one detent selects one position,
  wrapping 1–8. The position starts at 1 on every boot (no longer saved in
  NVS) and is resynchronized whenever the server link comes up. This is incremental selection, not an absolute
  shaft angle; a fixed printed pointer legend is deferred in favour of Step 7.
- Debounced `press`, 5-second `hold`, and `hold_release` events carry position,
  unique event IDs and shared per-press IDs. A dedicated sender orders events,
  retries the same IDs and drops stale events instead of replaying late presses.
- The normal ESP32 output now uses **64-row RAM downloaded bitmaps** (`GS *`
  then `GS / 0`) instead of streaming `GS v 0`. Native USB keeps its existing
  format. Definition+print stay in one opaque relay record; no reflash was
  required. Smaller advertised chunks shorten bands in eight-row units.

Hardware findings and verified results:

- Captive setup, WiFi printing from the development backend, web-UI prints,
  knob position events and button-triggered channel printing work on hardware.
- Identical calibration pixels stretched vertically with both 9600-baud UART
  and USB paced at 960 bytes/s, isolating slow raster arrival as the trigger.
  Downloaded bitmaps corrected the distortion and darkness. 24/48/64-row bands
  worked; 128/256 failed. Use the validated **64 rows / 3072 image bytes** rather
  than the generic manual's unverified maximum.
- The user measured both native-USB and new ESP32 calibration squares at
  **24×24 mm with calipers**, then confirmed normal channel prints were fixed.
  Remaining stop/start seams on dithered/grey fills were accepted. Baudrate,
  heat and density settings were not changed.
- Simulator decoding handles both bitmap encodings. The post-change backend
  suite passed **346 tests**. Device `completed` still means UART delivery, not
  proof of paper output; printer readiness is currently assumed and no DTR
  input or physical paper/offline sensing is implemented.

Still pending: real-device protocol parity checks, cancellation/menu interaction,
scheduled prints, WiFi/server/device restart recovery and production-container
end-to-end acceptance. Track these in Step 11; simulator coverage does not count
as hardware acceptance. Quick Actions UX clarification is also deferred there.

## Step 7 — Show the selected channel (implemented and hardware-tested)

Implemented display (channel screen has no header bar; name or number plus position dots only): SSD1306 128×64 I²C OLED (3V3, SDA = D4/GPIO5, SCL =
D5/GPIO6, address 0x3C or 0x3D, `PC1_OLED_*` build flags incl. 180° rotation),
driven by U8g2 from a low-priority task that reads cached state only and pushes
a frame only when it changed. Screens, highest priority first: reset-sequence
step (and "Resetting" just before erasing) → channel name, or the large number
if unnamed, plus 1–8 position dots for 3 s after a knob turn → WiFi setup AP
(SSID and 192.168.4.1) → printing (with a hold-to-cancel hint) →
UART error / no WiFi / connecting / server offline with the link detail → idle
clock with WiFi signal bars top right. The clock uses NTP for UTC and the
heartbeat reply's `clock` (timezone offset, 12h/24h); before both arrive it
shows `--:--`. Names render with Latin/extended fonts; other characters show
as `?`; long names wrap to two lines, then three smaller lines, then `...`. The
idle layout shifts by up to 2 px each minute and the panel dims after 60 s
without input or a screen change. No display at boot: firmware runs as before
and `/health` reports `display: "absent"`.

Original planning notes:

- Decide how the user sees the selected channel on the endless rotary encoder.
  Evaluate a small **OLED screen** against simpler indicators; choose the part,
  supply voltage, interface and wiring before implementing a display driver.
  D4/GPIO5 and D5/GPIO6 remain available as candidate I²C SDA/SCL pins.
- Show position 1–8, and preferably the server-configured channel name. Define
  a minimal server→device metadata/status contract rather than duplicating the
  channel configuration or menu logic in firmware. **Done:** channels have an
  optional `name` (single line, ≤24 characters, empty = unnamed) edited inline
  in the settings UI, stored in `config.json`, set via
  `PUT /api/channels/{position}/name` and printed on the channel list. Each
  heartbeat reply carries `channels_hash` (hash of the eight names, not a
  growing version); when it differs from the device's copy the firmware
  fetches `GET /api/device/channels` (relay token) and keeps the names in RAM,
  refetching after every boot. The device status page shows the selected
  channel and name. Still to do: render them on the chosen display.
- Keep the position display responsive during printing; define disconnected
  and reconnect states and how selection menus differ from ordinary channels.
- Keep physical position and the displayed selection synchronized after boot,
  server restart and configuration changes. A future screen-based selector can
  build on this without relying on an absolute-angle knob or fixed legend.

Acceptance: each detent visibly selects the correct position, button actions
match that position, and offline/printing/menu states are understandable on the
chosen indicator. **Passed on real hardware 2026-10-18** with the SSD1306 OLED
(see [hardware-acceptance.md](hardware-acceptance.md)).

## Step 8 — Browser firmware flasher (dropped)

Not planned: firmware is built and flashed from the terminal with
`just fw-build` / `just fw-flash` inside `nix develop`. The step number is kept
so references to Steps 9–11 stay valid.

## Step 9 — Firmware health endpoint and at-a-glance status (implemented and hardware-tested)

Implemented: the settings top bar shows a green/amber/red dot from
`GET /api/printer/link` (`indicator.level` and a one-line `indicator.label`),
polled every 5 s after login. Green: ESP32 heartbeat current and relay ready
(or USB output available). Amber: printing, heartbeat later than two intervals,
or development `console` output. Red: no/expired heartbeat, unreachable relay,
non-printable reported printer state, unavailable USB output or the status
request failing. It is deliberately visual only; troubleshooting stays with the
container logs, ESP32 web UI and serial monitor. Green does not prove paper.
Implemented: public firmware `GET /health` schema v1, shared with `/ui/status`
and authenticated heartbeat telemetry; the ESP32 page and server General →
Printer Settings show health, uptime, connection, UART, active job and last
outcome. Stale/missing reports are explicit. Physical printer status remains
`unknown` because no validated paper/power signal exists. See
`relay-protocol.md` for the contract. Hardware verification for Steps 7, 9 and
10 (all passed 2026-10-18) is recorded in
[hardware-acceptance.md](hardware-acceptance.md).

- Implemented a lightweight firmware **`GET /health`** endpoint with a versioned JSON
  shape: firmware version/uptime, WiFi connectivity, server-link status, UART
  readiness, active job and last job outcome. Keep it responsive during printing.
- Implemented a prominent summary in the **device web UI** (and aligned the server's relay
  status display) so users can immediately see ready, printing, disconnected,
  error or unknown, without inspecting raw JSON or USB logs.
- Distinguish **relay/UART ready** from **printer physically responding**. With
  today's wiring and no validated status query, do not claim that paper is
  present or the printer is powered merely because WiFi/UART initialization works.
- Investigate printer status replies and/or an optional busy/paper signal,
  including electrical levels, bounded timeouts and actual device support. Show
  unavailable/unknown when physical status cannot be verified. Define how stale
  health/heartbeat information appears in both UIs.
- Document which health fields are public and keep secrets out of responses.
  Health visibility does not bypass bearer auth on print jobs or input events.

Acceptance: the device page visibly changes between idle/printing/link loss and
recovery; the endpoint remains responsive during a long receipt. Any claim of
physical printer availability is backed by a tested signal, otherwise labeled
unknown rather than inferred.

## Step 10 — Encoder reset sequence (implemented and hardware-tested)

Revised from a plain 10-second reboot: the encoder performs the same **settings
erase** as holding BOOT 5 s, behind a deliberate sequence so it cannot happen
by accident.

- Hold the encoder button 10 s → turn one detent clockwise → one anticlockwise
  → click (press under 2 s). Both triggers call `netmgr::factoryReset()`: erase
  NVS (WiFi, server URL, token), restart into the setup network.
- At 5 s the normal `hold` is still sent (tactile cue, or cancelling a running
  print). At 10 s the device sends the new **`hold_cancel`** gesture: the server
  releases the hold reservation immediately (prints are not blocked for its
  20 s timeout) and ignores any `hold_release` of that press. No Quick Actions.
- While the sequence is active, turns and presses stay local: no position
  change, no events. A wrong-direction turn, a press before both turns, a long
  final press, or 10 s without the next step (counted after release) cancels it.
- Feedback: XIAO user LED (GPIO21) lit while active, blinking per accepted step;
  `[input] reset sequence: ...` log lines; `reset_sequence` in `/ui/inputs`.
- Because the 5 s hold already requests cancellation, a running print has
  normally stopped at a band boundary long before the sequence completes.
- The channel position is no longer saved: every boot starts at 1 and sends it
  when the link comes up, so a reset or power loss also returns to channel 1.

Acceptance: short press, 5 s Quick Actions and hold-to-cancel still work;
holding 10 s alone and releasing prints nothing and opens no menu; the full
sequence erases settings and opens the setup network; each cancel path leaves
settings intact and the knob working normally. **Passed on real hardware
2026-10-18 — see [hardware-acceptance.md](hardware-acceptance.md) for the full
result.**

## Step 11 — Hardware acceptance and migration closeout (in progress)

Use [hardware-acceptance.md](hardware-acceptance.md) as the combined execution
checklist for the implemented health/status, channel-sync/boot-position,
encoder-reset and OLED changes. The combined firmware-and-software acceptance
for Steps 7 (channel names and OLED display), 9 (health/status) and 10
(encoder reset) passed on real hardware 2026-10-18; see the Results section in that file. Remaining
items in this step are now scoped to container/scheduler/recovery acceptance
and final documentation.

- Exercise the real firmware against the simulator's contract: auth failures,
  duplicate create/chunks/events, ordering/backpressure, status polling fallback,
  cancellation and terminal job states. Add a targeted real-device test mode
  where useful, distinguishing transport checks from tests that consume paper.
- Test cancellation during a long receipt and confirm the cancelled press cannot
  also open Quick Actions. Revisit the confusing Quick Actions interaction:
  menu choices, invalid positions, timeout and cancel behaviour, plus how Step 7
  presents the current menu. Document the final user interaction.
- Test WiFi loss/reconnect, backend restart, ESP32 restart mid-job and printer
  availability changes to the extent that Step 9 can actually detect them. No
  duplicate print or stale input may replay on recovery; offline jobs are skipped.
- Run the production container with its real data bind mount and ESP32 output.
  Verify scheduled, physical-button and web-UI prints, container recreation with
  persisted settings/password/saves, and relay recovery after recreation.
- Validate the channel indicator, health UI and encoder reset sequence on
  hardware, including first-time onboarding and updates. **Done:** passed on
  real hardware 2026-10-18, including the OLED display (see
  [hardware-acceptance.md](hardware-acceptance.md)).
- Reconcile README, protocol, UART reference and troubleshooting with the final
  behaviour; record accepted limits (9600-baud throughput, dithering seams,
  band-boundary cancellation latency and any unverified physical printer state).

Acceptance: record actual hardware results for the above checks, keep backend
tests and frontend lint/build passing locally (`just test`; the `checks.yml`
CI workflow was removed), and remove stale "firmware
planned" statements. The core migration is complete when these results are
recorded; future display-driven UX extensions can then be planned separately.

Step 1a verification: 313 backend tests pass, including startup/shutdown checks for
both first and subsequent boots, authentication independent of AP state, and task
cleanup. A real Uvicorn smoke test ran through the former delayed WiFi startup
window and shut down cleanly without boot prints, sudo prompts or AP retries.

Step 1b result: `app/wifi_manager.py`, the WiFi router, captive-portal probe routes,
setup/welcome/system-ready receipts, WiFi quick actions and the WiFi UI screens are
removed. The SPA catch-all route is replaced by `/` so retired probe paths return
404. WiFi QR generation moved to `app/qr_payloads.py` for the QR module.
System Monitor initially reported the outbound-interface IP instead of WiFi SSID; it now reports application, relay and `/data` state. Verification:
303 backend tests (obsolete WiFi/AP tests removed; route-absence, QR and IP
tests added), frontend lint/build, full print gallery (41 renders, 0 failures), and
a live server smoke test (WiFi/probe routes 404, auth enforced, channel print works,
no sudo/AP activity).

Step 2 result: removed SSH status/enable/disable, Device Password change/status
and OS-account password sync, host reboot, system clock set and NTP enable/disable,
host timezone writes (including on settings save/import), in-place OTA
check/install/convert-to-production, the `release_channel`/`time_sync_mode`
settings (old configs still load), the Pi UART boot migration
(`app/printer_uart.py`, `scripts/wifi_ap_nmcli.sh`, burn-in tool) and their UI.
`GET /api/system/version` is now read-only (`.version` or git commit) and
`GET /api/system/time` is a read-only display in the configured timezone.

The password is now an application credential: `PC1_DEVICE_PASSWORD`, else
`PC1_DEVICE_PASSWORD_FILE` (Docker secret), else a random password generated once,
persisted with mode 0600 and logged once. Too-short or unreadable configured
values fail startup instead of silently falling back. Login cookies are keyed by
`PC1_SESSION_SECRET` or the password, no longer by hostname/machine-id (the old
fallback password and cookie key were derivable from host identity).

Verification: 283 backend tests, including route-absence and a source scan for
privileged host commands in `app/`, password resolution/persistence/permissions,
cookie-forgery and non-ASCII password tests; frontend lint/build; live server smoke
test (see below).

The GPIO drivers and Pi detection were removed in Step 3. The remaining service,
imaging and release scripts and `timedatectl` probes were removed in Step 5.

Step 3a result: `app/drivers/printer_serial.py` became `printer_raster.py`
(`RasterPrinter`), a renderer with no I/O that hands one bitmap per receipt to a
print output. CSN-A2 encoding (GS v 0 strips with guard bytes, feeds, init) lives in
`app/drivers/escpos.py`. Outputs: `console` (the `[PRINT]` echo from the mock
printer plus a `[RECEIPT]` line, optional PNG capture), `usb` (pyserial, 128-byte
writes, per-strip flush, cancel between strips, reconnect after unplug) and `esp32`
(reports unavailable until 3c). `PC1_PRINTER_OUTPUT` defaults to `esp32`; `just
backend` and the tests use `console`. Removed: Raspberry Pi detection, GPIO busy pin,
`/dev/serial0` probing and the SD-card incident log. GPIO button/dial drivers are no
longer selected (mocks only) and are deleted in 3b.

Offline policy: `try_begin_print_job` skips and logs when the output is
unavailable, so scheduled, email, Slack and webhook prints are dropped without
reserving the printer; print endpoints return 503 with the output name. Printer
availability is reported in `/api/health` (`printer`, `printer_output`) but does
not degrade server health.

Verification: 299 backend tests (USB strip framing, chunking, cancellation,
partial-write and failure handling, reconnect; output selection; console echo,
byte count and PNG capture; offline 503/skip/health). The full print gallery
rendered by the old serial driver and by the new renderer with the same random
seed is pixel-identical for every deterministic receipt (37/41; the rest are
live system-monitor values and a wordsearch whose order depends on Python's hash
seed). Live: `just backend` console print through the Vite proxy, plus `esp32`
and `usb` modes with no device (503, health stays healthy) and an invalid mode
(startup fails).

The post-migration cleanup measures rendered styled text and other receipt
operations in dots for `max_print_lines`, stops after the module exceeding the
limit, and truncates the receipt with a notice within the limit. Interactive
receipt margins are now explicit per print job instead of depending on global
selection mode while unrelated messages are rendering.

Step 3b result: `POST /api/device/events` (`app/device_input.py`) accepts
`position` (absolute 1–8) and `button` gesture events (`press`, `hold`,
`hold_release`, grouped by `press_id`), authenticated with the shared
bearer token from `PC1_RELAY_TOKEN`/`PC1_RELAY_TOKEN_FILE` (`app/relay_auth.py`;
unset disables the endpoint with 503, invalid fails startup). Gesture timing is a
firmware job; the container keeps the meaning: print channel, menu choice,
cancel-by-hold (which consumes the rest of that press) and Quick Actions.
Retries with the same `event_id` return the original result
without acting again (last 256 ids). Busy/unavailable are reported as results;
nothing is queued. The channel position is server-side state (`ChannelSelector`).
Removed: GPIO button/dial drivers, the ioctl helper, the mock button/dial and
button-event draining (events that arrive during a print are rejected as busy
instead of queued). Spec for the firmware: [relay-protocol.md](relay-protocol.md).
Dev helper: `just device ...` (`testing/device_event.py`).

Verification: 319 backend tests (token handling, event validation, dedupe,
consumed presses, gesture outcomes against the real handlers, unavailable
printer, health). Live: channel print, debounce, Quick Actions menu then a menu
choice, duplicate event, 401/422/503 responses, startup failure on a short token,
and events through the Vite proxy.

Step 3c result: `docs/relay-protocol.md` specifies the full protocol. The ESP32
posts heartbeats (`/api/device/heartbeat`: identity, boot id, callback URL,
printer state, limits) and job status (`/api/device/jobs/{id}/status`); link state
(`app/relay_link.py`) comes from heartbeats and push results and is shown in
General Settings (`GET /api/printer/link`) and `/api/health`. The `esp32` output
(`app/drivers/esp32_relay_output.py`) pushes each receipt as records (preamble,
one per raster strip, cutter feed) in device-sized chunks. It uses idempotent job
creation and chunks, handles 429 back-off and cancellation between records, and
waits for the final state through status callbacks with polling as a fallback. A
device reboot or timeout fails the job without replay. Tactile feeds are small
jobs that are not followed to completion. `PC1_RELAY_URL` optionally pins the
device address. Delivery failures are logged by the renderer instead of crashing
the print job. `testing/relay_simulator.py` (`just relay-sim`) is the reference
device: it writes records at the simulated baud rate, decodes the raster back to
PNG and reports status.

Verification at Step 3: 350 backend tests, including 30 relay tests with real HTTP in
both directions: byte-exact round trip, small chunks with a full buffer,
cancel between records, lost callbacks covered by polling, reboot mid-job
without replay, unreachable and recovered, busy, token mismatch, feeds,
idempotent create/chunks, and scheduled and button-triggered channel prints
through the simulator. Live at 9600 baud: a button press printed Sudoku in 26.2 s
(decoded PNG identical in layout) with `busy` for a second press; a UI print was
cancelled by hold after 6 strips; an every-minute schedule printed through the
relay; the device going away gave one logged failure and then 503, and it
recovered on the next heartbeat.

Step 5 result: removed Pi requirements, setup, imaging and SSH deployment
scripts/docs/config, the in-place OTA release builder/workflow and its three
tests. Removed the obsolete service launcher `run.sh`; use `just backend` for
development and a direct Uvicorn command for Step 4 packaging. Removed host `timedatectl`
probes; timezone discovery now reads `TZ` or the container's `/etc/timezone`.
The README, icon validator, test instructions and CI now reflect the server/relay
paths. The old Pi pin assignments remain only in the UART reference as historical
context for Step 6. Verification: 347 backend tests (three obsolete release
builder tests, one first-boot variant, and the launcher test removed; guard tests added),
frontend lint/build and icon validator. Container
packaging and container smoke CI remain Step 4.

Step 4 result: a multi-stage Dockerfile builds the UI with locked npm
dependencies, installs Python requirements, and starts one foreground Uvicorn
worker as UID/GID 1000:1000. `PC1_DATA_DIR=/data` consolidates settings,
generated password and Adventure saves while bundled content stays read-only.
Both Compose examples use `/path/to/data:/data`; Portainer references a
prebuilt image. Port mapping, environment variables, health check and bind
mount are documented in the README. Both Compose files run as
`user: "${PUID:-1000}:${PGID:-1000}"` so the UID/GID can match the owner of the
host data directory (e.g. 1000:100 on NixOS; 0:0 under rootless Podman).
CI builds and smoke-tests the container.
Verification: 355 backend tests, multi-stage Docker image build, UI/API/console
Sudoku print as a non-root user with a read-only root filesystem, configurable
listener port and healthy Docker image check; settings and generated login
password both survived container recreation with the same `/data` bind mount.

Current implementation checkpoint: Step 6 core firmware and hardware raster fix
are complete. Next: Step 7 channel-indication decision, followed by Steps 8–10;
use Step 11 to collect the remaining acceptance results throughout that work.
