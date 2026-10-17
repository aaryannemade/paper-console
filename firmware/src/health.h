#pragma once

#include <ArduinoJson.h>

namespace health {
// Cached local state only: never queries the network or writes printer commands.
void toJson(JsonObject out);
}
