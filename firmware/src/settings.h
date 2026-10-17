// Persistent device settings (NVS). Loaded once at boot; saving writes NVS and
// the device restarts, so the in-memory copy never changes while running.
#pragma once

#include <Arduino.h>

struct Settings {
  String ssid;
  String pass;
  String server;  // Container base URL, e.g. http://192.168.1.20:8000
  String token;   // Shared relay bearer token (PC1_RELAY_TOKEN)
};

namespace settings {

void load();
const Settings& current();
bool wifiConfigured();
bool save(const Settings& s);
void erase();

// Same rules as the container: 16-128 chars, no whitespace.
bool validToken(const String& token);
// http(s)://host[:port][/path], no credentials/query/fragment. Strips trailing '/'.
bool normalizeServer(const String& in, String& out);
bool safeEquals(const String& a, const String& b);

}  // namespace settings
