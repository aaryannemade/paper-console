"""
Selection Mode System for Paper Console.

This module provides infrastructure for interactive modules that need to
override normal channel switching behavior. When selection mode is active,
button presses read the dial position and pass it to a callback function
instead of triggering the current channel.

Example usage:
    from app.selection_mode import enter_selection_mode, exit_selection_mode
    
    def my_callback(dial_position: int):
        if dial_position == 8:
            exit_selection_mode()
        else:
            # Process the choice...
            pass
    
    enter_selection_mode(my_callback, "my-module-id")
"""

import logging
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


# --- Selection Mode State ---

_selection_mode_active = False
_selection_callback: Optional[Callable[[int], None]] = None
_current_module_id: Optional[str] = None
_valid_positions: Optional[set[int]] = None
_selection_in_progress = False
_selection_lock = threading.Lock()
# What the device display shows for the open menu ("Menu display" in readme.md).
_menu_title: str = ""
_menu_labels: Dict[int, str] = {}
_menu_seq = 0
_listeners: List[Callable[[dict], None]] = []

MENU_TITLE_MAX = 24
MENU_LABEL_MAX = 48


def _next_seq() -> int:
    """Strictly increasing, and larger after a container restart (wall clock, us)."""
    global _menu_seq
    _menu_seq = max(_menu_seq + 1, time.time_ns() // 1000)
    return _menu_seq


def _clean(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _snapshot_locked() -> dict:
    if not _selection_mode_active:
        return {"seq": _menu_seq, "active": False}
    positions = sorted(_valid_positions) if _valid_positions is not None else list(range(1, 9))
    return {
        "seq": _menu_seq,
        "active": True,
        "title": _menu_title,
        "options": [
            {"position": pos, "label": _menu_labels.get(pos) or f"Option {pos}"}
            for pos in positions
            if 1 <= pos <= 8
        ],
    }


def menu_snapshot() -> dict:
    """The open menu for the device display, or ``{"active": False}``.

    ``seq`` changes on every enter/exit; the device keeps the highest it has seen.
    """
    with _selection_lock:
        return _snapshot_locked()


def add_menu_listener(listener: Callable[[dict], None]) -> None:
    """Call ``listener(snapshot)`` after every menu change (e.g. to push it to the device)."""
    if listener not in _listeners:
        _listeners.append(listener)


def _notify(snapshot: dict) -> None:
    for listener in list(_listeners):
        try:
            listener(snapshot)
        except Exception:
            logger.exception("Menu listener failed")


def is_selection_mode_active() -> bool:
    """Check if selection mode is currently active."""
    with _selection_lock:
        return _selection_mode_active


def get_current_module_id() -> Optional[str]:
    """Get the ID of the module that owns the current selection mode."""
    with _selection_lock:
        return _current_module_id


def can_handle_selection(dial_position: int) -> bool:
    """Return True when the current mode would accept this dial position."""
    with _selection_lock:
        return (
            _selection_mode_active
            and _selection_callback is not None
            and not _selection_in_progress
            and (_valid_positions is None or dial_position in _valid_positions)
        )


def enter_selection_mode(
    callback: Callable[[int], None],
    module_id: str,
    valid_positions: Optional[Iterable[int]] = None,
    title: str = "",
    labels: Optional[Dict[int, str]] = None,
):
    """
    Enter selection mode - the next button press will call callback(dial_position).
    
    Args:
        callback: Function to call with dial position (1-8) when button is pressed
        module_id: ID of the module that entered selection mode (for debugging)
        valid_positions: Optional dial slots that should trigger the callback
        title: Short menu title for the device display (e.g. "Quick actions")
        labels: Display label per valid position (e.g. {8: "Cancel"})
    """
    global _selection_mode_active, _selection_callback, _current_module_id, _valid_positions
    global _menu_title, _menu_labels
    with _selection_lock:
        _selection_mode_active = True
        _selection_callback = callback
        _current_module_id = module_id
        _valid_positions = set(valid_positions) if valid_positions is not None else None
        _menu_title = _clean(title, MENU_TITLE_MAX) or "Menu"
        _menu_labels = {
            int(pos): _clean(text, MENU_LABEL_MAX) for pos, text in (labels or {}).items()
        }
        _next_seq()
        snapshot = _snapshot_locked()
    logger.info(f"Selection mode: Entered for module {module_id}")
    _notify(snapshot)


def exit_selection_mode():
    """Exit selection mode and return to normal channel switching."""
    global _selection_mode_active, _selection_callback, _current_module_id, _valid_positions
    global _menu_title, _menu_labels
    with _selection_lock:
        was_active = _selection_mode_active
        _selection_mode_active = False
        _selection_callback = None
        _current_module_id = None
        _valid_positions = None
        _menu_title = ""
        _menu_labels = {}
        if was_active:
            _next_seq()
        snapshot = _snapshot_locked()
    logger.info("Selection mode: Exited")
    if was_active:
        _notify(snapshot)


def handle_selection(dial_position: int) -> bool:
    """
    Handle a button press while in selection mode.
    
    Called by the HTTP input handler when a press arrives in selection mode.
    
    Args:
        dial_position: The current dial position (1-8)
    
    Returns:
        True if handled (was in selection mode), False otherwise.
    """
    global _selection_in_progress
    with _selection_lock:
        if (
            not _selection_mode_active
            or _selection_callback is None
            or _selection_in_progress
            or (_valid_positions is not None and dial_position not in _valid_positions)
        ):
            return False

        callback = _selection_callback
        _selection_in_progress = True

    try:
        callback(dial_position)
    except Exception as e:
        logger.error(f"Selection mode callback error: {e}")
        exit_selection_mode()
    finally:
        with _selection_lock:
            _selection_in_progress = False

    return True
