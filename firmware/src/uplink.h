// Device -> container requests: heartbeats and job status reports, sent from
// one background task so the HTTP server and UART writer never wait on them.
#pragma once

#include <Arduino.h>

namespace uplink {

struct StatusReport {
  char jobId[65];
  char state[12];
  uint32_t recordsWritten;
  uint32_t bytesWritten;
  char error[160];
};

struct LinkState {
  bool ok;
  String message;     // e.g. "connected", "token rejected (401)".
  int32_t lastOkAgoS; // -1 if never.
};

void begin();
void queueStatus(const StatusReport& report);
void requestHeartbeat();
LinkState state();
// True once a heartbeat has been accepted since the last failure.
bool linkOk();
// Increments every time the link goes from failing to OK (to resync state).
uint32_t linkGeneration();

// POSTs JSON to the configured server with the bearer token. Returns the HTTP
// status, or a negative HTTPClient error code. Safe to call from any task.
int postJson(const char* path, const String& body, String* response, uint16_t timeoutMs = 5000);

}  // namespace uplink
