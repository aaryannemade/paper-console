#include "timekeeping.h"

#include <WiFi.h>

namespace timekeeping {
namespace {

// Anything before 2024 means SNTP has not set the clock yet.
constexpr time_t kMinValidEpoch = 1704067200;

bool g_sntpStarted = false;
volatile int32_t g_offsetS = 0;
volatile bool g_haveZone = false;
volatile bool g_use24h = false;

}  // namespace

void loop() {
  if (g_sntpStarted || WiFi.status() != WL_CONNECTED) {
    return;
  }
  g_sntpStarted = true;
  // Offsets stay 0: the TZ environment is UTC and local time is computed here.
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
  Serial.println("[time] SNTP started");
}

void setZone(int32_t utcOffsetS, bool use24h) {
  if (!g_haveZone || g_offsetS != utcOffsetS || g_use24h != use24h) {
    Serial.printf("[time] UTC offset %ld s, %s\n", static_cast<long>(utcOffsetS), use24h ? "24h" : "12h");
  }
  g_offsetS = utcOffsetS;
  g_use24h = use24h;
  g_haveZone = true;
}

bool localTime(struct tm* out) {
  time_t now = time(nullptr);
  if (now < kMinValidEpoch || !g_haveZone) {
    return false;
  }
  now += g_offsetS;
  gmtime_r(&now, out);
  return true;
}

bool use24h() { return g_use24h; }

}  // namespace timekeeping
