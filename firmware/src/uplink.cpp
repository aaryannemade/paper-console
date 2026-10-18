#include "uplink.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>

#include <memory>

#include "channels.h"
#include "health.h"
#include "relay.h"
#include "settings.h"
#include "timekeeping.h"

namespace uplink {
namespace {

QueueHandle_t g_statusQueue = nullptr;
SemaphoreHandle_t g_mu = nullptr;
volatile bool g_heartbeatWanted = true;
bool g_ok = false;
String g_message = "starting";
uint32_t g_lastOkMs = 0;
bool g_everOk = false;

constexpr uint32_t kDefaultIntervalMs = 10000;
constexpr uint32_t kRejectedRetryMs = 30000;  // 401/503: do not hammer the server.
constexpr uint32_t kErrorRetryMs = 5000;

volatile uint32_t g_generation = 0;

void setState(bool ok, const String& message) {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  if (ok) {
    g_lastOkMs = millis();
    g_everOk = true;
    if (!g_ok) {
      ++g_generation;  // Link (re)established.
    }
  }
  bool changed = g_ok != ok || g_message != message;
  g_ok = ok;
  g_message = message;
  xSemaphoreGive(g_mu);
  // Log outside the lock: the web server's status page waits on it.
  if (changed) {
    Serial.printf("[link] %s\n", message.c_str());
  }
}

uint32_t sendHeartbeat() {
  relay::Snapshot snap = relay::snapshot();
  JsonDocument doc;
  doc["device_id"] = relay::deviceId();
  doc["boot_id"] = relay::bootId();
  doc["firmware"] = PC1_FIRMWARE_VERSION;
  health::toJson(doc["health"].to<JsonObject>());
  doc["callback_url"] = "http://" + WiFi.localIP().toString();
  JsonObject printer = doc["printer"].to<JsonObject>();
  printer["state"] = snap.printerState;
  printer["detail"] = nullptr;
  printer["width_dots"] = PC1_PRINTER_WIDTH_DOTS;
  printer["baudrate"] = PC1_PRINTER_BAUD;
  JsonObject limits = doc["limits"].to<JsonObject>();
  limits["max_chunk_bytes"] = relay::kMaxChunkBytes;
  limits["max_job_bytes"] = relay::kMaxJobBytes;
  if (snap.activeJobId.length()) {
    doc["active_job_id"] = snap.activeJobId;
  } else {
    doc["active_job_id"] = nullptr;
  }
  String body;
  serializeJson(doc, body);

  String response;
  int code = postJson("/api/device/heartbeat", body, &response);
  if (code == 200) {
    JsonDocument reply;
    uint32_t intervalS = 10;
    String channelsHash;
    if (!deserializeJson(reply, response)) {
      intervalS = reply["heartbeat_interval_s"] | 10;
      channelsHash = reply["channels_hash"] | "";
      JsonObject clock = reply["clock"];
      if (!clock.isNull() && clock["utc_offset_s"].is<int32_t>()) {
        // Plausible offsets only (UTC-14 .. UTC+14).
        int32_t offset = constrain(clock["utc_offset_s"].as<int32_t>(), -14 * 3600, 14 * 3600);
        timekeeping::setZone(offset, String(clock["time_format"] | "12h") == "24h");
      }
    }
    intervalS = constrain(intervalS, 2u, 30u);
    setState(true, "connected");
    // Fetches the names only when the server's hash differs from ours.
    channels::sync(channelsHash);
    return intervalS * 1000;
  }
  if (code == 401) {
    setState(false, "server rejected the relay token (401)");
    return kRejectedRetryMs;
  }
  if (code == 503) {
    setState(false, "server has no relay token configured (503)");
    return kRejectedRetryMs;
  }
  if (code > 0) {
    setState(false, String("server answered HTTP ") + code);
    return kRejectedRetryMs;
  }
  setState(false, String("server unreachable: ") + HTTPClient::errorToString(code));
  return kErrorRetryMs;
}

void sendStatus(const StatusReport& report) {
  JsonDocument doc;
  doc["state"] = report.state;
  doc["boot_id"] = relay::bootId();
  doc["records_written"] = report.recordsWritten;
  doc["bytes_written"] = report.bytesWritten;
  if (report.error[0]) {
    doc["error"] = report.error;
  } else {
    doc["error"] = nullptr;
  }
  String body;
  serializeJson(doc, body);
  String path = String("/api/device/jobs/") + report.jobId + "/status";
  for (int attempt = 0; attempt < 2; ++attempt) {
    int code = postJson(path.c_str(), body, nullptr);
    if (code > 0) {
      return;  // Delivered (404 = container no longer knows the job: stop).
    }
  }
  // The container also polls GET /jobs/{id}, so a lost report is recovered.
}

void uplinkTask(void*) {
  uint32_t nextHeartbeatMs = 0;
  for (;;) {
    StatusReport report;
    bool haveReport = xQueueReceive(g_statusQueue, &report, pdMS_TO_TICKS(100)) == pdTRUE;
    const Settings& s = settings::current();
    if (WiFi.status() != WL_CONNECTED || s.server.isEmpty() || s.token.isEmpty()) {
      g_heartbeatWanted = true;  // Send one as soon as we are back.
      if (s.server.isEmpty()) {
        setState(false, "server URL not set");
      } else if (s.token.isEmpty()) {
        setState(false, "relay token not set");
      } else {
        setState(false, "WiFi not connected");
      }
      continue;
    }
    if (haveReport) {
      sendStatus(report);
    }
    if (g_heartbeatWanted || static_cast<int32_t>(millis() - nextHeartbeatMs) >= 0) {
      g_heartbeatWanted = false;
      nextHeartbeatMs = millis() + sendHeartbeat();
    }
  }
}

}  // namespace

void begin() {
  g_mu = xSemaphoreCreateMutex();
  g_statusQueue = xQueueCreate(16, sizeof(StatusReport));
  // Large stack: an https server URL runs the TLS handshake on this task.
  xTaskCreatePinnedToCore(uplinkTask, "uplink", 16384, nullptr, 2, nullptr, 0);
}

void queueStatus(const StatusReport& report) {
  if (g_statusQueue) {
    xQueueSend(g_statusQueue, &report, 0);
  }
}

void requestHeartbeat() { g_heartbeatWanted = true; }

bool linkOk() { return g_ok; }

uint32_t linkGeneration() { return g_generation; }

int postJson(const char* path, const String& body, String* response, uint16_t timeoutMs) {
  return request("POST", path, &body, response, timeoutMs);
}

int getJson(const char* path, String* response, uint16_t timeoutMs) {
  return request("GET", path, nullptr, response, timeoutMs);
}

int request(const char* method, const char* path, const String* body, String* response,
            uint16_t timeoutMs) {
  const Settings& s = settings::current();
  if (s.server.isEmpty() || s.token.isEmpty()) {
    return HTTPC_ERROR_CONNECTION_REFUSED;
  }
  String url = s.server + path;
  // Clients are declared before HTTPClient so they outlive it: its destructor
  // may still call stop() on the client.
  WiFiClient plain;
  std::unique_ptr<WiFiClientSecure> secure;
  HTTPClient http;
  http.setReuse(false);
  bool started;
  if (url.startsWith("https://") || url.startsWith("HTTPS://")) {
    secure.reset(new WiFiClientSecure());
    secure->setInsecure();  // LAN server; certificate is not verified.
    started = http.begin(*secure, url);
  } else {
    started = http.begin(plain, url);
  }
  if (!started) {
    return HTTPC_ERROR_CONNECTION_REFUSED;
  }
  http.setConnectTimeout(3000);
  http.setTimeout(timeoutMs);
  http.addHeader("Authorization", "Bearer " + s.token);
  int code;
  if (body) {
    http.addHeader("Content-Type", "application/json");
    code = http.sendRequest(method, *body);
  } else {
    code = http.sendRequest(method);
  }
  if (response && code > 0) {
    *response = http.getString();
  }
  http.end();
  return code;
}

LinkState state() {
  xSemaphoreTake(g_mu, portMAX_DELAY);
  LinkState st{g_ok, g_message, g_everOk ? static_cast<int32_t>((millis() - g_lastOkMs) / 1000) : -1};
  if (st.ok && st.lastOkAgoS > 30) {
    st.ok = false;
    st.message = "heartbeat acknowledgement stale";
  }
  xSemaphoreGive(g_mu);
  return st;
}

}  // namespace uplink
