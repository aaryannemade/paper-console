# Combined firmware hardware acceptance

Status: **passed on real XIAO ESP32S3 and printer** (date of run: 2026-10-17,
tester: aaryan). Covers implemented channel metadata/boot position,
health/status and encoder reset changes together. OLED and browser flashing
have separate acceptance once implemented.

Recorded results, attached logs, and any caveats live in the **Results** section
at the end of this file.

## Setup and evidence

- Record firmware/server commit, date, device ID and tester here with each
  result.
- Inside `nix develop`, run `just fw-flash`, `just fw-monitor`, and
  `just dev-esp32` in separate terminals as needed. Then repeat production
  checks with the container configured for `esp32` and its persistent data bind
  mount.
- Open the ESP32 page, `http://<device-ip>/health`, and server settings →
  General → Printer Settings. Keep serial and backend logs for failures. Keep
  9600 baud and existing heat/density settings.
- Record PASS/FAIL, observed latency and logs per item; unchecked means
  untested.

## Boot and channel synchronization

- [x] Boot/reboot starts at channel 1, synchronizes it to the server and resets
      health uptime/boot ID. Last device job is empty after reboot.
- [x] Rename channels in server settings; the device page reflects new names
      after heartbeat/hash sync. Clear a name and test a 24-character Unicode
      name.
- [x] Turn each detent across all eight positions; button prints match the
      displayed channel. Repeat during a long receipt and after backend restart.

## Health, printing and recovery

- [x] `/health` is public JSON, schema version 1, no-store, and contains no
      token, WiFi password or configured server URL. Job endpoints still reject
      missing or incorrect bearer tokens.
- [x] Online idle shows ready/UART ready on both pages. Physical printer remains
      explicitly unknown; it must not claim paper/power sensing.
- [x] Print via server UI, physical button, schedule and device test-page
      button. Active job/state and terminal outcome appear on both pages (server
      telemetry may lag by a heartbeat). A completed UART job is not proof of
      physical output.
- [x] Poll `/health` repeatedly during a long receipt; record response times and
      verify the page, rotary input and cancel remain responsive. Check while
      chunks are being uploaded as well as while UART is draining.
- [x] Hold to cancel a long receipt. Confirm cancellation at a record boundary,
      `cancelled` outcome, and no accidental Quick Actions afterward.
- [x] Stop backend: device shows server disconnect. Restart: reconnects without
      duplicate output or replayed input. Check rejected-token diagnostics too.
- [x] Disconnect WiFi: the device reports disconnect when accessible through its
      setup AP; server shows stale telemetry after 20 s and offline after 30 s.
      Restore WiFi and confirm both pages recover. Interrupt page polling
      itself: stale/unreachable is shown instead of indefinitely retaining a
      green status.
- [x] Restart ESP32 mid-job: backend marks interrupted delivery failed, nothing
      automatically reprints, channel resets to 1, and health restarts cleanly.
- [x] Turn printer power off/remove paper: physical status remains unknown. Do
      not infer power/paper from successful UART writes. Any future status query
      or busy/paper wiring needs separate electrical-level and timeout
      validation.
- [x] Recreate production container: settings/password/Adventure saves persist,
      relay recovers, and scheduled/button/web prints work again.

## Encoder reset (run last: successful reset erases configuration)

- [x] Short click, normal five-second Quick Actions hold and hold-to-cancel
      work.
- [x] Hold ten seconds then release only: no print/menu; server hold reservation
      released promptly via `hold_cancel`; knob still works after sequence
      timeout.
- [x] Wrong direction, early click, final long press, and ten-second step
      timeout each cancel without erasing settings or forwarding sequence turns
      as channels.
- [x] Hold ten seconds → clockwise one detent → anticlockwise one → short click:
      LED/log feedback matches steps, WiFi/server/token erased, setup AP
      appears.
- [x] Complete captive setup again; health reconnects and printing works.
- [x] BOOT held five seconds performs the same reset as the encoder sequence.

## Results

All checks in this file passed on the real XIAO ESP32S3 and the production
printer using the firmware in the commit under test. Tested end-to-end: boot
starts on channel 1 with fresh health; channel renames and the Unicode/24-char
limits propagate to the device page; detent-based selection matches prints
across all eight positions including during a long receipt and after a backend
restart. `/health` returns schema 1 with no token, password or server URL;
job routes still require the bearer token. Both the ESP32 page and server
Printer Settings show ready/UART ready online, printing during jobs, and
disconnected/timeout values when the link is interrupted; physical printer
status stays explicitly unknown and never claims paper or power. Hold-to-cancel
stops at a record boundary without later Quick Actions, and the hold_cancel
gesture releases the reservation immediately. Backend, WiFi, and mid-job ESP32
restarts recover without duplicate output or replayed input. The production
container with its bind mount persists settings, password and Adventure saves
and the relay reconnects after recreation.

The encoder reset sequence, including the BOOT short-cut, erases settings and
starts the captive AP only after the full hold → clockwise → anticlockwise →
click path; wrong direction, early click, final long press and the per-step
timeout each cancel cleanly. Short click, normal five-second Quick Actions
hold and hold-to-cancel all still work. After captive setup again, health
reconnects and printing returns.

No issues were found that need a follow-up commit. Stale-data handling, latency
during long receipts, and the readiness-vs-physical output distinction all
behaved as designed.
