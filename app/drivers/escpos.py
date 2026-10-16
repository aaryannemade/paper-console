"""CSN-A2 / QR204 command encoding.

Pure functions that turn rendered receipts into printer bytes. No I/O: the same
bytes are written to a USB serial port in development and relayed by the ESP32 in
production. See docs/printer-uart-reference.md for the reasoning behind the framing.
"""

from typing import Iterator, Optional

from PIL import Image

# Each raster strip is its own GS v 0 command. NUL is ignored by the printer in
# command mode, so the guard bytes absorb a few lost payload bytes without
# shifting the next command.
RASTER_STRIP_ROWS = 24
RASTER_GUARD_BYTES = 4
DOTS_PER_LINE = 24
MAX_FEED_UNITS = 255

NUL = b"\x00"
CAN = b"\x18"  # Cancel print data in page mode
ESC_INIT = b"\x1b\x40"  # ESC @ — hardware reset
CANCEL_CHINESE_MODE = b"\x1c\x2e"  # FS .
USA_CHARSET = b"\x1b\x52\x00"  # ESC R 0
CODEPAGE_PC437 = b"\x1b\x74\x00"  # ESC t 0
STATUS_REQUEST = b"\x10\x04\x01"  # DLE EOT 1 — real-time printer status

# Bitmaps are rotated in software, so no ESC { is needed.
ASCII_SETTINGS = CANCEL_CHINESE_MODE + USA_CHARSET + CODEPAGE_PC437
# Re-asserted at the start of every receipt.
JOB_PREAMBLE = CANCEL_CHINESE_MODE


def pad_to_byte_width(img: Image.Image) -> Image.Image:
    """Return a 1-bit image whose width is a multiple of 8 (padding is white)."""
    img = img.convert("1")
    width, height = img.size
    if width % 8 == 0:
        return img
    padded = Image.new("1", (width + (8 - width % 8), height), 1)
    padded.paste(img, (0, 0))
    return padded


def encode_raster_strip(strip: Image.Image) -> bytes:
    """Encode one GS v 0 strip plus guard bytes. Black pixels print."""
    strip = strip.convert("1")
    width, height = strip.size
    if width % 8 != 0:
        raise ValueError("Raster strip width must be byte-aligned")

    bytes_per_row = width // 8
    header = b"\x1d\x76\x30\x00" + bytes(
        [
            bytes_per_row & 0xFF,
            (bytes_per_row >> 8) & 0xFF,
            height & 0xFF,
            (height >> 8) & 0xFF,
        ]
    )
    # PIL "1" mode stores white as 1; the printer prints set bits.
    payload = bytes(byte ^ 0xFF for byte in strip.tobytes())
    return header + payload + NUL * RASTER_GUARD_BYTES


def iter_raster_strips(img: Image.Image) -> Iterator[bytes]:
    """Yield one encoded command per 24-row strip of a receipt bitmap."""
    img = pad_to_byte_width(img)
    width, height = img.size
    for top in range(0, height, RASTER_STRIP_ROWS):
        bottom = min(top + RASTER_STRIP_ROWS, height)
        yield encode_raster_strip(img.crop((0, top, width, bottom)))


def feed_dots(dots: int) -> bytes:
    """ESC J n, split into commands of at most 255 dots."""
    remaining = max(0, int(dots))
    out = bytearray()
    while remaining > 0:
        chunk = min(remaining, MAX_FEED_UNITS)
        out += b"\x1b\x4a" + bytes([chunk])
        remaining -= chunk
    return bytes(out)


def feed_lines(lines: int) -> bytes:
    """Post-print feed: ESC d n followed by the ESC J equivalent.

    Both are sent because printer firmwares differ in which one they honor after
    raster data; the original driver did the same.
    """
    lines = max(0, int(lines))
    if lines == 0:
        return b""
    return b"\x1b\x64" + bytes([min(lines, MAX_FEED_UNITS)]) + feed_dots(
        lines * DOTS_PER_LINE
    )


def is_offline_status(status_byte: int) -> bool:
    """DLE EOT 1 response: bit 3 set means offline/busy."""
    return (status_byte & 0x08) != 0


# --- Relay job framing ---------------------------------------------------------
#
# A relay print job is a list of records. Each record is a self-contained piece
# of printer data that the ESP32 writes to the UART in one go; cancellation is
# only honored between records (never inside a GS v 0 strip). On the wire, a
# chunk body is a sequence of [2-byte big-endian length][record bytes].

RECORD_HEADER_BYTES = 2
MAX_RECORD_BYTES = 0xFFFF


def receipt_records(img: Image.Image, feed_lines_after: int) -> list:
    """Records for one receipt: preamble, one per raster strip, cutter feed."""
    records = [JOB_PREAMBLE]
    records.extend(iter_raster_strips(img))
    feed = feed_lines(feed_lines_after)
    if feed:
        records.append(feed)
    return records


def frame_records(records) -> bytes:
    out = bytearray()
    for record in records:
        if not record:
            continue
        if len(record) > MAX_RECORD_BYTES:
            raise ValueError("Record too large to frame")
        out += len(record).to_bytes(RECORD_HEADER_BYTES, "big") + record
    return bytes(out)


def split_records_into_chunks(records, max_chunk_bytes: int) -> list:
    """Group framed records into chunk bodies of at most max_chunk_bytes."""
    chunks, current = [], bytearray()
    for record in records:
        framed = frame_records([record])
        if not framed:
            continue
        if len(framed) > max_chunk_bytes:
            raise ValueError(
                f"Record of {len(record)} bytes exceeds the device chunk limit {max_chunk_bytes}"
            )
        if current and len(current) + len(framed) > max_chunk_bytes:
            chunks.append(bytes(current))
            current = bytearray()
        current += framed
    if current:
        chunks.append(bytes(current))
    return chunks


def parse_framed_records(body: bytes) -> list:
    """Inverse of frame_records; raises ValueError on a malformed body."""
    records, offset = [], 0
    while offset < len(body):
        if offset + RECORD_HEADER_BYTES > len(body):
            raise ValueError("Truncated record header")
        length = int.from_bytes(body[offset : offset + RECORD_HEADER_BYTES], "big")
        offset += RECORD_HEADER_BYTES
        if length == 0 or offset + length > len(body):
            raise ValueError("Invalid record length")
        records.append(body[offset : offset + length])
        offset += length
    return records


def decode_raster(data: bytes) -> Optional[Image.Image]:
    """Rebuild the bitmap from a printer byte stream (GS v 0 strips).

    Used by the relay simulator and tests to check what a printer would print.
    Non-raster commands are skipped. Returns None when there is no raster data.
    """
    strips, i = [], 0
    while i < len(data):
        if data[i : i + 4] == b"\x1d\x76\x30\x00" and i + 8 <= len(data):
            width_bytes = data[i + 4] | (data[i + 5] << 8)
            height = data[i + 6] | (data[i + 7] << 8)
            size = width_bytes * height
            payload = data[i + 8 : i + 8 + size]
            if len(payload) < size:
                break
            # Printer set bits are black; PIL "1" stores white as 1.
            strip = Image.frombytes(
                "1", (width_bytes * 8, height), bytes(b ^ 0xFF for b in payload)
            )
            strips.append(strip)
            i += 8 + size
        else:
            i += 1
    if not strips:
        return None
    width = max(s.width for s in strips)
    image = Image.new("1", (width, sum(s.height for s in strips)), 1)
    top = 0
    for strip in strips:
        image.paste(strip, (0, top))
        top += strip.height
    return image
