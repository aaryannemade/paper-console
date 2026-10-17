#include "channels.h"

#include "uplink.h"

namespace channels {
namespace {

SemaphoreHandle_t g_mu = nullptr;
String g_names[kCount];
String g_hash;

}  // namespace

void begin() { g_mu = xSemaphoreCreateMutex(); }

void sync(const String& serverHash) {
  if (serverHash.isEmpty() || serverHash == hash()) {
    return;  // Older server without names, or nothing changed.
  }
  String response;
  int code = uplink::getJson("/api/device/channels", &response, 4000);
  if (code != 200) {
    Serial.printf("[channels] fetch failed (%d); retrying next heartbeat\n", code);
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, response)) {
    Serial.println("[channels] invalid reply; retrying next heartbeat");
    return;
  }
  String names[kCount];
  for (JsonObject channel : doc["channels"].as<JsonArray>()) {
    int position = channel["position"] | 0;
    if (position >= 1 && position <= kCount) {
      names[position - 1] = channel["name"] | "";
    }
  }
  String newHash = doc["hash"] | "";
  xSemaphoreTake(g_mu, portMAX_DELAY);
  for (int i = 0; i < kCount; ++i) {
    g_names[i] = names[i];
  }
  g_hash = newHash;
  xSemaphoreGive(g_mu);

  // Log outside the lock (USB writes can stall briefly).
  String line;
  for (int i = 0; i < kCount; ++i) {
    if (names[i].length()) {
      line += String(line.isEmpty() ? "" : ", ") + (i + 1) + "=" + names[i];
    }
  }
  Serial.printf("[channels] names %s: %s\n", newHash.c_str(), line.isEmpty() ? "(none)" : line.c_str());
}

String name(int position) {
  if (position < 1 || position > kCount) {
    return String();
  }
  xSemaphoreTake(g_mu, portMAX_DELAY);
  String n = g_names[position - 1];
  xSemaphoreGive(g_mu);
  return n;
}

String hash() {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  String h = g_hash;
  xSemaphoreGive(g_mu);
  return h;
}

void toJson(JsonObject out) {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  out["hash"] = g_hash;
  JsonArray names = out["names"].to<JsonArray>();
  for (int i = 0; i < kCount; ++i) {
    names.add(g_names[i]);
  }
  xSemaphoreGive(g_mu);
}

}  // namespace channels
