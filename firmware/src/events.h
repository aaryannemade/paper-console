// Input events to the server: POST /api/device/events ("Input events" in readme.md).
//
// Events are queued by the input task and sent one at a time, in order, from
// a dedicated task (heartbeats never delay a press). A timeout or connection
// error resends the same event_id, which the server deduplicates. Events
// older than kMaxAgeMs are dropped rather than replayed late. Queued position
// events collapse to the newest one, and the position is re-sent whenever the
// link to the server is (re)established.
#pragma once

#include <Arduino.h>

namespace events {

void begin();
void position(int position);
// gesture: "press" | "hold" | "hold_release".
void button(const char* gesture, const String& pressId, int position);
// Last result per kind, for the status page.
String lastResult();

}  // namespace events
