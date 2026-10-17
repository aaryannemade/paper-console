// Device HTTP server on port 80 (ESP-IDF esp_http_server, its own task).
//
//   GET  /                 setup + status page
//   GET  /ui/status        status JSON for the page (no secrets)
//   GET  /ui/scan          cached WiFi networks; ?refresh=1 rescans in the background
//   POST /ui/save          store settings and restart
//   POST /ui/test-print    local printer test page
//   POST /jobs, PUT /jobs/{id}/chunks/{n}, POST /jobs/{id}/cancel,
//   GET  /jobs/{id}        relay protocol (bearer token)
//
// In setup-AP mode, unknown URLs redirect to the setup page (captive portal).
#pragma once

namespace web {
void begin();
}
