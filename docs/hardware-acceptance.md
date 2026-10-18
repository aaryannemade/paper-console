# Combined firmware hardware acceptance

Status: **passed on real XIAO ESP32S3 and printer** (reported by the project
owner on 2026-10-18). Covers implemented channel metadata/boot position,
health/status, encoder reset, OLED display and menu display changes. Firmware is flashed
from the terminal (`just fw-flash`); there is no browser flasher.

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

## OLED display (Step 7)

SSD1306 128×64 on SDA = D4/GPIO5, SCL = D5/GPIO6, 3V3.

- [x] Boot log shows `[display] SSD1306 at 0x3C` (or 0x3D); `/health` and both
      status pages report the display as connected. Unplugged: `absent`, and
      the relay otherwise behaves as before.
- [x] Each knob detent shows the new channel within ~0.1 s for 3 s: name when
      set (short, two-line, 24-character and accented names; unsupported
      characters as `?`), large number when unnamed, position dots correct.
      Renaming in settings updates the screen after the next heartbeat.
- [x] Turning during a long receipt stays responsive and returns to the
      printing screen; the printing screen shows for server, button and
      test-page prints and clears when the job ends or is cancelled.
- [x] Fresh/erased device and the 30 s fallback AP both show WiFi setup with
      the correct `PaperConsole-XXXXXX` name and 192.168.4.1.
- [x] WiFi off: "No WiFi". Backend stopped: "Server offline" with the link
      detail. Wrong token: the 401 detail. Recovery returns to the clock.
- [x] Idle clock matches the server timezone; switching timezone and 12h/24h
      in settings updates within one heartbeat. Before NTP/server sync it shows
      `--:--`. WiFi bars track signal (e.g. move the device away).
- [x] Clock position shifts slightly each minute; display dims after 60 s
      idle and brightens on any knob/button input or screen change.
- [x] Reset sequence shows each step and "Resetting" before restarting;
      cancelling returns to the normal screen. BOOT 5 s also shows
      "Resetting".

## Menus on the OLED (Step 11)

- [x] Hold 5 s and release: after the Quick Actions receipt, the OLED shows
      "Quick actions" with the option under the knob (Table of contents, System
      monitor, Cancel) and "Nothing here" on 3–7; pressing an empty position
      does nothing; the receipt has no `Dial:` line.
- [x] Choosing an option or Cancel closes the menu; the OLED returns to the
      clock and turning shows channel names again.
- [x] Leaving Quick Actions open for 2 minutes closes it without printing.
- [x] A channel with two interactive modules shows "Select app" with their
      names; Adventure shows its choice text, and "Save & exit" on 8.
- [x] Restart the relay while a menu is open: after reconnecting the OLED
      shows the menu again (heartbeat), and `[menu]` lines appear in the log.

## Results

2026-10-18: the project owner ran every item above on the real XIAO ESP32S3 and
printer and reported that all of them passed in real-world use. The OLED
section was tested afterwards the same day, after correcting swapped SDA/SCL
wires and removing the channel screen header bar; it also passed. The menus on
the OLED section was added with the final Quick Actions/menu changes and passed
in the project owner's test the same day. No failures or
follow-up issues were reported. Per-item latency figures and logs were not
recorded.
