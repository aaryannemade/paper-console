// Physical controls: EC11 rotary encoder (A/B) with push button (C).
//
// The knob selects the channel position 1-8: one detent per step, clockwise
// increases, wrapping 8 -> 1 and 1 -> 8. The position is kept in NVS so it
// survives restarts. The button reports gestures as in docs/relay-protocol.md:
// press (released before 5 s), hold (5 s reached), hold_release.
//
// Measured on the DFRobot SEN0235 module: A, B and C idle high (module
// pull-ups), the button pulls C low, and one detent is a full quadrature cycle
// (4 transitions) resting at A=B=1.
//
// Events are logged ("[input] ...") and sent to the server by events.cpp.
#pragma once

#include <Arduino.h>

namespace inputs {

void begin();
int currentPosition();
// Prints queued "[input] ..." log lines; call from the Arduino loop.
void loop();
// Current state and counters for /ui/inputs.
String diagnosticsJson();

}  // namespace inputs
