# Combined firmware hardware acceptance

Status: **pending on real XIAO ESP32S3 and printer**. Covers implemented
channel metadata/boot position, health/status and encoder reset changes together.
OLED and browser flashing have separate acceptance once implemented.

## Setup and evidence

- Record firmware/server commit, date, device ID and tester here with each result.
- Inside `nix develop`, run `just fw-flash`, `just fw-monitor`, and
  `just dev-esp32` in separate terminals as needed. Then repeat production checks
  with the container configured for `esp32` and its persistent data bind mount.
- Open the ESP32 page, `http://<device-ip>/health`, and server settings → General
  → Printer Settings. Keep serial and backend logs for failures. Keep 9600 baud
  and existing heat/density settings.
- Record PASS/FAIL, observed latency and logs per item; unchecked means untested.

## Boot and channel synchronization

- [ ] Boot/reboot starts at channel 1, synchronizes it to the server and resets
  health uptime/boot ID. Last device job is empty after reboot.
- [ ] Rename channels in server settings; the device page reflects new names
  after heartbeat/hash sync. Clear a name and test a 24-character Unicode name.
- [ ] Turn each detent across all eight positions; button prints match the
  displayed channel. Repeat during a long receipt and after backend restart.

## Health, printing and recovery

- [ ] `/health` is public JSON, schema version 1, no-store, and contains no token,
  WiFi password or configured server URL. Job endpoints still reject missing or
  incorrect bearer tokens.
- [ ] Online idle shows ready/UART ready on both pages. Physical printer remains
  explicitly unknown; it must not claim paper/power sensing.
- [ ] Print via server UI, physical button, schedule and device test-page button.
  Active job/state and terminal outcome appear on both pages (server telemetry
  may lag by a heartbeat). A completed UART job is not proof of physical output.
- [ ] Poll `/health` repeatedly during a long receipt; record response times and
  verify the page, rotary input and cancel remain responsive. Check while chunks
  are being uploaded as well as while UART is draining.
- [ ] Hold to cancel a long receipt. Confirm cancellation at a record boundary,
  `cancelled` outcome, and no accidental Quick Actions afterward.
- [ ] Stop backend: device shows server disconnect. Restart: reconnects without
  duplicate output or replayed input. Check rejected-token diagnostics too.
- [ ] Disconnect WiFi: the device reports disconnect when accessible through its
  setup AP; server shows stale telemetry after 20 s and offline after 30 s.
  Restore WiFi and confirm both pages recover. Interrupt page polling itself:
  stale/unreachable is shown instead of indefinitely retaining a green status.
- [ ] Restart ESP32 mid-job: backend marks interrupted delivery failed, nothing
  automatically reprints, channel resets to 1, and health restarts cleanly.
- [ ] Turn printer power off/remove paper: physical status remains unknown.
  Do not infer power/paper from successful UART writes. Any future status query
  or busy/paper wiring needs separate electrical-level and timeout validation.
- [ ] Recreate production container: settings/password/Adventure saves persist,
  relay recovers, and scheduled/button/web prints work again.

## Encoder reset (run last: successful reset erases configuration)

- [ ] Short click, normal five-second Quick Actions hold and hold-to-cancel work.
- [ ] Hold ten seconds then release only: no print/menu; server hold reservation
  released promptly via `hold_cancel`; knob still works after sequence timeout.
- [ ] Wrong direction, early click, final long press, and ten-second step timeout
  each cancel without erasing settings or forwarding sequence turns as channels.
- [ ] Hold ten seconds → clockwise one detent → anticlockwise one → short click:
  LED/log feedback matches steps, WiFi/server/token erased, setup AP appears.
- [ ] Complete captive setup again; health reconnects and printing works.
- [ ] BOOT held five seconds performs the same reset as the encoder sequence.

## Results

No hardware results recorded yet. Automated backend/simulator tests and firmware
builds are development checks only. Attach results here and update Step 11 in
`container-esp32-plan.md` after this session.
