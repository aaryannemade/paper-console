#include "web.h"

#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_http_server.h>

#include <algorithm>
#include <vector>

#include "channels.h"
#include "inputs.h"
#include "netmgr.h"
#include "portal_html.h"
#include "relay.h"
#include "settings.h"
#include "uplink.h"

namespace web {
namespace {

httpd_handle_t g_server = nullptr;
uint32_t g_lastTestPrintMs = 0;
constexpr size_t kMaxFormBytes = 2048;
constexpr size_t kMaxCreateBytes = 1024;

const char* statusLine(int code) {
  switch (code) {
    case 200: return "200 OK";
    case 201: return "201 Created";
    case 302: return "302 Found";
    case 400: return "400 Bad Request";
    case 401: return "401 Unauthorized";
    case 403: return "403 Forbidden";
    case 404: return "404 Not Found";
    case 409: return "409 Conflict";
    case 413: return "413 Payload Too Large";
    case 429: return "429 Too Many Requests";
    case 503: return "503 Service Unavailable";
    default: return "500 Internal Server Error";
  }
}

esp_err_t sendJson(httpd_req_t* req, int code, const String& body) {
  httpd_resp_set_status(req, statusLine(code));
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, body.c_str(), body.length());
}

esp_err_t sendMessage(httpd_req_t* req, int code, bool ok, const String& message) {
  JsonDocument doc;
  doc["ok"] = ok;
  doc["message"] = message;
  String body;
  serializeJson(doc, body);
  return sendJson(req, code, body);
}

String header(httpd_req_t* req, const char* name, size_t maxLen = 256) {
  size_t len = httpd_req_get_hdr_value_len(req, name);
  if (len == 0 || len > maxLen) {
    return String();
  }
  std::vector<char> buf(len + 1);
  if (httpd_req_get_hdr_value_str(req, name, buf.data(), buf.size()) != ESP_OK) {
    return String();
  }
  return String(buf.data());
}

// Reads the whole body (up to maxLen) into a String. Returns false on error.
bool readBody(httpd_req_t* req, size_t maxLen, String& out) {
  if (req->content_len > maxLen) {
    return false;
  }
  std::vector<char> buf(req->content_len + 1, 0);
  size_t got = 0;
  int timeouts = 0;
  while (got < req->content_len) {
    int r = httpd_req_recv(req, buf.data() + got, req->content_len - got);
    if (r == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts < 3) {
      continue;
    }
    if (r <= 0) {
      return false;
    }
    got += r;
  }
  out = String(buf.data());
  return true;
}

String path(httpd_req_t* req) {
  String uri = req->uri;
  int q = uri.indexOf('?');
  return q >= 0 ? uri.substring(0, q) : uri;
}

bool authorized(httpd_req_t* req) {
  const String& token = settings::current().token;
  if (token.isEmpty()) {
    return false;
  }
  return settings::safeEquals(header(req, "Authorization"), "Bearer " + token);
}

esp_err_t unauthorized(httpd_req_t* req) {
  return sendJson(req, 401, "{\"error\":\"unauthorized\"}");
}

// --- URL-encoded form parsing ----------------------------------------------

String urlDecode(const String& in) {
  String out;
  out.reserve(in.length());
  for (size_t i = 0; i < in.length(); ++i) {
    char c = in[i];
    if (c == '+') {
      out += ' ';
    } else if (c == '%' && i + 2 < in.length() && isxdigit(in[i + 1]) && isxdigit(in[i + 2])) {
      char hex[3] = {in[i + 1], in[i + 2], 0};
      out += static_cast<char>(strtol(hex, nullptr, 16));
      i += 2;
    } else {
      out += c;
    }
  }
  return out;
}

String formField(const String& body, const char* name) {
  String key = String(name) + "=";
  size_t pos = 0;
  while (pos <= body.length()) {
    int end = body.indexOf('&', pos);
    if (end < 0) {
      end = body.length();
    }
    if (body.substring(pos, pos + key.length()) == key) {
      return urlDecode(body.substring(pos + key.length(), end));
    }
    pos = end + 1;
  }
  return String();
}

// --- Setup UI --------------------------------------------------------------

esp_err_t handleIndex(httpd_req_t* req) {
  httpd_resp_set_type(req, "text/html; charset=utf-8");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, kPortalHtml, sizeof(kPortalHtml) - 1);
}

esp_err_t handleStatus(httpd_req_t* req) {
  const Settings& s = settings::current();
  relay::Snapshot snap = relay::snapshot();
  uplink::LinkState link = uplink::state();
  JsonDocument doc;
  doc["device_id"] = relay::deviceId();
  doc["firmware"] = PC1_FIRMWARE_VERSION;
  doc["ssid"] = s.ssid;
  doc["wifi_connected"] = netmgr::staConnected();
  doc["ip"] = netmgr::staIp();
  doc["ap_active"] = netmgr::apActive();
  doc["ap_ssid"] = netmgr::apSsid();
  doc["ap_ip"] = netmgr::apIp();
  doc["server"] = s.server;
  doc["token_set"] = s.token.length() > 0;
  doc["link"]["ok"] = link.ok;
  doc["link"]["message"] = link.message;
  doc["link"]["last_ok_s"] = link.lastOkAgoS;
  doc["printer"]["state"] = snap.printerState;
  doc["printer"]["active_job"] = snap.activeJobId;
  doc["printer"]["last_job"] = snap.lastJobSummary;
  int position = inputs::currentPosition();
  doc["channel"]["position"] = position;
  doc["channel"]["name"] = channels::name(position);
  channels::toJson(doc["channels"].to<JsonObject>());
  doc["uptime_s"] = millis() / 1000;
  String body;
  serializeJson(doc, body);
  return sendJson(req, 200, body);
}

// Cached networks; "?refresh=1" starts a short background rescan (never a
// blocking scan here: it would stall this server and drop setup clients).
esp_err_t handleScan(httpd_req_t* req) {
  char query[32] = {0};
  char refresh[4] = {0};
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) == ESP_OK &&
      httpd_query_key_value(query, "refresh", refresh, sizeof(refresh)) == ESP_OK &&
      strcmp(refresh, "1") == 0) {
    netmgr::requestScan();
  }
  return sendJson(req, 200, netmgr::scanJson());
}

esp_err_t handleSave(httpd_req_t* req) {
  String body;
  if (!readBody(req, kMaxFormBytes, body)) {
    return sendMessage(req, 400, false, "Request too large or unreadable.");
  }
  // Settings are open to anyone on the network (by design). The relay token
  // only authenticates server <-> relay requests: it is write-only here and
  // never shown or required to change settings.
  const Settings& old = settings::current();
  Settings next = old;
  next.ssid = formField(body, "ssid");
  next.ssid.trim();
  if (next.ssid.isEmpty() || next.ssid.length() > 32) {
    return sendMessage(req, 400, false, "Enter a WiFi network name (up to 32 characters).");
  }
  String pass = formField(body, "pass");
  if (pass.length()) {
    if (pass.length() < 8 || pass.length() > 63) {
      return sendMessage(req, 400, false, "WiFi password must be 8-63 characters.");
    }
    next.pass = pass;
  } else if (next.ssid != old.ssid) {
    next.pass = "";  // New network without a password: open network.
  }
  if (!settings::normalizeServer(formField(body, "server"), next.server)) {
    return sendMessage(req, 400, false, "Server URL must look like http://192.168.1.20:8000");
  }
  String token = formField(body, "token");
  if (token.length()) {
    if (!settings::validToken(token)) {
      return sendMessage(req, 400, false, "Relay token must be 16-128 characters without spaces.");
    }
    next.token = token;
  }
  if (next.token.isEmpty()) {
    return sendMessage(req, 400, false, "Enter the relay token (PC1_RELAY_TOKEN from the server).");
  }
  if (!settings::save(next)) {
    return sendMessage(req, 500, false, "Could not store settings.");
  }
  Serial.printf("[setup] saved: network \"%s\", server %s\n", next.ssid.c_str(), next.server.c_str());
  netmgr::scheduleRestart(1500);
  return sendMessage(req, 200, true,
                     "Saved. The relay restarts and joins \"" + next.ssid +
                         "\". Reconnect to that network and open http://" + netmgr::kHostname +
                         ".local/");
}

esp_err_t handleTestPrint(httpd_req_t* req) {
  if (g_lastTestPrintMs && millis() - g_lastTestPrintMs < 5000) {
    return sendMessage(req, 429, false, "Wait a few seconds between test prints.");
  }
  const Settings& s = settings::current();
  // The container prints receipts rotated 180 degrees for the printer's
  // mounting; print upside-down text in reverse line order to match.
  std::vector<String> lines = {
      "PAPER CONSOLE RELAY TEST",
      String("Device:  ") + relay::deviceId(),
      String("Firmware: ") + PC1_FIRMWARE_VERSION,
      String("WiFi:    ") + (netmgr::staConnected() ? s.ssid : String("(setup mode)")),
      String("IP:      ") + (netmgr::staConnected() ? netmgr::staIp() : netmgr::apIp()),
      String("Server:  ") + (s.server.length() ? s.server : String("(not set)")),
      "--------------------------------",
  };
  std::vector<uint8_t> out = {0x1B, 0x40, 0x1B, 0x7B, 0x01};  // ESC @, ESC { 1 (upside-down)
  for (auto it = lines.rbegin(); it != lines.rend(); ++it) {
    out.insert(out.end(), it->c_str(), it->c_str() + it->length());
    out.push_back('\n');
  }
  const uint8_t tail[] = {0x1B, 0x7B, 0x00, 0x1B, 0x4A, 120};  // ESC { 0, ESC J 120
  out.insert(out.end(), tail, tail + sizeof(tail));
  int code = relay::localPrint(out.data(), out.size());
  if (code == 200) {
    g_lastTestPrintMs = millis();
    return sendMessage(req, 200, true, "Test page sent to the printer.");
  }
  return sendMessage(req, code, false, code == 409 ? "Printer is busy." : "Could not print.");
}

esp_err_t handleInputs(httpd_req_t* req) { return sendJson(req, 200, inputs::diagnosticsJson()); }

// --- Relay protocol --------------------------------------------------------

// Splits "/jobs/{id}[/rest]".
bool parseJobPath(const String& p, String& id, String& rest) {
  if (!p.startsWith("/jobs/")) {
    return false;
  }
  String tail = p.substring(6);
  int slash = tail.indexOf('/');
  id = slash >= 0 ? tail.substring(0, slash) : tail;
  rest = slash >= 0 ? tail.substring(slash) : String();
  return id.length() > 0 && id.length() <= 64;
}

esp_err_t handleCreateJob(httpd_req_t* req) {
  if (!authorized(req)) {
    return unauthorized(req);
  }
  String body;
  if (!readBody(req, kMaxCreateBytes, body)) {
    return sendJson(req, 400, "{\"error\":\"bad job\"}");
  }
  String out;
  int code = relay::createJob(body.c_str(), body.length(), out);
  return sendJson(req, code, out);
}

esp_err_t handleJobPost(httpd_req_t* req) {
  if (!authorized(req)) {
    return unauthorized(req);
  }
  String id, rest;
  if (!parseJobPath(path(req), id, rest) || rest != "/cancel") {
    return sendJson(req, 404, "{\"error\":\"not found\"}");
  }
  String out;
  int code = relay::cancelJob(id, out);
  return sendJson(req, code, out);
}

esp_err_t handleJobGet(httpd_req_t* req) {
  if (!authorized(req)) {
    return unauthorized(req);
  }
  String id, rest;
  if (!parseJobPath(path(req), id, rest) || rest.length()) {
    return sendJson(req, 404, "{\"error\":\"not found\"}");
  }
  String out;
  int code = relay::getJob(id, out);
  return sendJson(req, code, out);
}

esp_err_t handleChunk(httpd_req_t* req) {
  if (!authorized(req)) {
    return unauthorized(req);
  }
  String id, rest;
  if (!parseJobPath(path(req), id, rest) || !rest.startsWith("/chunks/")) {
    return sendJson(req, 404, "{\"error\":\"not found\"}");
  }
  String indexText = rest.substring(8);
  if (indexText.isEmpty() || indexText.length() > 6) {
    return sendJson(req, 404, "{\"error\":\"not found\"}");
  }
  for (size_t i = 0; i < indexText.length(); ++i) {
    if (!isdigit(indexText[i])) {
      return sendJson(req, 404, "{\"error\":\"not found\"}");
    }
  }
  uint32_t index = indexText.toInt();
  String finalHeader = header(req, "X-PC1-Final-Chunk", 4);
  int finalFlag = finalHeader == "1" ? 1 : (finalHeader == "0" ? 0 : -1);

  String out;
  uint32_t retryMs = 0;
  int pre = relay::precheckChunk(id, index, req->content_len, finalFlag, out, retryMs);
  if (pre == 429) {
    String retry(retryMs);
    httpd_resp_set_hdr(req, "Retry-After-Ms", retry.c_str());
    return sendJson(req, 429, out);
  }
  if (pre != 0) {
    return sendJson(req, pre, out);  // Unread body is discarded by the server.
  }

  size_t len = req->content_len;
  uint8_t* data = relay::allocBuffer(len);
  if (!data) {
    httpd_resp_set_hdr(req, "Retry-After-Ms", "500");
    return sendJson(req, 429, "{\"error\":\"out of memory\"}");
  }
  size_t got = 0;
  int timeouts = 0;
  while (got < len) {
    int r = httpd_req_recv(req, reinterpret_cast<char*>(data) + got, len - got);
    if (r == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts < 3) {
      continue;
    }
    if (r <= 0) {
      free(data);
      return ESP_FAIL;  // Connection broken; the container resends.
    }
    got += r;
  }
  int code = relay::commitChunk(id, index, data, len, finalFlag == 1, out);
  return sendJson(req, code, out);
}

// --- Captive portal / 404 --------------------------------------------------

esp_err_t handleNotFound(httpd_req_t* req, httpd_err_code_t) {
  if (netmgr::apActive()) {
    String host = header(req, "Host", 128);
    String apIp = netmgr::apIp();
    if (host != apIp && host != netmgr::staIp()) {
      static String location;
      location = "http://" + apIp + "/";
      httpd_resp_set_status(req, statusLine(302));
      httpd_resp_set_hdr(req, "Location", location.c_str());
      httpd_resp_set_hdr(req, "Cache-Control", "no-store");
      return httpd_resp_send(req, nullptr, 0);
    }
  }
  return sendJson(req, 404, "{\"error\":\"not found\"}");
}

void route(const char* uri, httpd_method_t method, esp_err_t (*handler)(httpd_req_t*)) {
  httpd_uri_t def = {};
  def.uri = uri;
  def.method = method;
  def.handler = handler;
  httpd_register_uri_handler(g_server, &def);
}

}  // namespace

void begin() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  config.max_uri_handlers = 12;
  config.stack_size = 8192;
  config.uri_match_fn = httpd_uri_match_wildcard;
  config.lru_purge_enable = true;
  config.recv_wait_timeout = 10;
  config.send_wait_timeout = 10;
  if (httpd_start(&g_server, &config) != ESP_OK) {
    Serial.println("[web] failed to start HTTP server");
    return;
  }
  route("/", HTTP_GET, handleIndex);
  route("/ui/status", HTTP_GET, handleStatus);
  route("/ui/scan", HTTP_GET, handleScan);
  route("/ui/save", HTTP_POST, handleSave);
  route("/ui/test-print", HTTP_POST, handleTestPrint);
  route("/ui/inputs", HTTP_GET, handleInputs);
  route("/jobs", HTTP_POST, handleCreateJob);
  route("/jobs/*", HTTP_POST, handleJobPost);
  route("/jobs/*", HTTP_PUT, handleChunk);
  route("/jobs/*", HTTP_GET, handleJobGet);
  httpd_register_err_handler(g_server, HTTPD_404_NOT_FOUND, handleNotFound);
  Serial.println("[web] HTTP server on port 80");
}

}  // namespace web
