# CSN-A2 / QR204 printer UART reference (for ESP32-S3 firmware)

This captures how the upstream Raspberry Pi build drove the thermal printer, so
the ESP32-S3 relay can reproduce it in Step 3 of the
[migration plan](container-esp32-plan.md). The Pi-specific code was removed in
Step 2 (`app/printer_uart.py`, `scripts/wifi_ap_nmcli.sh`,
`testing/printer_uart_burnin.py`). Use `git show 57a39b3:<path>` to read the
originals.

The byte-level transport described below still lives in
`app/drivers/printer_serial.py` (`_send_bitmap`, `_encode_raster_strip`,
`_initialize_printer`, `feed_direct`) and is covered by
`testing/test_printer_transport.py`. Keep this document in sync with that code
until the transport moves to the ESP32.

## Electrical and serial settings

| Item | Upstream Pi value | ESP32-S3 note |
| --- | --- | --- |
| Baud / framing | 9600, 8N1, no parity. The driver comment says QR204 only supports 9600. | Configure a hardware UART (UART1 or UART2) at 9600 8N1. |
| Hardware flow control | None. RTS/CTS were not used. | Keep `UART_HW_FLOWCTRL_DISABLE`. |
| Host TX → printer RX | Pi GPIO14 (pin 8) | Any free GPIO routed to the UART TX. |
| Printer TX → host RX | Pi GPIO15 (pin 10) | ESP32-S3 GPIOs are **not 5 V tolerant**. Measure the printer TX level before connecting, or use a divider or level shifter. The Pi is also 3.3 V-only, but measure anyway. |
| Printer DTR ("busy") | Pi GPIO18 (pin 12), input | GPIO input. Polarity as used upstream: **1 = busy**. |
| Signal ground | Pi pin 14 | Common ground with the printer supply is required. |
| Printer power | Separate 5 V supply, ≥4–5 A peak | Never power the printer from the ESP32 board's 5 V pin or regulator. |
| Paper | 58 mm, 384 dots wide, 203 dpi | 48 bytes per raster row. |

### Why the Pi needed a UART migration, and what it means here

The Pi Zero 2 W initially used the **mini UART** (`ttyS0`). Its baud clock comes
from the VPU core clock, so CPU load and frequency scaling caused timing drift
and dropped bytes. A single lost byte in a `GS v 0` raster payload desynchronizes
every command after it, which produced garbage receipts (upstream issue #23). The fix had two parts:

1. **Move `/dev/serial0` to the PL011 UART (`ttyAMA0`).** In `config.txt`, add
   `enable_uart=1` and `dtoverlay=disable-bt` (Bluetooth gives up the PL011). Remove
   `console=serial0|ttyAMA*|ttyS*` from `cmdline.txt`. Disable `hciuart` and
   mask `serial-getty@{serial0,ttyS0,ttyAMA0}`. The helper backed up the boot
   files, then did one guarded reboot, tracked by boot-id, to apply the change.
2. **Harden the transport framing** (below) so that if a byte is lost anyway,
   only one strip is damaged.

On the ESP32-S3 the UART peripherals have their own clock source, so the
mini-UART problem does not apply directly. The lessons carry over:

- Use a dedicated hardware UART. Do not share it with the boot/log console
  (UART0 / USB-Serial-JTAG). Bootloader or `ESP_LOG` output on the printer TX
  line is a likely source of garbage receipts.
- Keep the TX path fed from a buffer. Do not interleave WiFi or HTTP work in a way
  that can starve the UART mid-command. Use the UART driver's TX ring buffer and
  feed it from a dedicated task.
- Keep the strip framing and guard bytes even though the link should be clean.

## Byte protocol used by the driver

The container renders the whole receipt as one 1-bit bitmap, 384 px wide, already
**rotated 180°** because the printer is mounted inverted. No printer-side
rotation (`ESC {`) is used.

### Initialization (on port open)

```text
00 00 00 00 00        flush any partial command in the printer   ; wait 100 ms
1B 40                 ESC @  reset                                ; wait 300 ms
1C 2E                 FS .   cancel Chinese (double-byte) mode
1B 52 00              ESC R 0  USA character set
1B 74 00              ESC t 0  code page PC437
```

`clear_hardware_buffer()` sends `18` (CAN), waits 50 ms, sends `ESC @`,
waits 300 ms, then re-applies the three ASCII settings above.

### Raster strips (one receipt)

- Split the bitmap into strips of **24 rows**. The last strip may be shorter.
  Pad the width to a multiple of 8 with white pixels.
- Each strip is a complete, independent command:

  ```text
  1D 76 30 00  xL xH  yL yH  <payload>  00 00 00 00
  GS v  0  m=0 bytes/row   rows        data         4 NUL guard bytes
  ```

  - `xL xH` is bytes per row, little-endian (48 for 384 px). `yL yH` is the
    strip's row count.
  - The payload is row-major, MSB first. **1 = black dot.** PIL mode `1` stores
    white as 1, so the driver XORs every byte with `0xFF`.
  - The printer ignores NUL in command mode. If a few payload bytes are lost,
    the guards absorb the shortfall, so the next strip still starts on a
    command boundary.
- Strips are sent back-to-back with **no feeds or idle waits between them**. They
  are protocol boundaries, not separate jobs.
- Write in **128-byte chunks**. Before each chunk, consult DTR (see below).
  After each strip, wait for the host UART to drain (`tcdrain` /
  `uart_wait_tx_done`).
- **Cancellation is only honored between strips**, never mid-strip. Stopping
  inside a payload would leave the printer parsing image data as commands.

### DTR / busy handling (advisory)

- Before each 128-byte chunk: if DTR reads 1, wait 10 ms (debounce). If it is
  still 1, poll every 5 ms until it reads 0.
- If DTR stays asserted for **15 s**, record an incident and **stop consulting
  DTR for the rest of that receipt**. Upstream found DTR useful when it asserts,
  but not reliable enough to block on indefinitely.
- If DTR is not wired, skip this entirely. The driver works without it.

### After the receipt

- Post-print feed for cutter clearance (`feed_direct`): wait 50 ms, then send
  `1B 64 n` (ESC d, n lines). As a backup, also send `1B 4A k` (ESC J, k dots)
  in chunks of up to 255, where k = n × 24.
- Tactile "blip" when a print starts: `1B 4A 02` (ESC J 2, feed 2 dots).
- Optional status query: `10 04 01` (DLE EOT 1). Bit 3 of the 1-byte reply is
  1 when the printer is offline or busy. No reply means "assume ready".

### Error handling expectations

- A write that makes no progress, or that fails, aborts the receipt and reports
  how many bytes were written. Do not report success for a partial transfer.
- After a failed or cancelled receipt, skip the post-print feed.
- Upstream kept a bounded incident log (busy-pin timeouts, failed strips). The
  ESP32 should report the equivalent in its job status back to the container.

## Throughput and buffer sizing

At 9600 baud (~960 bytes/s), one full-width 24-row strip is
8 + 48 × 24 + 4 = **1164 bytes, about 1.2 s**. A typical 500-row receipt is
21 strips, about 24.5 KB, about 26 s of UART time. The ESP32 HTTP server must stay
responsive for inputs, heartbeats and status during that whole time.

The raw bitmap is 48 bytes per row (~48 KB for 1000 rows), which fits in
ESP32-S3 SRAM, or in PSRAM if the module has it. Step 3 decides whether the
container sends a raw bitmap (the ESP32 frames strips) or pre-framed command
bytes (the ESP32 only paces and handles DTR). The framing rules above apply
either way.

## Burn-in testing idea

Upstream's `testing/printer_uart_burnin.py` printed labeled stripe, checker and
diagonal patterns under CPU load, and checked each receipt visually for
corruption. A similar ESP32 test should print the same patterns while generating
WiFi/HTTP traffic. Torn or shifted rows show up immediately in those patterns.
