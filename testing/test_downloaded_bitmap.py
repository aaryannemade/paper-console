"""Wire-format checks before testing GS * / GS / on the physical printer."""

import pytest
from PIL import Image

from app.drivers import escpos


def test_downloaded_bitmap_uses_vertical_msb_first_columns():
    image = Image.new("1", (16, 16), 1)
    for point in ((0, 0), (0, 7), (0, 8), (1, 1), (8, 3), (15, 15)):
        image.putpixel(point, 0)
    command = escpos.encode_downloaded_bitmap(image)
    expected = bytearray(32)
    expected[0:2] = b"\x81\x80"
    expected[2] = 0x40
    expected[16] = 0x10
    expected[31] = 0x01
    assert command == b"\x1d\x2a\x02\x02" + expected + b"\x1d\x2f\x00"


@pytest.mark.parametrize("size", [(7, 8), (8, 7), (2048, 8), (8, 392), (384, 264)])
def test_downloaded_bitmap_rejects_invalid_dimensions(size):
    with pytest.raises(ValueError):
        escpos.encode_downloaded_bitmap(Image.new("1", size, 1))


def test_downloaded_bands_keep_padding_white_and_feed_once():
    image = Image.new("1", (384, 25), 0)
    records = escpos.downloaded_receipt_records(image, feed_lines_after=5, band_rows=24)
    assert len(records) == 4
    assert records[0] == escpos.JOB_PREAMBLE
    assert records[1][:4] == b"\x1d\x2a\x30\x03"
    assert records[1][4:-3] == b"\xff" * (384 * 3)
    assert records[2][:4] == b"\x1d\x2a\x30\x01"
    assert records[2][4:-3] == b"\x80" * 384
    assert records[3] == escpos.feed_lines(5)
    assert escpos.parse_framed_records(escpos.frame_records(records)) == records


def test_max_width_downloaded_bands_use_12k_ram_limit():
    records = escpos.downloaded_receipt_records(
        Image.new("1", (384, 432), 0), 5, band_rows=256
    )
    assert len(records) == 4
    assert records[1][:4] == b"\x1d\x2a\x30\x20"
    assert records[1][4:-3] == b"\xff" * 12288
    assert records[2][:4] == b"\x1d\x2a\x30\x16"
    assert records[2][4:-3] == b"\xff" * 8448


@pytest.mark.parametrize("rows", [0, -8, 25, 264])
def test_invalid_downloaded_band_height(rows):
    with pytest.raises(ValueError):
        escpos.downloaded_receipt_records(Image.new("1", (384, 432), 1), 5, band_rows=rows)


def test_downloaded_default_round_trip_preserves_pixels_and_white_padding():
    image = Image.new("1", (384, 131), 1)
    for y in range(image.height):
        for x in range(image.width):
            if (x * 13 + y * 7) % 19 < 8:
                image.putpixel((x, y), 0)
    records = escpos.downloaded_receipt_records(image, 5)
    assert records[1][:4] == b"\x1d\x2a\x30\x08"  # 64 rows, not 128 or 256.
    decoded = escpos.decode_raster(b"".join(records))
    assert decoded.size == (384, 136)
    assert decoded.crop((0, 0, 384, 131)).tobytes() == image.tobytes()
    assert decoded.crop((0, 131, 384, 136)).tobytes() == b"\xff" * (48 * 5)


def test_downloaded_definition_does_not_print_until_trigger_and_reset_clears_it():
    command = escpos.encode_downloaded_bitmap(Image.new("1", (16, 16), 0))
    assert escpos.decode_raster(command[:-3]) is None
    assert escpos.decode_raster(command[:-3] + escpos.ESC_INIT + command[-3:]) is None
