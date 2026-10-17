#include "netmgr.h"

#include <ArduinoJson.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include <algorithm>
#include <vector>

#include "relay.h"
#include "settings.h"
#include "uplink.h"

namespace netmgr {
namespace {

DNSServer g_dns;
volatile bool g_apActive = false;
bool g_wasConnected = false;
uint32_t g_disconnectedSinceMs = 0;
uint32_t g_connectedSinceMs = 0;
uint32_t g_lastStaRetryMs = 0;
uint32_t g_restartAtMs = 0;
uint32_t g_buttonDownMs = 0;
String g_apSsid;

struct Net {
  String ssid;
  int32_t rssi;
  bool open;
  uint32_t seenMs;
};
// Scan cache, read by the HTTP task; scans are started/collected in loop().
SemaphoreHandle_t g_scanMu = nullptr;
std::vector<Net> g_networks;
volatile bool g_scanRequested = false;
volatile bool g_scanning = false;
volatile bool g_scanDone = false;  // Set by the SCAN_DONE event.
uint32_t g_scanStartedMs = 0;

const IPAddress kApIp(192, 168, 4, 1);
const IPAddress kApMask(255, 255, 255, 0);
constexpr uint32_t kFallbackAfterMs = 30000;
constexpr uint32_t kApRetryEveryMs = 60000;
constexpr uint32_t kResetHoldMs = 5000;
constexpr size_t kMaxNetworks = 25;
constexpr uint32_t kScanGiveUpMs = 10000;
constexpr uint32_t kNetworkStaleMs = 5 * 60 * 1000;

// Scans are started with esp_wifi_scan_start directly: WiFi.scanNetworks()
// forces a 100 ms minimum dwell and declares the scan finished after
// max_ms_per_chan * 20 even while the radio is still scanning, after which
// the late SCAN_DONE races with our use of the results.
//
// Rescans use a short dwell: the single radio leaves the setup AP channel for
// each scanned channel, and long scans (the default is up to 300 ms x 13
// channels) make phones drop the setup network.
struct ScanTiming {
  uint32_t minMs, maxMs;
};
constexpr ScanTiming kBootScan{100, 300};  // Nobody connected yet: be thorough.
constexpr ScanTiming kRescan{30, 80};      // ~1 s off-channel in total.

// Merges finished scan results into the cache. A short rescan can miss weak
// networks, so earlier sightings are kept until they go stale.
void collectScan(int count) {
  uint32_t now = millis();
  int found = 0;
  xSemaphoreTake(g_scanMu, portMAX_DELAY);
  for (int i = 0; i < count; ++i) {
    String ssid = WiFi.SSID(i);
    if (ssid.isEmpty()) {
      continue;
    }
    ++found;
    int32_t rssi = WiFi.RSSI(i);
    auto it = std::find_if(g_networks.begin(), g_networks.end(), [&](const Net& n) { return n.ssid == ssid; });
    if (it == g_networks.end()) {
      g_networks.push_back({ssid, rssi, WiFi.encryptionType(i) == WIFI_AUTH_OPEN, now});
    } else {
      // Fresh sighting replaces the old reading; several BSSIDs keep the best.
      it->rssi = it->seenMs == now ? std::max(it->rssi, rssi) : rssi;
      it->open = WiFi.encryptionType(i) == WIFI_AUTH_OPEN;
      it->seenMs = now;
    }
  }
  g_networks.erase(std::remove_if(g_networks.begin(), g_networks.end(),
                                  [&](const Net& n) { return now - n.seenMs > kNetworkStaleMs; }),
                   g_networks.end());
  std::sort(g_networks.begin(), g_networks.end(), [](const Net& a, const Net& b) { return a.rssi > b.rssi; });
  if (g_networks.size() > kMaxNetworks) {
    g_networks.resize(kMaxNetworks);
  }
  size_t listed = g_networks.size();
  xSemaphoreGive(g_scanMu);
  WiFi.scanDelete();
  Serial.printf("[wifi] scan: %d access points, %u networks listed\n", found, static_cast<unsigned>(listed));
}

bool startScan(const ScanTiming& timing) {
  if (g_scanning) {
    return true;
  }
  WiFi.scanDelete();  // Frees old results, clears the core's DONE bit.
  wifi_scan_config_t config = {};
  config.show_hidden = false;
  config.scan_type = WIFI_SCAN_TYPE_ACTIVE;
  config.scan_time.active.min = timing.minMs;
  config.scan_time.active.max = timing.maxMs;
  g_scanDone = false;
  esp_err_t err = esp_wifi_scan_start(&config, false);
  if (err != ESP_OK) {
    // e.g. ESP_ERR_WIFI_STATE while the station is connecting.
    Serial.printf("[wifi] scan not started: %s\n", esp_err_to_name(err));
    return false;
  }
  g_scanning = true;
  g_scanStartedMs = millis();
  return true;
}

// Collects a finished scan; the core's SCAN_DONE handler has already copied
// the records, read here through WiFi.SSID(i) etc.
void pollScan() {
  if (g_scanning) {
    if (g_scanDone) {
      int count = WiFi.scanComplete();
      collectScan(count > 0 ? count : 0);
      g_scanning = false;
    } else if (millis() - g_scanStartedMs > kScanGiveUpMs) {
      esp_wifi_scan_stop();
      g_scanning = false;
      Serial.println("[wifi] scan timed out");
    }
    return;
  }
  if (g_scanRequested) {
    g_scanRequested = false;
    startScan(kRescan);
  }
}

// Blocking scan; only used while no setup client can be connected.
void scanNow() {
  if (!startScan(kBootScan)) {
    return;
  }
  while (g_scanning) {
    pollScan();
    delay(20);
  }
}

void startAp() {
  if (g_apActive) {
    return;
  }
  WiFi.mode(WIFI_AP_STA);  // STA stays available for scans and retries.
  WiFi.softAPConfig(kApIp, kApIp, kApMask);
  WiFi.softAP(g_apSsid.c_str());
  g_dns.setErrorReplyCode(DNSReplyCode::NoError);
  g_dns.start(53, "*", kApIp);
  g_apActive = true;
  Serial.printf("[wifi] setup AP \"%s\" at http://%s/\n", g_apSsid.c_str(), kApIp.toString().c_str());
}

void stopAp() {
  if (!g_apActive) {
    return;
  }
  g_dns.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  g_apActive = false;
  Serial.println("[wifi] setup AP stopped");
}

void beginSta() {
  const Settings& s = settings::current();
  WiFi.begin(s.ssid.c_str(), s.pass.length() ? s.pass.c_str() : nullptr);
}

void startMdns() {
  MDNS.end();
  if (MDNS.begin(kHostname)) {
    MDNS.addService("http", "tcp", 80);
  } else {
    Serial.println("[wifi] mDNS failed to start");
  }
}

void checkResetButton() {
  if (digitalRead(PC1_RESET_BUTTON_PIN) == LOW) {
    if (!g_buttonDownMs) {
      g_buttonDownMs = millis();
    } else if (millis() - g_buttonDownMs > kResetHoldMs) {
      factoryReset("BOOT held");
    }
  } else {
    g_buttonDownMs = 0;
  }
}

}  // namespace

void begin() {
  g_scanMu = xSemaphoreCreateMutex();
  // Runs after the core's own SCAN_DONE handler has stored the results.
  WiFi.onEvent([](arduino_event_id_t, arduino_event_info_t) { g_scanDone = true; },
               ARDUINO_EVENT_WIFI_SCAN_DONE);
  pinMode(PC1_RESET_BUTTON_PIN, INPUT_PULLUP);
  String suffix = String(relay::deviceId()).substring(4);  // "pc1-xxxxxx"
  suffix.toUpperCase();
  g_apSsid = "PaperConsole-" + suffix;

  WiFi.persistent(false);  // Credentials live in our own NVS namespace.
  WiFi.setHostname(kHostname);  // DHCP name, so routers list it as paper-console.
  WiFi.setSleep(false);         // Lower latency for pushed print jobs.

  if (!settings::wifiConfigured()) {
    Serial.println("[wifi] no network configured");
    WiFi.mode(WIFI_STA);
    scanNow();  // Before the AP exists: nobody to disconnect.
    startAp();
    return;
  }
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  beginSta();
  g_disconnectedSinceMs = millis();
  Serial.printf("[wifi] connecting to \"%s\"\n", settings::current().ssid.c_str());
}

void loop() {
  if (g_apActive) {
    g_dns.processNextRequest();
  }
  checkResetButton();
  pollScan();
  if (g_restartAtMs && static_cast<int32_t>(millis() - g_restartAtMs) >= 0) {
    ESP.restart();
  }
  if (!settings::wifiConfigured()) {
    return;
  }

  uint32_t now = millis();
  bool connected = WiFi.status() == WL_CONNECTED;
  if (connected) {
    if (!g_wasConnected) {
      g_wasConnected = true;
      g_connectedSinceMs = now;
      WiFi.setAutoReconnect(true);
      Serial.printf("[wifi] connected, IP %s (http://%s.local/)\n", WiFi.localIP().toString().c_str(),
                    kHostname);
      startMdns();
      uplink::requestHeartbeat();
    }
    // Leave the fallback AP once the network is stable and nobody uses it.
    if (g_apActive && now - g_connectedSinceMs > 10000 && WiFi.softAPgetStationNum() == 0) {
      stopAp();
    }
    return;
  }

  if (g_wasConnected) {
    g_wasConnected = false;
    g_disconnectedSinceMs = now;
    Serial.println("[wifi] disconnected");
  }
  if (!g_apActive && !g_scanning && now - g_disconnectedSinceMs > kFallbackAfterMs) {
    // STA reconnect scans hop channels and would drop setup clients; retry
    // manually instead while the AP is up.
    WiFi.setAutoReconnect(false);
    WiFi.disconnect(false);
    scanNow();  // Fresh list for the setup page, before anyone can join.
    startAp();
    g_lastStaRetryMs = now;
  }
  if (g_apActive && !g_scanning && now - g_lastStaRetryMs > kApRetryEveryMs &&
      WiFi.softAPgetStationNum() == 0) {
    g_lastStaRetryMs = now;
    Serial.println("[wifi] retrying network");
    beginSta();
  }
}

void requestScan() { g_scanRequested = true; }

String scanJson() {
  JsonDocument doc;
  doc["scanning"] = g_scanning || g_scanRequested;
  JsonArray arr = doc["networks"].to<JsonArray>();
  xSemaphoreTake(g_scanMu, portMAX_DELAY);
  for (const Net& n : g_networks) {
    JsonObject o = arr.add<JsonObject>();
    o["ssid"] = n.ssid;
    o["rssi"] = n.rssi;
    o["open"] = n.open;
  }
  xSemaphoreGive(g_scanMu);
  String body;
  serializeJson(doc, body);
  return body;
}

bool apActive() { return g_apActive; }
String apSsid() { return g_apSsid; }
String apIp() { return kApIp.toString(); }
String staIp() { return WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : String(); }
bool staConnected() { return WiFi.status() == WL_CONNECTED; }

void scheduleRestart(uint32_t delayMs) { g_restartAtMs = millis() + delayMs; }

void factoryReset(const char* reason) {
  Serial.printf("[setup] %s: erasing settings and restarting into setup\n", reason);
  settings::erase();
  delay(200);
  ESP.restart();
  for (;;) {
  }
}

}  // namespace netmgr
