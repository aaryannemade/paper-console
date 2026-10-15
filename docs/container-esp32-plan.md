# Container + ESP32-S3 migration plan

Status: proposed; implementation should proceed in small, independently verified steps.

## Target

- One container serves the HTTP API and built settings UI on a configurable port.
- The container owns configuration, module execution, scheduling, and receipt rendering.
- An ESP32-S3 sends HTTP requests in response to physical controls.
- Network provisioning belongs to the container host and ESP32 firmware, not the backend.
- Local development uses the same application with mock/capture output.
- No Raspberry Pi detection, GPIO access, host WiFi changes, sudo, or host reboot/update operations.

## Decision needed before physical print transport work

Where is the printer connected? An HTTP trigger alone does not carry the generated
receipt back to the printer. Confirm whether the ESP32-S3 owns the printer UART,
or the container has access to a printer port.

If the ESP32 owns the UART, the proposed flow is:

1. ESP32 submits a channel print request; server returns a job ID.
2. Server renders a receipt into a bounded, downloadable print payload.
3. ESP32 retrieves it and streams it to the printer with local flow control.
4. ESP32 acknowledges completion or failure; server updates job status.

Scheduled/email/Slack prints need the same delivery path even without a button
request, so the ESP32 must also discover pending jobs (polling is a simple first
implementation). Define payload format, chunking, printer width, retry/deduplication,
timeouts, and offline behavior before implementing this protocol. A server-side
HTTP success must not be presented as confirmation of physical printing.

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

## Step 2 — Remove host administration

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
- Remove Pi detection, UART boot migration, GPIO drivers and button callbacks.
- Replace shared dial state with explicit request inputs. Define HTTP interaction
  events for Adventure and other selection modes before removing their controls.
- Extract rendering from `printer_serial.py`; preserve mock/capture outputs and
  the full-receipt snapshot workflow.
- Implement the agreed transport/job protocol, including how status, cancellation,
  concurrency and authentication work for the ESP32.

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

Recommended next implementation: Step 1. It addresses the observed development
startup behavior and does not depend on the printer transport decision.
