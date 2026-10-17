// WiFi station + setup access point with captive-portal DNS.
//
// Unconfigured: open AP "PaperConsole-XXXXXX" at 192.168.4.1 with every DNS
// name pointing at the setup page. Configured: joins the network and answers
// at http://paper-console.local/; if it cannot connect for 30 s the setup AP
// comes up as well (and STA retries pause while someone is connected to it).
// Holding BOOT for 5 s (or the encoder reset sequence) erases all settings.
//
// WiFi scans: the single radio leaves the AP channel while scanning, which
// drops setup clients. So networks are scanned before the AP starts and
// cached; a rescan runs in the background with a short per-channel dwell.
#pragma once

#include <Arduino.h>

namespace netmgr {

constexpr const char* kHostname = "paper-console";

void begin();
void loop();
// Requests a background rescan (performed from loop()).
void requestScan();
// Cached scan results as a JSON body: {"scanning":bool,"networks":[...]}.
String scanJson();
bool apActive();
String apSsid();
String apIp();
String staIp();
bool staConnected();
void scheduleRestart(uint32_t delayMs);
// Erases all saved settings (WiFi, server, token) and restarts into setup.
// Used by BOOT held 5 s and by the encoder reset sequence (inputs.cpp).
[[noreturn]] void factoryReset(const char* reason);

}  // namespace netmgr
