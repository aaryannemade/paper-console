#include "settings.h"

#include <Preferences.h>

namespace settings {
namespace {
Settings g_settings;
constexpr const char* kNamespace = "pc1";
}  // namespace

void load() {
  Preferences prefs;
  if (!prefs.begin(kNamespace, true)) {
    return;  // Namespace not created yet: factory-fresh device.
  }
  g_settings.ssid = prefs.getString("ssid", "");
  g_settings.pass = prefs.getString("pass", "");
  g_settings.server = prefs.getString("server", "");
  g_settings.token = prefs.getString("token", "");
  prefs.end();
}

const Settings& current() { return g_settings; }

bool wifiConfigured() { return g_settings.ssid.length() > 0; }

bool save(const Settings& s) {
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) {
    return false;
  }
  bool ok = prefs.putString("ssid", s.ssid) == s.ssid.length();
  ok = prefs.putString("pass", s.pass) == s.pass.length() && ok;
  ok = prefs.putString("server", s.server) == s.server.length() && ok;
  ok = prefs.putString("token", s.token) == s.token.length() && ok;
  prefs.end();
  return ok;
}

void erase() {
  Preferences prefs;
  if (prefs.begin(kNamespace, false)) {
    prefs.clear();
    prefs.end();
  }
}

bool validToken(const String& token) {
  if (token.length() < 16 || token.length() > 128) {
    return false;
  }
  for (size_t i = 0; i < token.length(); ++i) {
    char c = token[i];
    if (c <= ' ' || c == 0x7f) {
      return false;
    }
  }
  return true;
}

bool normalizeServer(const String& in, String& out) {
  String url = in;
  url.trim();
  while (url.endsWith("/")) {
    url.remove(url.length() - 1);
  }
  String lower = url;
  lower.toLowerCase();
  size_t schemeLen = 0;
  if (lower.startsWith("http://")) {
    schemeLen = 7;
  } else if (lower.startsWith("https://")) {
    schemeLen = 8;
  } else {
    return false;
  }
  if (url.length() <= schemeLen || url.length() > 200) {
    return false;
  }
  for (size_t i = 0; i < url.length(); ++i) {
    char c = url[i];
    if (c <= ' ' || c == '?' || c == '#' || c == '@' || c == '"' || c == 0x7f) {
      return false;
    }
  }
  char first = url[schemeLen];
  if (first == '/' || first == ':') {
    return false;  // No host.
  }
  out = url;
  return true;
}

bool safeEquals(const String& a, const String& b) {
  if (a.length() != b.length()) {
    return false;
  }
  uint8_t diff = 0;
  for (size_t i = 0; i < a.length(); ++i) {
    diff |= static_cast<uint8_t>(a[i]) ^ static_cast<uint8_t>(b[i]);
  }
  return diff == 0;
}

}  // namespace settings
