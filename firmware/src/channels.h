// Channel display names from the server (docs/relay-protocol.md).
//
// Every heartbeat reply carries "channels_hash", a hash of the eight names.
// When it differs from the hash of the copy held here, the uplink task fetches
// GET /api/device/channels once and replaces the copy. Names live in RAM only:
// after a restart the first heartbeat fetches them again. An empty name means
// the channel is unnamed (show just its number). Intended for a display.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

namespace channels {

constexpr int kCount = 8;

void begin();
// Called by the uplink task after each accepted heartbeat. Blocks for one
// request when the hash changed; a failed fetch is retried next heartbeat.
void sync(const String& serverHash);
// Name for position 1-8 ("" if unnamed or not fetched yet).
String name(int position);
// Hash of the names held here ("" until the first successful fetch).
String hash();
// {"hash": ..., "names": ["", "Work update", ...]} for the status page.
void toJson(JsonObject out);

}  // namespace channels
