// Wall-clock time for the OLED idle screen.
//
// UTC comes from NTP (started once WiFi is connected; the ESP-IDF SNTP client
// keeps it synced). Local time is UTC plus the offset the server sends in each
// heartbeat reply ("clock": {"utc_offset_s", "time_format"}), so DST and
// timezone edits in settings arrive within one heartbeat. The device keeps no
// timezone database of its own.
#pragma once

#include <Arduino.h>
#include <time.h>

namespace timekeeping {

// Starts SNTP once WiFi is up; call from the Arduino loop.
void loop();
// From the heartbeat reply (uplink task).
void setZone(int32_t utcOffsetS, bool use24h);
// Fills `out` with local time; false until NTP has synced and the server has
// sent an offset at least once.
bool localTime(struct tm* out);
bool use24h();

}  // namespace timekeeping
