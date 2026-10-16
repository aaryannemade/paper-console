# Container + ESP32-S3 migration plan

Status: architecture confirmed; Steps 1–3 (container side) and 5 (legacy removal)
implemented. Next: Step 4 (container packaging). ESP32 firmware is Step 6. Printer UART
details for the firmware are in [printer-uart-reference.md](printer-uart-reference.md).

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
implement channel/module logic.

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

## Remaining implementation

| Area | Main locations | Required change |
| --- | --- | --- |
| Container packaging | `app/config.py`, `app/device_password.py`, `web/` | Add a multi-stage image, mounted data directory, non-root runtime, Compose example and health check. |
| ESP32 firmware | [relay-protocol.md](relay-protocol.md), [printer-uart-reference.md](printer-uart-reference.md) | Implement the reference protocol and hardware UART on the chosen ESP32-S3 board. |

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
- Remove WiFi-specific quick actions and factory-reset hooks; retain application reset.
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
- Make settings reset affect only application data; revise system-monitor output
  and health/status responses to describe the server rather than a Pi appliance.

Acceptance: application lifecycle and supported APIs do not call privileged host
tools; login/logout, settings persistence/reset, timezone formatting and scheduling
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
  channel print, quick actions, cancel, factory reset, Adventure and app-picker
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

## Step 4 — Container packaging and persistence

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

## Step 6 — ESP32-S3 firmware

- Implement `docs/relay-protocol.md` on the ESP32-S3: captive portal, persistent
  WiFi/container URL/token configuration, local link-status UI, heartbeat, input
  events, HTTP print receiver and CSN-A2 UART relay.
- Map the rotary encoder with push switch to the protocol's position and button
  gestures. Confirm board pin assignments when wiring.
- Follow [printer-uart-reference.md](printer-uart-reference.md) for serial
  settings, raster strip framing, DTR handling and throughput/buffer sizing.
- Keep the HTTP server responsive during UART transfer.

Acceptance: the firmware passes the same protocol tests as the Step 3c simulator;
physical completion/failure and scheduled and input-triggered prints are tested on
hardware.

Step 1a verification: 313 backend tests pass, including startup/shutdown checks for
both first and subsequent boots, authentication independent of AP state, and task
cleanup. A real Uvicorn smoke test ran through the former delayed WiFi startup
window and shut down cleanly without boot prints, sudo prompts or AP retries.

Step 1b result: `app/wifi_manager.py`, the WiFi router, captive-portal probe routes,
setup/welcome/system-ready receipts, WiFi quick actions and the WiFi UI screens are
removed. The SPA catch-all route is replaced by `/` so retired probe paths return
404. Resets restore application settings only (no WiFi forget, password change or
reboot). WiFi QR generation moved to `app/qr_payloads.py` for the QR module.
System Monitor reports the outbound-interface IP instead of WiFi SSID. Verification:
303 backend tests (obsolete WiFi/AP tests removed; route-absence, reset, QR and IP
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

Known issue (pre-existing, unchanged): `max_print_lines` only counts legacy
`text` ops, and content uses `styled` ops, so the length limit rarely applies.
Console mode now matches the real printer here; the old mock counted every call.

Step 3b result: `POST /api/device/events` (`app/device_input.py`) accepts
`position` (absolute 1–8) and `button` gesture events (`press`, `hold`,
`hold_release`, `long_hold`, grouped by `press_id`), authenticated with the shared
bearer token from `PC1_RELAY_TOKEN`/`PC1_RELAY_TOKEN_FILE` (`app/relay_auth.py`;
unset disables the endpoint with 503, invalid fails startup). Gesture timing is a
firmware job; the container keeps the meaning: print channel, menu choice,
cancel-by-hold (which consumes the rest of that press), Quick Actions and
application reset. Retries with the same `event_id` return the original result
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

Next implementation: Step 4 (container packaging and persistence).
