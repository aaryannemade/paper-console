"""Physical input events relayed by the ESP32 (migration Step 3b).

The ESP32 turns its controls into two kinds of events (see docs/relay-protocol.md):

* ``position`` — the selected channel position, 1-8 (absolute).
* ``button`` — a gesture: ``press`` (released before 5 s), ``hold`` (5 s reached,
  still held), or ``hold_release`` (released after a hold). Gestures of one
  physical press share a ``press_id``.

How the physical control produces these (e.g. a rotary encoder with a push switch)
is firmware logic. What a gesture *means* — print the channel, choose in a menu,
cancel, or quick actions — is decided here, using server-side state.

Each event has an ``event_id``; a retried event returns the original result
without acting twice. Events are processed one at a time.
"""

import logging
import threading
from collections import OrderedDict, deque
from typing import Callable, Dict, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.relay_auth import require_relay_token

logger = logging.getLogger(__name__)

MIN_POSITION = 1
MAX_POSITION = 8

Gesture = Literal["press", "hold", "hold_release"]
GESTURES = ("press", "hold", "hold_release")


class ChannelSelector:
    """Server-side channel position (1-8), as last reported by the device."""

    def __init__(self, position: int = MIN_POSITION):
        self._lock = threading.Lock()
        self.current_position = position

    def read_position(self) -> int:
        with self._lock:
            return self.current_position

    def set_position(self, position: int) -> None:
        position = int(position)
        if not MIN_POSITION <= position <= MAX_POSITION:
            raise ValueError(f"Position must be {MIN_POSITION}-{MAX_POSITION}")
        with self._lock:
            self.current_position = position


class DeviceInputEvent(BaseModel):
    # Unknown fields are ignored so newer firmware can add fields.
    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1, max_length=64)
    type: Literal["position", "button"]
    position: Optional[int] = Field(default=None, ge=MIN_POSITION, le=MAX_POSITION)
    gesture: Optional[Gesture] = None
    press_id: Optional[str] = Field(default=None, min_length=1, max_length=64)

    @field_validator("press_id", mode="before")
    @classmethod
    def _press_id_as_text(cls, value):
        if isinstance(value, bool):
            raise ValueError("press_id must be a string or integer")
        if isinstance(value, int):
            return str(value)
        return value

    @model_validator(mode="after")
    def _check_shape(self):
        if self.type == "position":
            if self.position is None:
                raise ValueError("position events require 'position'")
            if self.gesture is not None or self.press_id is not None:
                raise ValueError("position events do not take 'gesture' or 'press_id'")
        else:
            if self.gesture is None or self.press_id is None:
                raise ValueError("button events require 'gesture' and 'press_id'")
        return self


GestureHandler = Callable[[], str]


class InputProcessor:
    """Deduplicates events, tracks per-press state and dispatches gestures."""

    RECENT_EVENT_LIMIT = 256
    CONSUMED_PRESS_LIMIT = 16

    def __init__(self):
        self._lock = threading.Lock()
        self._recent: "OrderedDict[str, dict]" = OrderedDict()
        self._consumed_presses: deque = deque(maxlen=self.CONSUMED_PRESS_LIMIT)
        self._handlers: Dict[str, GestureHandler] = {}
        self.selector: Optional[ChannelSelector] = None

    def configure(self, selector: ChannelSelector, handlers: Dict[str, GestureHandler]):
        missing = set(GESTURES) - set(handlers)
        if missing:
            raise ValueError(f"Missing gesture handlers: {sorted(missing)}")
        with self._lock:
            self.selector = selector
            self._handlers = dict(handlers)

    def reset(self):
        with self._lock:
            self._recent.clear()
            self._consumed_presses.clear()

    def process(self, event: DeviceInputEvent) -> dict:
        with self._lock:
            cached = self._recent.get(event.event_id)
            if cached is not None:
                return {**cached, "duplicate": True}

            result = self._apply(event)
            response = {
                "event_id": event.event_id,
                "result": result,
                "position": self.selector.read_position() if self.selector else None,
                "duplicate": False,
            }
            self._recent[event.event_id] = response
            while len(self._recent) > self.RECENT_EVENT_LIMIT:
                self._recent.popitem(last=False)
            logger.info(
                "Device input %s %s%s -> %s",
                event.type,
                event.gesture or "",
                f" position={event.position}" if event.position else "",
                result,
            )
            return response

    def _apply(self, event: DeviceInputEvent) -> str:
        if self.selector is None:
            return "not_ready"

        # Button events may carry the position so a lost position event cannot
        # make a press act on the wrong channel.
        if event.position is not None:
            self.selector.set_position(event.position)
        if event.type == "position":
            return "position_set"

        if event.gesture == "hold_release" and event.press_id in self._consumed_presses:
            # This press already cancelled a print; its release must not open
            # Quick Actions.
            return "ignored_consumed"

        handler = self._handlers.get(event.gesture)
        if handler is None:
            return "not_ready"
        try:
            result = handler()
        except Exception:
            logger.exception("Device gesture %s failed", event.gesture)
            return "error"

        if (event.gesture, result) == ("hold", "cancel_requested"):
            self._consumed_presses.append(event.press_id)
        return result


processor = InputProcessor()


def init_runtime(selector: ChannelSelector, **handlers: GestureHandler) -> None:
    """Wire gesture handlers (from app.main) into the processor."""
    processor.configure(selector, handlers)


router = APIRouter()


@router.post(
    "/api/device/events",
    dependencies=[Depends(require_relay_token)],
    tags=["device"],
)
def post_device_event(event: DeviceInputEvent):
    """Receive one input event from the ESP32 relay.

    Returns promptly with the outcome. Busy/unavailable outcomes are results,
    not HTTP errors: the event was received and deliberately not acted on.
    """
    # A sync endpoint runs in FastAPI's thread pool, so handlers that touch the
    # printer (tactile feed) never block the event loop.
    return processor.process(event)
