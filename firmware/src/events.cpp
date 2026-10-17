#include "events.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include <deque>

#include "inputs.h"
#include "relay.h"
#include "uplink.h"

namespace events {
namespace {

struct Event {
  String id;
  bool isPosition;
  int position;
  String gesture;
  String pressId;
  uint32_t createdMs;
};

constexpr size_t kMaxQueued = 16;
constexpr uint32_t kMaxAgeMs = 5000;        // Stale presses must not print late.
constexpr uint32_t kRetryDelayMs = 250;
constexpr uint32_t kRejectedPauseMs = 10000;  // 401/503: no tight retry loop.
constexpr uint16_t kRequestTimeoutMs = 4000;

SemaphoreHandle_t g_mu = nullptr;
TaskHandle_t g_task = nullptr;
std::deque<Event> g_queue;
uint32_t g_counter = 0;
String g_lastResult = "none";

// event_id: random boot id + counter, unique across reboots.
void enqueue(Event e) {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  e.id = String(relay::bootId()) + "-" + (++g_counter);
  // A newer position supersedes one still waiting (not the one in flight,
  // which is never in the queue).
  if (e.isPosition && !g_queue.empty() && g_queue.back().isPosition) {
    g_queue.back() = e;
  } else {
    if (g_queue.size() >= kMaxQueued) {
      g_queue.pop_front();
    }
    g_queue.push_back(e);
  }
  xSemaphoreGive(g_mu);
  if (g_task) {
    xTaskNotifyGive(g_task);
  }
}

String describe(const Event& e) {
  return e.isPosition ? String("position ") + e.position
                      : e.gesture + " " + e.pressId + " (position " + e.position + ")";
}

String toJson(const Event& e) {
  JsonDocument doc;
  doc["event_id"] = e.id;
  doc["type"] = e.isPosition ? "position" : "button";
  doc["position"] = e.position;
  if (!e.isPosition) {
    doc["gesture"] = e.gesture;
    doc["press_id"] = e.pressId;
  }
  String body;
  serializeJson(doc, body);
  return body;
}

void setResult(const String& line) {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  g_lastResult = line;
  xSemaphoreGive(g_mu);
  Serial.printf("[event] %s\n", line.c_str());
}

// Sends one event, retrying the same event_id until it is answered or stale.
// Returns false if the server rejected the device (401/503).
bool deliver(const Event& e) {
  String body = toJson(e);
  for (;;) {
    if (millis() - e.createdMs > kMaxAgeMs) {
      setResult(describe(e) + " -> dropped (stale, server unreachable)");
      return true;
    }
    if (WiFi.status() != WL_CONNECTED) {
      vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
      continue;
    }
    String response;
    int code = uplink::postJson("/api/device/events", body, &response, kRequestTimeoutMs);
    if (code == 200) {
      JsonDocument reply;
      String result = "ok";
      if (!deserializeJson(reply, response)) {
        result = reply["result"] | "ok";
        if (reply["duplicate"] | false) {
          result += " (duplicate)";
        }
      }
      setResult(describe(e) + " -> " + result);
      return true;
    }
    if (code == 401 || code == 503) {
      setResult(describe(e) + (code == 401 ? " -> rejected: relay token (401)"
                                           : " -> server has no relay token (503)"));
      return false;
    }
    if (code > 0) {
      // 422 etc.: the event itself is wrong; resending will not help.
      setResult(describe(e) + " -> HTTP " + code + " " + response.substring(0, 80));
      return true;
    }
    vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));  // Network error: same event_id again.
  }
}

void eventTask(void*) {
  uint32_t seenGeneration = 0;
  for (;;) {
    // Re-sync the knob position whenever the link comes (back) up.
    uint32_t generation = uplink::linkGeneration();
    if (generation != seenGeneration && uplink::linkOk()) {
      seenGeneration = generation;
      position(inputs::currentPosition());
    }

    Event e;
    bool have = false;
    xSemaphoreTake(g_mu, portMAX_DELAY);
    if (!g_queue.empty()) {
      e = g_queue.front();
      g_queue.pop_front();
      have = true;
    }
    xSemaphoreGive(g_mu);
    if (!have) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(500));
      continue;
    }
    if (!deliver(e)) {
      // Drop what is queued: it would be stale by the time we retry.
      xSemaphoreTake(g_mu, portMAX_DELAY);
      g_queue.clear();
      xSemaphoreGive(g_mu);
      vTaskDelay(pdMS_TO_TICKS(kRejectedPauseMs));
    }
  }
}

}  // namespace

void begin() {
  g_mu = xSemaphoreCreateMutex();
  xTaskCreatePinnedToCore(eventTask, "events", 8192, nullptr, 3, &g_task, 0);
}

void position(int pos) {
  enqueue({String(), true, pos, String(), String(), millis()});
}

void button(const char* gesture, const String& pressId, int pos) {
  enqueue({String(), false, pos, String(gesture), pressId, millis()});
}

String lastResult() {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  String r = g_lastResult;
  xSemaphoreGive(g_mu);
  return r;
}

}  // namespace events
