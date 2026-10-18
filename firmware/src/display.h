// SSD1306 128x64 I2C OLED (SDA = D4/GPIO5, SCL = D5/GPIO6, address 0x3C/0x3D).
//
// Drawn by a low-priority task that only reads cached state, so it never
// delays printing, input sampling or HTTP. Screens, highest priority first:
//   reset sequence step  >  channel, or the open menu's option (3 s after a
//   knob turn)  >  WiFi setup AP  >  printing  >  disconnected / connecting
//   >  open menu (menu.h)  >  idle clock + WiFi signal.
// The idle clock moves a few pixels every minute (burn-in), and the panel
// dims after a minute without input or a screen change. If no display
// answers at boot the firmware runs without one.
#pragma once

#include <Arduino.h>

namespace display {

void begin();
// "ok", "absent" (no I2C reply at boot). Reported in /health.
const char* status();
// Shows a final message (factory reset) and waits briefly for it to be drawn.
void showResetting();

}  // namespace display
