// Paper Console ESP32-S3 relay (XIAO ESP32S3).
//
// Receives rendered receipts from the Paper Console container over WiFi and
// writes them to the CSN-A2 printer UART; see docs/relay-protocol.md.
// Setup: join the "PaperConsole-XXXXXX" WiFi network and fill in the page.
#include <Arduino.h>

#include "channels.h"
#include "events.h"
#include "inputs.h"
#include "netmgr.h"
#include "relay.h"
#include "settings.h"
#include "uplink.h"
#include "web.h"

void setup() {
  // Printer first: resets it after any boot-time noise on the TX pin.
  settings::load();
  relay::begin();

  Serial.begin(115200);
  // Bound USB log writes when the cable is plugged into a host that is not
  // reading. Must not be 0: HWCDC::write counts its retries down from this
  // value as unsigned, so 0 wraps around and blocks the caller indefinitely.
  Serial.setTxTimeoutMs(10);
  delay(200);
  Serial.printf("\n[boot] Paper Console relay %s, device %s, boot %s, PSRAM %u KB, reset reason %d\n",
                PC1_FIRMWARE_VERSION, relay::deviceId(), relay::bootId(),
                static_cast<unsigned>(ESP.getPsramSize() / 1024), static_cast<int>(esp_reset_reason()));

  channels::begin();
  events::begin();
  inputs::begin();
  netmgr::begin();
  uplink::begin();
  web::begin();
}

void loop() {
  netmgr::loop();
  inputs::loop();
  delay(5);
}
