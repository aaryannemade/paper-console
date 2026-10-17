// Print jobs (docs/relay-protocol.md) and the printer UART.
//
// HTTP handlers create jobs and queue chunk bodies; a dedicated writer task
// writes each record to the UART unchanged and in order, honouring
// cancellation only between records. All job state is guarded by one mutex so
// heartbeats, status polls and input stay responsive while a job prints.
#pragma once

#include <Arduino.h>

namespace relay {

constexpr uint32_t kMaxChunkBytes = 8192;
constexpr uint32_t kMaxJobBytes = 1024 * 1024;

struct Snapshot {
  const char* printerState;  // "ready" | "busy"
  String activeJobId;        // Empty when idle.
  String lastJobSummary;     // Human-readable, for the device UI.
};

void begin();
const char* deviceId();
const char* bootId();
Snapshot snapshot();

// HTTP-facing operations. Each returns the HTTP status and fills `out` with a
// JSON body.
int createJob(const char* body, size_t len, String& out);
// Checks a chunk before its body is read. Returns 0 to continue, otherwise the
// HTTP status to answer with (and `retryAfterMs` for 429).
int precheckChunk(const String& jobId, uint32_t index, size_t contentLen, int finalFlag,
                  String& out, uint32_t& retryAfterMs);
// Takes ownership of `data` (heap/PSRAM buffer from relay::allocBuffer).
int commitChunk(const String& jobId, uint32_t index, uint8_t* data, size_t len, bool finalFlag,
                String& out);
int cancelJob(const String& jobId, String& out);
int getJob(const String& jobId, String& out);

// A device-originated print (setup test page). Returns HTTP-style status:
// 200 queued, 409 busy, 500 out of memory.
int localPrint(const uint8_t* data, size_t len);

uint8_t* allocBuffer(size_t len);

}  // namespace relay
