// The server's open menu (Quick Actions, app selection, Adventure choices) for
// the OLED; see "Menu display" in readme.md.
//
// The container pushes it to POST /menu whenever a menu opens or closes, and
// every heartbeat reply repeats it. Updates are kept only if their `seq` is
// higher than the one held, so a late heartbeat cannot undo a newer push.
// RAM only: after a restart the first heartbeat delivers the current menu.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

namespace menu {

constexpr int kPositions = 8;

struct State {
  bool active = false;
  String title;
  bool valid[kPositions] = {};
  String labels[kPositions];  // Index = position - 1; "" where not valid.
};

void begin();
// Applies a menu object ({"seq", "active", "title", "options": [...]}).
// Returns true if it replaced the held state.
bool apply(JsonVariantConst value, const char* source);
State current();

}  // namespace menu
