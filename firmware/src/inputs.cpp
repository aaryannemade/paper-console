#include "inputs.h"

#include <ArduinoJson.h>
#include <Preferences.h>

#include <deque>

#include "events.h"
#include "relay.h"

namespace inputs {
namespace {

constexpr int kPinA = PC1_ENC_A_PIN;
constexpr int kPinB = PC1_ENC_B_PIN;
constexpr int kPinC = PC1_BUTTON_PIN;

constexpr int kMinPosition = 1;
constexpr int kMaxPosition = 8;
// With A on D0 and B on D1, clockwise turns decode as negative steps.
constexpr int kDirection = -1;
constexpr int kStepsPerDetent = 4;
constexpr uint32_t kButtonDebounceMs = 15;
constexpr uint32_t kHoldMs = 5000;
constexpr uint32_t kSavePositionAfterMs = 2000;  // NVS write once the knob rests.

// Gray-code transition table: index (prev << 2 | cur); 2 = invalid (skipped state).
constexpr int8_t kQuadrature[16] = {0, 1, -1, 2, -1, 0, 2, 1, 1, 2, 0, -1, 2, -1, 1, 0};

volatile int g_position = kMinPosition;
volatile bool g_pressed = false;
volatile uint32_t g_detents = 0;
volatile uint32_t g_invalid = 0;
volatile uint32_t g_presses = 0;
String g_lastEvent = "none";
SemaphoreHandle_t g_mu = nullptr;

int loadPosition() {
  Preferences prefs;
  int pos = kMinPosition;
  if (prefs.begin("pc1", true)) {
    pos = prefs.getUChar("position", kMinPosition);
    prefs.end();
  }
  return (pos >= kMinPosition && pos <= kMaxPosition) ? pos : kMinPosition;
}

void savePosition(int pos) {
  Preferences prefs;
  if (prefs.begin("pc1", false)) {
    prefs.putUChar("position", static_cast<uint8_t>(pos));
    prefs.end();
  }
}

// Log lines are queued and printed from loop(): a USB log write can stall for
// up to the TX timeout, which would make the sampler miss encoder states.
std::deque<String> g_logQueue;
constexpr size_t kMaxQueuedLogs = 32;

void logEvent(const String& line) {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  g_lastEvent = line;
  if (g_logQueue.size() < kMaxQueuedLogs) {
    g_logQueue.push_back(line);
  }
  xSemaphoreGive(g_mu);
}

int wrap(int pos) {
  int span = kMaxPosition - kMinPosition + 1;
  return ((pos - kMinPosition) % span + span) % span + kMinPosition;
}

void inputTask(void*) {
  int a = digitalRead(kPinA), b = digitalRead(kPinB);
  int quarterSteps = 0;
  uint32_t positionChangedMs = 0;
  int savedPosition = g_position;

  // Button: debounced level, press bookkeeping.
  bool rawPressed = digitalRead(kPinC) == LOW;
  bool stablePressed = rawPressed;
  uint32_t rawSinceMs = millis();
  uint32_t pressStartMs = 0;
  bool holdSent = false;
  uint32_t pressCounter = 0;
  String pressId;

  for (;;) {
    vTaskDelay(1);
    uint32_t now = millis();

    // --- Encoder: count transitions, emit one step per completed detent.
    int na = digitalRead(kPinA), nb = digitalRead(kPinB);
    if (na != a || nb != b) {
      int8_t step = kQuadrature[((a << 1 | b) << 2) | (na << 1 | nb)];
      if (step == 2) {
        ++g_invalid;  // Missed an intermediate state; ignore it.
      } else {
        quarterSteps += step;
      }
      a = na;
      b = nb;
      if (a == 1 && b == 1) {  // Detent rest position.
        int detents = 0;
        if (quarterSteps >= kStepsPerDetent - 1) {
          detents = 1;
        } else if (quarterSteps <= -(kStepsPerDetent - 1)) {
          detents = -1;
        }
        quarterSteps = 0;  // Partial turns (bounce, rocking) are dropped here.
        if (detents) {
          ++g_detents;
          g_position = wrap(g_position + detents * kDirection);
          positionChangedMs = now;
          logEvent(String("position ") + g_position + (detents * kDirection > 0 ? " (clockwise)" : " (anticlockwise)"));
          events::position(g_position);
        }
      }
    }
    if (positionChangedMs && now - positionChangedMs > kSavePositionAfterMs) {
      positionChangedMs = 0;
      if (g_position != savedPosition) {
        savedPosition = g_position;
        savePosition(savedPosition);
      }
    }

    // --- Button: debounce, then gestures.
    bool r = digitalRead(kPinC) == LOW;
    if (r != rawPressed) {
      rawPressed = r;
      rawSinceMs = now;
    }
    if (rawPressed != stablePressed && now - rawSinceMs >= kButtonDebounceMs) {
      stablePressed = rawPressed;
      g_pressed = stablePressed;
      if (stablePressed) {
        pressStartMs = now;
        holdSent = false;
        // Unique across reboots: random boot id + per-boot counter.
        pressId = String(relay::bootId()) + "-p" + (++pressCounter);
        ++g_presses;
      } else {
        uint32_t held = now - pressStartMs;
        if (holdSent) {
          logEvent(String("hold_release ") + pressId + " (" + String(held / 1000.0f, 1) + " s, position " +
                   g_position + ")");
          events::button("hold_release", pressId, g_position);
        } else {
          logEvent(String("press ") + pressId + " (" + held + " ms, position " + g_position + ")");
          events::button("press", pressId, g_position);
        }
      }
    }
    if (stablePressed && !holdSent && now - pressStartMs >= kHoldMs) {
      holdSent = true;
      logEvent(String("hold ") + pressId + " (5 s reached, position " + g_position + ")");
      events::button("hold", pressId, g_position);
    }
  }
}

}  // namespace

void begin() {
  g_mu = xSemaphoreCreateMutex();
  // The module has its own (weak) pull-ups; the internal ones add margin.
  pinMode(kPinA, INPUT_PULLUP);
  pinMode(kPinB, INPUT_PULLUP);
  pinMode(kPinC, INPUT_PULLUP);
  g_position = loadPosition();
  Serial.printf("[input] encoder A=GPIO%d B=GPIO%d, button GPIO%d; position %d\n", kPinA, kPinB, kPinC,
                g_position);
  xTaskCreatePinnedToCore(inputTask, "inputs", 4096, nullptr, 4, nullptr, 1);
}

int currentPosition() { return g_position; }

void loop() {
  for (;;) {
    String line;
    xSemaphoreTake(g_mu, portMAX_DELAY);
    if (!g_logQueue.empty()) {
      line = g_logQueue.front();
      g_logQueue.pop_front();
    }
    xSemaphoreGive(g_mu);
    if (line.isEmpty()) {
      return;
    }
    Serial.printf("[input] %s\n", line.c_str());
  }
}

String diagnosticsJson() {
  JsonDocument doc;
  doc["position"] = g_position;
  doc["pressed"] = g_pressed;
  doc["detents"] = g_detents;
  doc["presses"] = g_presses;
  doc["invalid_transitions"] = g_invalid;
  doc["raw"]["a"] = digitalRead(kPinA);
  doc["raw"]["b"] = digitalRead(kPinB);
  doc["raw"]["c"] = digitalRead(kPinC);
  xSemaphoreTake(g_mu, portMAX_DELAY);
  doc["last_event"] = g_lastEvent;
  xSemaphoreGive(g_mu);
  doc["last_server_result"] = events::lastResult();
  String body;
  serializeJson(doc, body);
  return body;
}

}  // namespace inputs
