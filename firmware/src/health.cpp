#include "health.h"

#include <esp_timer.h>

#include "display.h"
#include "netmgr.h"
#include "relay.h"
#include "uplink.h"

namespace health {
void toJson(JsonObject out) {
  const auto printer = relay::snapshot();
  const auto link = uplink::state();
  const bool wifi = netmgr::staConnected();
  const char* status = "ready";
  if (!printer.uartReady) status = "error";
  else if (!wifi || !link.ok) status = "disconnected";
  else if (printer.activeJobId.length()) status = "printing";
  else if (printer.lastJobState == "failed") status = "error";

  out["schema_version"] = 1;
  out["device_id"] = relay::deviceId();
  out["boot_id"] = relay::bootId();
  out["firmware"] = PC1_FIRMWARE_VERSION;
  out["uptime_s"] = static_cast<uint64_t>(esp_timer_get_time() / 1000000);
  out["status"] = status;
  out["wifi_connected"] = wifi;
  out["server_state"] = wifi && link.ok ? "connected" : link.message == "starting" ? "unknown" : "disconnected";
  out["server_detail"] = link.message;
  out["server_last_ok_s"] = link.lastOkAgoS;
  out["uart_ready"] = printer.uartReady;
  out["physical_printer"] = "unknown";
  out["active_job_id"] = printer.activeJobId;
  out["active_job_state"] = printer.activeState;
  out["last_job_id"] = printer.lastJobId;
  out["last_job_state"] = printer.lastJobState;
  out["last_job_error"] = printer.lastJobError;
  out["display"] = display::status();  // Additive to schema 1: "ok" or "absent".
}
}  // namespace health
