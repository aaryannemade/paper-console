# ESP32-S3 to CSN-A2 / QR204 printer UART reference

The container encodes each receipt in `app/drivers/escpos.py` and sends complete
ESC/POS records over the HTTP protocol in [relay-protocol.md](relay-protocol.md).
The ESP32 firmware writes each record to the printer UART unchanged. The native
USB development path in `app/drivers/usb_serial_output.py` renders the same
bitmap, but uses streaming `GS v 0` commands and no UART baud rate for native USB.

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

## Downloaded bitmap bands and flow control

The container rotates the 384-wide, 1-bit receipt **180°** for the printer's
mounting direction and sends independent **64-row downloaded bitmap bands** to
the relay. Each band is one complete record:

```text
1D 2A 30 08  <3072 image bytes>  1D 2F 00
GS * x y                       GS / 0 (print at normal size)
```

- `x` and `y` are dimensions in eight-dot units (48 and 8 for 384x64).
  Bytes are column-major, top to bottom, MSB first; a set bit prints black.
  No NUL guards are inserted in downloaded bitmap records.
- The printer receives a complete band into RAM before the explicit print
  command. This avoids the vertical stretching seen with slow `GS v 0` delivery.
- The last band is padded with at most seven white rows. Smaller relay chunk
  limits reduce the band height in multiples of eight rows; no fallback to
  `GS v 0` is used. Image data is capped at the validated 3072 bytes per band.
- Write in ~128-byte chunks and wait for the UART to drain at each band
  boundary. If DTR reads 1, wait 10 ms, then poll every 5 ms until it clears.
  After 15 s of DTR asserted, record an incident and stop consulting DTR for
  the rest of that receipt. If DTR is not wired, skip it.
- Cancel **between complete records**, never inside a raster command. A partial
  command leaves the printer interpreting pixel bytes as instructions.
- At 9600 baud (~960 bytes/s), a 64-row band is 3079 printer bytes (~3.2 s);
  cancellation can take that long to reach a safe boundary. A 500-row receipt
  needs roughly 26 s of transmission. Heartbeats and inputs must keep working
  throughout a print. If the UART reports a short/failed write, fail the job
  with its byte count and never report a completed receipt.
- On success, the container sends `ESC J 120` (five lines / 120 dots) once for
  tear clearance. Tactile blips use `ESC J 2`. Cancelled/failed receipts skip
  the post-print feed.

For end-to-end cancellation, status and idempotent chunk delivery see the
[relay protocol](relay-protocol.md). Use `testing/console_raster_preview.py`
for deterministic raster QA.

## Hardware calibration results

An identical 384x432 calibration bitmap printed normally over native USB stayed
square, whereas UART at 9600 baud and USB paced at 960 bytes/s both produced
vertical elongation and a lighter checkerboard. This isolates slow delivery as
a trigger; the printer's internal cause is not yet established. Do not change
heat settings or compensate image geometry on that evidence alone.

`testing/print_raster_calibration.py` reuses the saved calibration PNG across
outputs. Its `--encoding downloaded` mode uses `GS *` to define
each bitmap in printer RAM, then `GS / 0` to print at normal density.
The CSN-A2 manual specifies column-major vertical MSB-first bytes, dimensions
in eight-dot units, `y <= 48`, and `x*y <= 1536`. Definition and print are one
record; no flash writes, baud changes or heat overrides are used.

- 24-, 48- and 64-row bands worked over slow USB.
- 128- and 256-row bands printed gibberish. The generic manual's maximum is
  not a validated device capacity; the exact reason for failure is unknown.
- 64-row bands also worked over the real ESP32 UART. The user measured both
  native-USB and ESP32 calibration squares at **24x24 mm with calipers**.
- Pauses can still leave seams in dithered/grey areas; the user accepted this
  tradeoff. Solid black did not show visible seams in their tests.

64 rows is now the normal ESP32 format. No firmware update is required: it
already forwards opaque records and its 8192-byte chunk limit fits these bands.

```bash
python testing/print_raster_calibration.py --output usb --usb-bytes-per-second 960 --encoding downloaded --downloaded-band-rows 64
python testing/print_raster_calibration.py --output esp32 --encoding downloaded --downloaded-band-rows 64
```
