# ESP32-S3 to CSN-A2 / QR204 printer UART reference

The container encodes each receipt in `app/drivers/escpos.py` and sends complete
ESC/POS records over the HTTP protocol in [relay-protocol.md](relay-protocol.md).
The ESP32 firmware writes each record to the printer UART unchanged. The native
USB development path in `app/drivers/usb_serial_output.py` carries the same bytes
but does not use a UART baud rate.

## Electrical interface

| Item | ESP32-S3 to printer |
| --- | --- |
| UART | Dedicated hardware UART (UART1/2), **9600 8N1**, no RTS/CTS. Do not share boot/log console pins. |
| TX / RX | ESP32 TX to printer RX; printer TX to ESP32 RX. Check printer TX voltage first: ESP32 GPIO is **not 5 V tolerant**. |
| Busy (DTR) | Optional GPIO input; **1 = busy**. Do not block indefinitely when asserted. |
| Ground and power | Common ground; independent 5 V printer supply rated for ≥4–5 A peak. |
| Paper | 58 mm, 384 dots wide at 203 dpi (48 bytes per raster row). |

## Printer initialisation on firmware boot

Send these once after opening the UART, before accepting a receipt:

```text
00 00 00 00 00        flush partial printer command; wait 100 ms
1B 40                 ESC @ — initialise; wait 300 ms
1C 2E                 FS . — cancel Chinese (double-byte) mode
1B 52 00              ESC R 0 — USA character set
1B 74 00              ESC t 0 — code page PC437
```

Do not send reset commands when the container starts: there may be no relay
heartbeat yet. Each receipt starts with the `FS .` ASCII preamble. The firmware
initialises its own UART on reboot, regardless of the container lifecycle.

## Raster strips and flow control

The container rotates the 384-wide, 1-bit receipt **180°** for the printer's
mounting direction, frames it into independent 24-row ESC/POS strips and sends
the bytes to the relay. Each strip is a complete record:

```text
1D 76 30 00  xL xH  yL yH  <raster bytes>  00 00 00 00
GS v  0     bytes/row    rows   1=black dot    four NUL guards
```

- Width = 48 bytes per row. Rows are MSB first; the encoder XORs PIL's
  white-as-1 representation. The four NUL bytes absorb a short UART payload
  after a damaged strip.
- Write in ~128-byte chunks and wait for the UART to drain at each strip
  boundary. If DTR reads 1, wait 10 ms, then poll every 5 ms until it clears.
  After 15 s of DTR asserted, record an incident and stop consulting DTR for
  the rest of that receipt. If DTR is not wired, skip it.
- Cancel **between complete records**, never inside a raster command. A partial
  command leaves the printer interpreting pixel bytes as instructions.
- At 9600 baud (~960 bytes/s), a 24-row strip is 1164 bytes (~1.2 s); a
  500-row receipt is roughly 26 s. Heartbeats and inputs must keep working
  throughout a print. If the UART reports a short/failed write, fail the job
  with its byte count and never report a completed receipt.
- On success, the container sends `ESC J 120` (five lines / 120 dots) once for
  tear clearance. Tactile blips use `ESC J 2`. Cancelled/failed receipts skip
  the post-print feed.

For end-to-end cancellation, status and idempotent chunk delivery see the
[relay protocol](relay-protocol.md). Use `testing/console_raster_preview.py`
for deterministic raster QA.
