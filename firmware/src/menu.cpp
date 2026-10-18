#include "menu.h"

namespace menu {
namespace {

SemaphoreHandle_t g_mu = nullptr;
State g_state;
uint64_t g_seq = 0;

}  // namespace

void begin() { g_mu = xSemaphoreCreateMutex(); }

bool apply(JsonVariantConst value, const char* source) {
  if (!value.is<JsonObjectConst>() || !value["seq"].is<uint64_t>()) {
    return false;
  }
  uint64_t seq = value["seq"].as<uint64_t>();
  State next;
  next.active = value["active"] | false;
  if (next.active) {
    next.title = value["title"] | "";
    for (JsonObjectConst option : value["options"].as<JsonArrayConst>()) {
      int position = option["position"] | 0;
      if (position >= 1 && position <= kPositions) {
        next.valid[position - 1] = true;
        next.labels[position - 1] = option["label"] | "";
      }
    }
  }

  xSemaphoreTake(g_mu, portMAX_DELAY);
  bool newer = seq > g_seq;
  if (newer) {
    g_seq = seq;
    g_state = next;
  }
  xSemaphoreGive(g_mu);
  if (!newer) {
    return false;
  }

  // Log outside the lock (USB writes can stall briefly).
  if (!next.active) {
    Serial.printf("[menu] closed (%s)\n", source);
    return true;
  }
  String line;
  for (int i = 0; i < kPositions; ++i) {
    if (next.valid[i]) {
      line += String(line.isEmpty() ? "" : ", ") + (i + 1) + "=" + next.labels[i];
    }
  }
  Serial.printf("[menu] %s (%s): %s\n", next.title.c_str(), source, line.c_str());
  return true;
}

State current() {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  State copy = g_state;
  xSemaphoreGive(g_mu);
  return copy;
}

}  // namespace menu
