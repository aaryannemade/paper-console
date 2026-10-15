# Container + ESP32-S3 migration plan

Status: architecture confirmed; Steps 1 and 2 implemented. Steps 3–5 remain
pending. ESP32 transport and firmware are not implemented yet. Printer UART
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

Protocol implementation will specify paired credentials, job IDs, duplicate input
and print rejection, bounded payload/chunk sizes, printer width and serial settings,
busy/cancel/error responses, heartbeat expiry, offline queue policy and restart
reconciliation. Do not blindly replay a job whose UART delivery is uncertain.
Keep the ESP32 HTTP server responsive during UART transfer so input/heartbeat/status
traffic does not deadlock while the container is pushing a receipt.

## Current coupling

| Area | Main locations | Required change |
| --- | --- | --- |
| Startup and WiFi | `app/main.py`, `app/wifi_manager.py`, `app/routers/wifi.py` | Remove AP startup/recovery, powersave repair, WiFi APIs and captive portal routes. |
| Host administration | `app/main.py`, `app/device_password.py`, `app/factory_reset.py` | Remove SSH management, OS password synchronization, clock changes, reboot and in-place device updates. Keep application authentication and settings reset. |
| Physical controls | `app/hardware.py`, `app/drivers/*gpio*.py`, `app/selection_mode.py`, `app/main.py` | Remove hardware autodetection and local button/dial lifecycle; represent needed interactions through HTTP. |
| Printer transport/rendering | `app/drivers/printer_serial.py`, `app/printer_uart.py`, `testing/render_all_prints.py` | Separate reusable layout/raster rendering from serial/GPIO and UART boot migration. The serial driver contains rendering code, so deleting it wholesale would lose functionality. |
| UI | `web/src/App.jsx`, `web/src/WiFiSetup.jsx`, `web/src/components/APInstructionsModal.jsx`, `web/src/components/GeneralSettings.jsx` | Remove WiFi onboarding and host administration controls; retain content/channel settings and print controls. |
| Deployment | `scripts/setup_pi.sh`, `scripts/wifi_ap_nmcli.sh`, `scripts/prepare_golden_image.sh`, `scripts/deploy_automated.py`, `run.sh`, `requirements-pi.txt`, `raspberry_pi/` | Replace Pi installation and service management with container packaging. Review device OTA release scripts/workflows and documentation. |

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

## Step 3 — Replace local hardware with an HTTP-controlled print service

- Keep the existing authenticated `POST /action/print-channel/{position}` as the
  initial trigger API. It already accepts a channel directly, reserves the printer,
  and returns while work runs in the background.
- Extract print orchestration/reservation from GPIO-oriented `app/hardware.py`.
- Remove Pi detection, GPIO drivers and button callbacks. (The UART boot migration
  was already removed in Step 2.)
- Replace shared dial state with explicit request inputs. Define HTTP interaction
  events for Adventure and other selection modes before removing their controls.
- Extract rendering from `printer_serial.py`; preserve mock/capture outputs and
  the full-receipt snapshot workflow.
- Implement the agreed transport/job protocol, including how status, cancellation,
  concurrency and authentication work for the ESP32.
- Add ESP32-S3 firmware with captive portal, persistent WiFi/container configuration,
  local link-status UI, physical input events, HTTP print receiver and CSN-A2 UART
  relay. Keep application decisions in the container and test scheduled pushes as
  well as input-triggered pushes. Confirm board pin assignments when wiring firmware.
  Follow [printer-uart-reference.md](printer-uart-reference.md) for serial settings,
  raster strip framing, DTR handling and throughput/buffer sizing.

Acceptance: HTTP triggers work without local hardware; concurrent requests and
retries have defined behavior; full receipt snapshots preserve layout; physical
completion/failure is tested against the chosen transport.

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
- Use direct Uvicorn startup without reload or `run.sh` process-killing behavior.

Acceptance: build/run without privileged mode or host network management; UI/API
available through the mapped port; settings survive container recreation; graceful
shutdown; smoke test a mock print via HTTP from outside the container.

## Step 5 — Remove obsolete deployment material and finalize docs

- Remove Pi requirements, provisioning/imaging/deployment scripts, device OTA
  artifacts/workflows and hardware-only tests after their replacements pass.
- Update README, agent instructions, dependency lists and snapshot helpers for
  container development/deployment and the ESP32 protocol.
- Add CI checks for backend tests, frontend build and container smoke tests.
- Verify no remaining runtime paths rely on Pi files, GPIO, `nmcli`, `sudo`,
  `systemctl`, `raspi-config`, `chpasswd` or host filesystem mutation.

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

Left for later steps: GPIO drivers and Pi detection in `app/hardware.py`
(Step 3); `run.sh` service handling, `scripts/setup_pi.sh`, release/OTA build
scripts and workflow (Step 5). `GET /api/system/timezone` and the location default
still try read-only `timedatectl show`, which falls back cleanly in a container.

Next implementation: Step 3 (HTTP relay protocol and ESP32-S3 firmware).
