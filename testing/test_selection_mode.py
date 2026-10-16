"""Menus that take over the dial: selection mode, Quick Actions and app pickers."""

import asyncio
import types
import app.main as main_module
from app.config import ChannelConfig, ChannelModuleAssignment, ModuleInstance


def test_quick_actions_only_offer_toc_monitor_and_cancel(monkeypatch, capsys):
    import app.selection_mode as selection_mode
    from app.device_input import ChannelSelector
    from app.drivers.print_outputs import ConsoleOutput
    from app.drivers.printer_mock import PrinterDriver
    from app.drivers.printer_raster import RasterPrinter

    monkeypatch.setattr(main_module, "printer", RasterPrinter(output=ConsoleOutput(), echo=PrinterDriver()))
    monkeypatch.setattr(main_module, "dial", ChannelSelector(position=5))

    asyncio.run(main_module.long_press_menu_trigger())

    menu = capsys.readouterr().out
    assert "Table of contents" in menu and "System monitor" in menu
    assert "Reset" not in menu
    assert {position for position in range(1, 9) if selection_mode.can_handle_selection(position)} == {1, 2, 8}


def test_selection_mode_ignores_overlapping_callbacks():
    import threading
    import app.selection_mode as selection_mode

    selection_mode.exit_selection_mode()
    started = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def slow_callback(dial_position):
        calls.append(dial_position)
        started.set()
        release.wait(timeout=1)

    selection_mode.enter_selection_mode(slow_callback, "busy-test")

    worker = threading.Thread(
        target=lambda: results.append(selection_mode.handle_selection(1))
    )
    worker.start()
    assert started.wait(timeout=1)

    results.append(selection_mode.handle_selection(2))
    release.set()
    worker.join(timeout=1)

    assert calls == [1]
    assert results.count(True) == 1
    assert results.count(False) == 1

    selection_mode.exit_selection_mode()


def test_failing_selection_callback_exits_the_menu():
    import app.selection_mode as selection_mode

    def fail(position):
        raise RuntimeError(f"cannot handle {position}")

    selection_mode.enter_selection_mode(fail, "broken", valid_positions={2})
    assert selection_mode.handle_selection(2) is True
    assert not selection_mode.is_selection_mode_active()
    assert not selection_mode.can_handle_selection(2)


def test_invalid_selection_position_does_not_blip(monkeypatch):
    import app.selection_mode as selection_mode

    calls = []

    class FakePrinter:
        def blip(self):
            calls.append("blip")

    selection_mode.exit_selection_mode()
    selection_mode.enter_selection_mode(
        lambda dial_position: calls.append(f"choice_{dial_position}"),
        "valid-position-test",
        valid_positions={1},
    )

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(
        main_module,
        "clear_print_reservation",
        lambda clear_hold=False: calls.append(f"clear_{clear_hold}"),
    )

    asyncio.run(main_module.handle_selection_async(2))

    assert calls == ["clear_False"]
    assert selection_mode.is_selection_mode_active() is True

    selection_mode.exit_selection_mode()


def test_interactive_module_picker_ignores_empty_slots(monkeypatch):
    import app.selection_mode as selection_mode

    events = []

    class FakePrinter:
        def blip(self):
            events.append("blip")

        def reset_buffer(self, max_lines=0):  # noqa: ARG002
            events.append("reset_buffer")

        def print_header(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_header")

        def print_line(self):
            events.append("print_line")

        def print_body(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_body")

        def print_caption(self, *args, **kwargs):  # noqa: ARG002
            events.append("print_caption")

        def feed(self, lines=1):  # noqa: ARG002
            events.append("feed")

        def flush_buffer(self):
            events.append("flush_buffer")

    modules = {
        "adventure-one": ModuleInstance(
            id="adventure-one",
            type="adventure",
            name="Adventure One",
            config={},
        ),
        "adventure-two": ModuleInstance(
            id="adventure-two",
            type="adventure",
            name="Adventure Two",
            config={},
        ),
    }
    channels = {
        1: ChannelConfig(
            modules=[
                ChannelModuleAssignment(module_id="adventure-one", order=0),
                ChannelModuleAssignment(module_id="adventure-two", order=1),
            ]
        )
    }

    monkeypatch.setattr(main_module, "printer", FakePrinter())
    monkeypatch.setattr(
        main_module,
        "settings",
        types.SimpleNamespace(max_print_lines=200, channels=channels, modules=modules),
    )
    monkeypatch.setattr(
        main_module,
        "get_module",
        lambda module_type: types.SimpleNamespace(interactive=True),
        raising=False,
    )
    monkeypatch.setattr(main_module, "clear_print_reservation", lambda clear_hold=False: None)

    selection_mode.exit_selection_mode()

    asyncio.run(main_module.trigger_channel(1))

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "channel-menu-1"
    assert selection_mode.can_handle_selection(1) is True
    assert selection_mode.can_handle_selection(2) is True
    assert selection_mode.can_handle_selection(3) is False
    assert selection_mode.can_handle_selection(8) is True

    events.clear()
    asyncio.run(main_module.handle_selection_async(3))

    assert events == []
    assert selection_mode.is_selection_mode_active() is True

    selection_mode.exit_selection_mode()


def test_adventure_ignores_invalid_choice_without_printing(monkeypatch, tmp_path):
    import app.hardware as hardware
    import app.selection_mode as selection_mode
    from app.modules import adventure

    selection_mode.exit_selection_mode()

    class FakePrinter:
        def __init__(self):
            self.bodies = []

        def reset_buffer(self, max_lines=0):  # noqa: ARG002
            return None

        def print_header(self, *args, **kwargs):  # noqa: ARG002
            return None

        def print_subheader(self, *args, **kwargs):  # noqa: ARG002
            return None

        def print_body(self, text):
            self.bodies.append(text)

        def print_caption(self, *args, **kwargs):  # noqa: ARG002
            return None

        def print_line(self):
            return None

        def feed(self, lines=1):  # noqa: ARG002
            return None

        def flush_buffer(self):
            return None

    fake_printer = FakePrinter()

    monkeypatch.setattr(hardware, "printer", fake_printer)
    monkeypatch.setattr(
        adventure,
        "_get_state_path",
        lambda module_id: tmp_path / f"{module_id}.json",
    )

    adventure.format_adventure_receipt(
        fake_printer,
        config={},
        module_name="ADVENTURE",
        module_id="adventure-one",
    )

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "adventure-one"
    printed_before_invalid = list(fake_printer.bodies)

    assert selection_mode.can_handle_selection(7) is False
    assert selection_mode.handle_selection(7) is False

    assert selection_mode.is_selection_mode_active() is True
    assert selection_mode.get_current_module_id() == "adventure-one"
    assert fake_printer.bodies == printed_before_invalid

    selection_mode.exit_selection_mode()
