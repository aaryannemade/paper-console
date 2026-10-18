"""Menu metadata for the device display (selection mode) and Quick Actions UX."""

import asyncio

import pytest

import app.main as main
import app.selection_mode as selection_mode


@pytest.fixture(autouse=True)
def closed_menu():
    selection_mode.exit_selection_mode()
    yield
    selection_mode.exit_selection_mode()


def test_snapshot_lists_valid_positions_with_labels():
    seen = []
    selection_mode.add_menu_listener(seen.append)
    selection_mode.enter_selection_mode(
        lambda pos: None, "m", valid_positions={8, 3}, title="  Pick\none  ", labels={3: "Third " * 20}
    )
    snap = selection_mode.menu_snapshot()
    assert snap["active"] and snap["title"] == "Pick one"
    assert [o["position"] for o in snap["options"]] == [3, 8]
    assert len(snap["options"][0]["label"]) == selection_mode.MENU_LABEL_MAX
    assert snap["options"][1]["label"] == "Option 8"  # unlabeled valid slot
    assert seen[-1] == snap

    selection_mode.exit_selection_mode()
    closed = selection_mode.menu_snapshot()
    assert closed == {"seq": closed["seq"], "active": False} and closed["seq"] > snap["seq"]
    count = len(seen)
    selection_mode.exit_selection_mode()  # already closed: no new seq, no notification
    assert len(seen) == count and selection_mode.menu_snapshot()["seq"] == closed["seq"]


def test_unrestricted_menu_offers_every_position():
    selection_mode.enter_selection_mode(lambda pos: None, "m")
    snap = selection_mode.menu_snapshot()
    assert snap["title"] == "Menu" and [o["position"] for o in snap["options"]] == list(range(1, 9))


class Recorder:
    def __init__(self):
        self.lines = []

    def __getattr__(self, name):
        if name.startswith("print_"):
            return lambda text="", **kw: self.lines.append((name, text))
        raise AttributeError(name)

    def feed(self, *a, **kw):
        pass


def test_quick_actions_menu_labels_and_silent_timeout(monkeypatch):
    printer = Recorder()
    monkeypatch.setattr(main, "printer", printer)
    monkeypatch.setattr(main, "QUICK_ACTIONS_TIMEOUT_SECONDS", 0.05)

    async def run():
        await main.long_press_menu_trigger()
        snap = selection_mode.menu_snapshot()
        await asyncio.sleep(0.2)
        return snap

    snap = asyncio.run(run())
    assert snap["title"] == "Quick actions"
    assert {o["position"]: o["label"] for o in snap["options"]} == main.QUICK_ACTION_LABELS
    assert not any("Dial" in str(text) for _, text in printer.lines)
    # Timed out: closed without printing anything further.
    assert not selection_mode.is_selection_mode_active()
    assert ("print_header", "QUICK ACTIONS") in printer.lines
    assert printer.lines.count(("print_header", "QUICK ACTIONS")) == 1
