"""Render limits and menu styling apply to the current receipt only."""

import app.selection_mode as selection_mode
from PIL import Image
from app.drivers.print_outputs import ConsoleOutput
from app.drivers.printer_raster import RasterPrinter


class CaptureOutput(ConsoleOutput):
    def __init__(self):
        self.images = []
        super().__init__()

    def print_receipt(self, img, feed_lines_after, cancel_event):
        self.images.append(img.copy())
        return {"cancelled": False}


def test_styled_text_and_images_respect_print_limit(monkeypatch):
    output = CaptureOutput()
    printer = RasterPrinter(output=output)
    rendered_ops = []
    original_render = printer._render_unified_bitmap

    def render(ops):
        rendered_ops.append(list(ops))
        return original_render(ops)

    monkeypatch.setattr(printer, "_render_unified_bitmap", render)
    printer.reset_buffer(max_lines=4)
    for _ in range(20):
        printer.print_body("A long line of styled text that should be truncated")
    assert printer.is_max_lines_exceeded()
    printer.flush_buffer()

    assert printer._max_lines_hit
    assert len(output.images) == 1
    assert rendered_ops[0][-1] == ("styled", {"text": "-- MAX LENGTH REACHED --", "style": "regular_sm"})
    assert len(rendered_ops[0]) < 20
    assert output.images[0].height <= 4 * printer.LINE_HEIGHT + printer.SPACING_LARGE * 2

    # A single oversized image is omitted intact; the notice remains printable.
    printer.reset_buffer(max_lines=4)
    printer.print_image(Image.new("1", (384, 400), 0))
    assert printer.is_max_lines_exceeded()
    printer.flush_buffer()
    assert rendered_ops[1] == [rendered_ops[0][-1]]
    assert output.images[1].height <= 4 * printer.LINE_HEIGHT + printer.SPACING_LARGE * 2


def test_selection_indicator_is_per_receipt_not_global_input_state():
    output = CaptureOutput()
    printer = RasterPrinter(output=output)
    selection_mode.enter_selection_mode(lambda _: None, "test-menu")

    printer.print_body("Regular incoming message")
    assert printer._get_left_margin() == 2
    printer.flush_buffer()

    printer.reset_buffer()
    printer.set_selection_indicator()
    printer.print_body("Menu choices")
    assert printer._get_left_margin() == 10
    printer.flush_buffer()
    assert printer._get_left_margin() == 2
    assert len(output.images) == 2
