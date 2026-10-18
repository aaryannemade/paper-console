// Physical controls: EC11 rotary encoder (A/B) with push button (C).
//
// The knob selects the channel position 1-8: one detent per step, clockwise
// increases, wrapping 8 -> 1 and 1 -> 8. The position starts at 1 on every
// boot (not saved) and is sent to the server whenever the link comes up. The
// button reports gestures as in readme.md ("Input events"): press (released
// before 5 s), hold (5 s reached), hold_release.
//
// Reset sequence (same effect as holding BOOT 5 s: erase settings, reopen
// setup): hold the button 10 s (the user LED lights; the server is sent
// hold_cancel so the release opens no menu), then turn one detent clockwise,
// one anticlockwise, and click (press < 2 s). A wrong turn, an early press or
// 10 s without the next step cancels it; those inputs are not sent.
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
// millis() of the last channel change by the knob (0 = none since boot).
uint32_t lastTurnMs();
// millis() of the last knob/button activity of any kind (for display wake).
uint32_t lastActivityMs();
// Reset sequence step for the display: 0 idle, 1 armed (turn clockwise),
// 2 turn anticlockwise, 3 click to reset.
int resetStep();
// Prints queued "[input] ..." log lines; call from the Arduino loop.
void loop();
// Current state and counters for /ui/inputs.
String diagnosticsJson();

}  // namespace inputs
