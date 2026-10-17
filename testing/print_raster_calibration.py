#!/usr/bin/env python3
"""Print identical calibration pixels through the normal USB or ESP32 output.

Run inside nix develop. First render, then print explicitly:
  python testing/print_raster_calibration.py
  python testing/print_raster_calibration.py --output esp32
  python testing/print_raster_calibration.py --output usb
  python testing/print_raster_calibration.py --output usb --usb-bytes-per-second 960
  python testing/print_raster_calibration.py --output usb --usb-bytes-per-second 960 --encoding downloaded

ESP32 requires PC1_RELAY_TOKEN and PC1_DEVICE_PASSWORD for the live backend
(--server). Uses a private DeviceLink and polls completion; no server settings
are changed. Avoid other prints during this comparison. The USB run reuses the
saved PNG, so the pixels, orientation, framing and cutter feed match exactly.
"""

import argparse
import hashlib
import os
from pathlib import Path
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.drivers import escpos  # noqa: E402
from app.drivers.print_outputs import create_output  # noqa: E402
from app.drivers.esp32_relay_output import Esp32RelayOutput  # noqa: E402
from app.relay_link import DeviceLink, Heartbeat  # noqa: E402


def render():
    image = Image.new("1", (384, 432), 1)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(
        str(ROOT / "web/public/fonts/IBM_Plex_Mono/IBMPlexMono-Medium.ttf"), 14
    )
    draw.text((8, 4), "RASTER CALIBRATION v1", font=font, fill=0)
    draw.text((8, 25), "Square: 192 x 192 dots (24 mm)", font=font, fill=0)
    # Inclusive bounds give exactly 192 pixels on both axes.
    draw.rectangle((96, 52, 287, 243), outline=0, width=2)
    draw.ellipse((120, 76, 263, 219), outline=0, width=2)
    draw.line((184, 148, 200, 148), fill=0, width=1)
    draw.line((192, 140, 192, 156), fill=0, width=1)
    draw.text((8, 253), "Solid     50% dots   50% rows", font=font, fill=0)
    draw.rectangle((8, 278, 111, 317), fill=0)
    for y in range(278, 318):
        for x in range(136, 240):
            if (x + y) % 2 == 0:
                draw.point((x, y), fill=0)
        if y % 2 == 0:
            draw.line((264, y, 367, y), fill=0)
    draw.text((8, 329), "Full-width solid: 8 dot rows", font=font, fill=0)
    draw.rectangle((8, 352, 375, 359), fill=0)
    draw.text((8, 374), "8 dots/mm; no heat overrides", font=font, fill=0)
    draw.text((8, 395), "Compare square height AND width", font=font, fill=0)
    return image


def relay_output(server):
    password = os.environ.get("PC1_DEVICE_PASSWORD", "")
    if not password or not os.environ.get("PC1_RELAY_TOKEN"):
        raise RuntimeError("Set PC1_DEVICE_PASSWORD and PC1_RELAY_TOKEN first")
    response = requests.get(
        server.rstrip("/") + "/api/printer/link",
        headers={"X-PC1-Device-Password": password}, timeout=5,
    )
    response.raise_for_status()
    state = response.json()
    if not state["available"] or state["printer_state"] != "ready":
        raise RuntimeError("Relay must be online and idle before the comparison")
    link = DeviceLink()
    link.record_heartbeat(Heartbeat(
        device_id=state["device_id"], boot_id=state["boot_id"],
        callback_url=state["pinned_url"] or state["callback_url"],
        printer={"state": "ready", "width_dots": 384, "baudrate": 9600},
        limits={"max_chunk_bytes": 8192, "max_job_bytes": 1048576},
    ))
    return Esp32RelayOutput(link=link)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", choices=("esp32", "usb"), help="Omit to render only")
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--encoding", choices=("raster", "downloaded"),
                        help="Defaults to downloaded for ESP32, raster for USB/render-only")
    parser.add_argument("--downloaded-band-rows", type=int, default=64,
                        help="Downloaded band height; 64 validated, 128/256 failed on this printer")
    parser.add_argument("--usb-bytes-per-second", type=int, default=None,
                        help="Pace USB writes in at most 16-byte bursts (UART timing approximation)")
    parser.add_argument("--image", type=Path, default=ROOT / "testing/artifacts/debug/raster-calibration-v1.png")
    args = parser.parse_args()
    if args.encoding is None:
        args.encoding = "downloaded" if args.output == "esp32" else "raster"
    if args.encoding == "downloaded" and args.output == "esp32" and args.downloaded_band_rows > 64:
        parser.error("ESP32 calibration is limited to the USB-validated band heights (up to 64 rows)")
    if args.usb_bytes_per_second is not None and (
        args.output != "usb" or args.usb_bytes_per_second <= 0
    ):
        parser.error("--usb-bytes-per-second requires --output usb and a positive rate")
    if not args.image.exists():
        args.image.parent.mkdir(parents=True, exist_ok=True)
        render().save(args.image)
    with Image.open(args.image) as source:
        image = source.convert("1")
    if image.size != (384, 432):
        raise RuntimeError("Expected the saved 384x432 calibration bitmap")
    # Same rotation used by RasterPrinter for the printer mounting.
    bitmap = image.rotate(180)
    if args.encoding == "downloaded":
        records = escpos.downloaded_receipt_records(
            bitmap, feed_lines_after=5, band_rows=args.downloaded_band_rows
        )
        print(f"Downloaded band height: {args.downloaded_band_rows} rows", flush=True)
    else:
        records = escpos.receipt_records(bitmap, feed_lines_after=5)
    digest = hashlib.sha256(b"".join(records)).hexdigest()
    print(f"Image: {args.image}\nEncoding: {args.encoding}\nPrinter bytes SHA256: {digest}", flush=True)
    if not args.output:
        return
    output = relay_output(args.server) if args.output == "esp32" else create_output("usb")
    paced_hash = hashlib.sha256()
    if args.usb_bytes_per_second is not None:
        # Initialization has already happened. Pace just the receipt and feed,
        # leaving the production drivers and all command bytes unchanged.
        write = output._write

        def paced_write(data):
            total = 0
            for offset in range(0, len(data), 16):
                chunk = data[offset:offset + 16]
                started_chunk = time.monotonic()
                written = write(chunk)
                paced_hash.update(chunk[:written])
                total += written
                time.sleep(max(0, len(chunk) / args.usb_bytes_per_second -
                               (time.monotonic() - started_chunk)))
            return total

        output._write = paced_write
        print(f"USB pacing: {args.usb_bytes_per_second} bytes/s, bursts <=16 bytes", flush=True)
    started = time.monotonic()
    try:
        if args.output == "esp32" and args.encoding == "downloaded" and args.downloaded_band_rows == 64:
            stats = output.print_receipt(bitmap, feed_lines_after=5, cancel_event=threading.Event())
        elif args.output == "esp32":
            # Use the existing authenticated job/chunk transport and polling,
            # changing only the explicitly selected diagnostic command records.
            with output._job_lock:
                job_id, total = output._push(records, "receipt")
                stats = output._await(job_id, total, threading.Event())
        elif args.encoding == "downloaded":
            if not output.is_available():
                raise RuntimeError(f"USB unavailable: {output.describe()}")
            sent = 0
            for record in records:
                sent += output._write(record)
                output._flush()
            output.wait_for_idle()
            stats = {"bytes_sent": sent, "records": len(records)}
        else:
            stats = output.print_receipt(bitmap, feed_lines_after=5, cancel_event=threading.Event())
        print(f"{args.output}: {stats}; elapsed {time.monotonic() - started:.1f}s")
        if args.usb_bytes_per_second is not None:
            if paced_hash.hexdigest() != digest:
                raise RuntimeError("Actual paced bytes did not match the calibration receipt")
            print("Verified: actual paced bytes match the selected encoding's SHA256")
    finally:
        output.close()


if __name__ == "__main__":
    main()
