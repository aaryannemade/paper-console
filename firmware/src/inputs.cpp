#include "inputs.h"

#include <ArduinoJson.h>
#include <Preferences.h>

#include <deque>

#include "events.h"
#include "netmgr.h"
#include "relay.h"

namespace inputs {
namespace {

constexpr int kPinA = PC1_ENC_A_PIN;
constexpr int kPinB = PC1_ENC_B_PIN;
constexpr int kPinC = PC1_BUTTON_PIN;
constexpr int kPinLed = PC1_STATUS_LED_PIN;  // Active low.

constexpr int kMinPosition = 1;
constexpr int kMaxPosition = 8;
// With A on D0 and B on D1, clockwise turns decode as negative steps.
constexpr int kDirection = -1;
constexpr int kStepsPerDetent = 4;
constexpr uint32_t kButtonDebounceMs = 15;
constexpr uint32_t kHoldMs = 5000;
// Reset sequence: hold 10 s, then one detent clockwise, one anticlockwise, click.
constexpr uint32_t kResetArmMs = 10000;
constexpr uint32_t kResetStepTimeoutMs = 10000;  // Per step, once released.
constexpr uint32_t kResetClickMaxMs = 2000;      // The final press must be short.
constexpr uint32_t kLedBlinkMs = 150;

// Gray-code transition table: index (prev << 2 | cur); 2 = invalid (skipped state).
constexpr int8_t kQuadrature[16] = {0, 1, -1, 2, -1, 0, 2, 1, 1, 2, 0, -1, 2, -1, 1, 0};

// Idle: normal input. Armed: waiting for clockwise. Clockwise: waiting for
// anticlockwise. Anticlockwise: waiting for the click.
enum class Reset { Idle, Armed, Clockwise, Anticlockwise };

const char* resetName(Reset r) {
  switch (r) {
    case Reset::Armed: return "armed: turn clockwise";
    case Reset::Clockwise: return "turn anticlockwise";
    case Reset::Anticlockwise: return "click to reset";
    default: return "idle";
  }
}

volatile int g_position = kMinPosition;
volatile bool g_pressed = false;
volatile uint32_t g_detents = 0;
volatile uint32_t g_invalid = 0;
volatile uint32_t g_presses = 0;
volatile Reset g_reset = Reset::Idle;
volatile uint32_t g_lastTurnMs = 0;
volatile uint32_t g_lastActivityMs = 0;
String g_lastEvent = "none";
SemaphoreHandle_t g_mu = nullptr;

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

  // Button: debounced level, press bookkeeping.
  bool rawPressed = digitalRead(kPinC) == LOW;
  bool stablePressed = rawPressed;
  uint32_t rawSinceMs = millis();
  uint32_t pressStartMs = 0;
  bool holdSent = false;
  // The current press belongs to the reset sequence: nothing is sent for it.
  bool pressTaken = false;
  uint32_t pressCounter = 0;
  String pressId;

  uint32_t resetStepMs = 0;
  uint32_t ledBlinkUntilMs = 0;

  auto setReset = [&](Reset next, uint32_t now, const String& why) {
    g_reset = next;
    resetStepMs = now;
    ledBlinkUntilMs = next == Reset::Idle ? 0 : now + kLedBlinkMs;  // Blink = step accepted.
    logEvent(String("reset sequence: ") + why + (next == Reset::Idle ? "" : String(" -> ") + resetName(next)));
  };

  for (;;) {
    vTaskDelay(1);
    uint32_t now = millis();

    // --- Encoder: count transitions, act on each completed detent.
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
          g_lastActivityMs = now;
          bool clockwise = detents * kDirection > 0;
          if (g_reset != Reset::Idle) {
            // During the sequence turns are steps, not channel changes.
            if (g_reset == Reset::Armed && clockwise) {
              setReset(Reset::Clockwise, now, "clockwise");
            } else if (g_reset == Reset::Clockwise && !clockwise) {
              setReset(Reset::Anticlockwise, now, "anticlockwise");
            } else {
              setReset(Reset::Idle, now, String("cancelled (unexpected ") +
                                             (clockwise ? "clockwise" : "anticlockwise") + " turn)");
            }
          } else {
            g_position = wrap(g_position + detents * kDirection);
            g_lastTurnMs = now ? now : 1;
            logEvent(String("position ") + g_position + (clockwise ? " (clockwise)" : " (anticlockwise)"));
            events::position(g_position);
          }
        }
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
      g_lastActivityMs = now;
      if (stablePressed) {
        pressStartMs = now;
        holdSent = false;
        // Unique across reboots: random boot id + per-boot counter.
        pressId = String(relay::bootId()) + "-p" + (++pressCounter);
        ++g_presses;
        pressTaken = false;
        if (g_reset == Reset::Armed || g_reset == Reset::Clockwise) {
          // Pressed too early: stop, and do not let this press print.
          pressTaken = true;
          setReset(Reset::Idle, now, "cancelled (pressed before the turns)");
        } else if (g_reset == Reset::Anticlockwise) {
          pressTaken = true;  // The final click; judged on release.
        }
      } else {
        uint32_t held = now - pressStartMs;
        if (pressTaken) {
          if (g_reset == Reset::Anticlockwise) {
            if (held <= kResetClickMaxMs) {
              logEvent("reset sequence: complete");
              digitalWrite(kPinLed, LOW);
              netmgr::factoryReset("encoder reset sequence");
            }
            setReset(Reset::Idle, now, "cancelled (final press held too long)");
          } else if (g_reset != Reset::Idle) {
            resetStepMs = now;  // Released after arming: step timeout starts now.
            logEvent(String("released ") + pressId + " (kept for the reset sequence)");
          }
        } else if (holdSent) {
          logEvent(String("hold_release ") + pressId + " (" + String(held / 1000.0f, 1) + " s, position " +
                   g_position + ")");
          events::button("hold_release", pressId, g_position);
        } else {
          logEvent(String("press ") + pressId + " (" + held + " ms, position " + g_position + ")");
          events::button("press", pressId, g_position);
        }
      }
    }
    if (stablePressed && !pressTaken && !holdSent && now - pressStartMs >= kHoldMs) {
      holdSent = true;
      logEvent(String("hold ") + pressId + " (5 s reached, position " + g_position + ")");
      events::button("hold", pressId, g_position);
    }
    if (stablePressed && !pressTaken && holdSent && now - pressStartMs >= kResetArmMs) {
      // 10 s: the device keeps this press. Tell the server to drop the armed
      // hold so its release does not open Quick Actions.
      pressTaken = true;
      events::button("hold_cancel", pressId, g_position);
      setReset(Reset::Armed, now, "10 s hold");
    }

    // --- Reset sequence timeout (not while the arming press is still held).
    if (g_reset != Reset::Idle && !stablePressed && now - resetStepMs > kResetStepTimeoutMs) {
      setReset(Reset::Idle, now, "cancelled (timed out)");
    }
    // LED: lit while the sequence is active, briefly off when a step is accepted.
    bool ledOn = g_reset != Reset::Idle && static_cast<int32_t>(now - ledBlinkUntilMs) >= 0;
    digitalWrite(kPinLed, ledOn ? LOW : HIGH);
  }
}

}  // namespace

void begin() {
  g_mu = xSemaphoreCreateMutex();
  // The module has its own (weak) pull-ups; the internal ones add margin.
  pinMode(kPinA, INPUT_PULLUP);
  pinMode(kPinB, INPUT_PULLUP);
  pinMode(kPinC, INPUT_PULLUP);
  pinMode(kPinLed, OUTPUT);
  digitalWrite(kPinLed, HIGH);  // Off.
  // The position always starts at 1 and is not saved. Remove the key older
  // firmware kept in NVS.
  Preferences prefs;
  if (prefs.begin("pc1", false)) {
    if (prefs.isKey("position")) {
      prefs.remove("position");
    }
    prefs.end();
  }
  g_position = kMinPosition;
  Serial.printf("[input] encoder A=GPIO%d B=GPIO%d, button GPIO%d; position %d\n", kPinA, kPinB, kPinC,
                g_position);
  xTaskCreatePinnedToCore(inputTask, "inputs", 4096, nullptr, 4, nullptr, 1);
}

int currentPosition() { return g_position; }
uint32_t lastTurnMs() { return g_lastTurnMs; }
uint32_t lastActivityMs() { return g_lastActivityMs; }
int resetStep() { return static_cast<int>(g_reset); }

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
  doc["reset_sequence"] = resetName(g_reset);
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
