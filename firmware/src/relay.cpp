#include "relay.h"

#include <ArduinoJson.h>
#include <esp_heap_caps.h>
#include <esp_system.h>

#include <deque>

#include "uplink.h"

namespace relay {
namespace {

enum class State : uint8_t { Receiving, Printing, Completed, Cancelled, Failed };

const char* stateName(State s) {
  switch (s) {
    case State::Receiving: return "receiving";
    case State::Printing: return "printing";
    case State::Completed: return "completed";
    case State::Cancelled: return "cancelled";
    case State::Failed: return "failed";
  }
  return "failed";
}

bool isFinal(State s) { return s == State::Completed || s == State::Cancelled || s == State::Failed; }

struct Chunk {
  uint8_t* data = nullptr;
  size_t len = 0;
};

struct Job {
  bool used = false;
  bool local = false;  // Device-originated; never reported to the container.
  char id[65] = {0};
  char kind[16] = {0};
  uint32_t seq = 0;
  uint32_t expectedChunks = 0, expectedRecords = 0, expectedBytes = 0;
  uint32_t receivedChunks = 0, receivedRecords = 0, receivedBytes = 0;
  uint32_t recordsWritten = 0, bytesWritten = 0;
  uint32_t createdMs = 0, lastDataMs = 0, finishedMs = 0;
  State state = State::Receiving;
  bool cancel = false;
  char error[160] = {0};
};

// Keep recently finished jobs so late polls and retried creates get answers.
constexpr size_t kHistory = 16;
// A job that stops receiving data (container died mid-push) must not block
// the device forever.
constexpr uint32_t kStallTimeoutMs = 60000;
constexpr size_t kUartWriteSlice = 128;

Job g_jobs[kHistory];
Job* g_active = nullptr;
std::deque<Chunk> g_queue;  // Chunks of the active job, not yet written.
size_t g_queuedBytes = 0;
size_t g_bufferLimit = 32 * 1024;
uint32_t g_seq = 0;
uint32_t g_localSeq = 0;
SemaphoreHandle_t g_mu = nullptr;
TaskHandle_t g_writer = nullptr;
char g_bootId[13] = {0};
char g_deviceId[16] = {0};
String g_lastSummary = "No jobs yet";
String g_lastJobId, g_lastJobState, g_lastJobError;
String g_pendingLog;

HardwareSerial& g_uart = Serial1;

struct Lock {
  Lock() { xSemaphoreTake(g_mu, portMAX_DELAY); }
  ~Lock() { xSemaphoreGive(g_mu); }
};

void wakeWriter() {
  if (g_writer) {
    xTaskNotifyGive(g_writer);
  }
}

bool validJobId(const char* id) {
  size_t n = strlen(id);
  if (n < 1 || n > 64) {
    return false;
  }
  for (size_t i = 0; i < n; ++i) {
    char c = id[i];
    if (!(isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_')) {
      return false;
    }
  }
  return true;
}

Job* findLocked(const String& id) {
  for (auto& job : g_jobs) {
    if (job.used && id.equals(job.id)) {
      return &job;
    }
  }
  return nullptr;
}

Job* allocSlotLocked() {
  Job* oldest = nullptr;
  for (auto& job : g_jobs) {
    if (!job.used) {
      return &job;
    }
    if (&job == g_active || !isFinal(job.state)) {
      continue;
    }
    if (!oldest || job.seq < oldest->seq) {
      oldest = &job;
    }
  }
  return oldest;
}

void statusJson(const Job& job, String& out) {
  JsonDocument doc;
  doc["job_id"] = job.id;
  doc["state"] = stateName(job.state);
  doc["boot_id"] = g_bootId;
  doc["records_written"] = job.recordsWritten;
  doc["bytes_written"] = job.bytesWritten;
  if (job.error[0]) {
    doc["error"] = job.error;
  } else {
    doc["error"] = nullptr;
  }
  out.clear();
  serializeJson(doc, out);
}

void errorJson(String& out, const char* message) {
  JsonDocument doc;
  doc["error"] = message;
  out.clear();
  serializeJson(doc, out);
}

void reportLocked(const Job& job) {
  if (job.local) {
    return;
  }
  uplink::StatusReport report;
  strlcpy(report.jobId, job.id, sizeof(report.jobId));
  strlcpy(report.state, stateName(job.state), sizeof(report.state));
  report.recordsWritten = job.recordsWritten;
  report.bytesWritten = job.bytesWritten;
  strlcpy(report.error, job.error, sizeof(report.error));
  uplink::queueStatus(report);
}

void dropQueueLocked() {
  for (auto& chunk : g_queue) {
    free(chunk.data);
  }
  g_queue.clear();
  g_queuedBytes = 0;
}

void finishLocked(Job& job, State state, const char* error) {
  if (isFinal(job.state)) {
    return;
  }
  job.state = state;
  strlcpy(job.error, error ? error : "", sizeof(job.error));
  job.finishedMs = millis();
  g_lastJobId = job.id;
  g_lastJobState = stateName(state);
  g_lastJobError = job.error;
  if (&job == g_active) {
    dropQueueLocked();
  }
  float seconds = (job.finishedMs - job.createdMs) / 1000.0f;
  g_lastSummary = String(job.kind) + " " + String(job.id).substring(0, 8) + " " + stateName(state) +
                  ": " + job.recordsWritten + " records, " + job.bytesWritten + " bytes, " +
                  String(seconds, 1) + " s" + (job.error[0] ? String(" (") + job.error + ")" : "");
  g_pendingLog = g_lastSummary;  // Printed by the writer outside the lock.
  reportLocked(job);
  uplink::requestHeartbeat();  // printer.state is back to ready.
}

// Validates [2-byte big-endian length][record] framing; returns the record
// count or -1.
int countRecords(const uint8_t* data, size_t len) {
  size_t off = 0;
  int count = 0;
  while (off < len) {
    if (off + 2 > len) {
      return -1;
    }
    size_t n = (static_cast<size_t>(data[off]) << 8) | data[off + 1];
    off += 2;
    if (n == 0 || off + n > len) {
      return -1;
    }
    off += n;
    ++count;
  }
  return count;
}

bool writeRecord(const uint8_t* data, size_t len) {
  size_t done = 0;
  while (done < len) {
    size_t slice = min(kUartWriteSlice, len - done);
    size_t written = g_uart.write(data + done, slice);
    if (written != slice) {
      return false;
    }
    done += written;
  }
  g_uart.flush();  // Wait until the record has left the UART.
  return true;
}

void writerTask(void*) {
  Chunk current;
  size_t offset = 0;
  for (;;) {
    const uint8_t* record = nullptr;
    size_t recordLen = 0;
    Job* job = nullptr;
    String logLine;
    {
      Lock lock;
      logLine = g_pendingLog;
      g_pendingLog = String();
    }
    if (logLine.length()) {
      Serial.printf("[job] %s\n", logLine.c_str());
    }
    {
      Lock lock;
      Job* active = g_active;
      if (active && !isFinal(active->state)) {
        if (active->cancel) {
          free(current.data);
          current = Chunk();
          finishLocked(*active, State::Cancelled, nullptr);
        } else {
          if (!current.data && !g_queue.empty()) {
            current = g_queue.front();
            g_queue.pop_front();
            g_queuedBytes -= current.len;
            offset = 0;
          }
          if (current.data) {
            recordLen = (static_cast<size_t>(current.data[offset]) << 8) | current.data[offset + 1];
            record = current.data + offset + 2;
            offset += 2 + recordLen;
            job = active;
            if (job->state == State::Receiving) {
              job->state = State::Printing;
            }
          } else if (active->receivedChunks == active->expectedChunks) {
            finishLocked(*active, State::Completed, nullptr);
          } else if (millis() - active->lastDataMs > kStallTimeoutMs) {
            finishLocked(*active, State::Failed, "timed out waiting for job data");
          }
        }
      }
    }

    if (!record) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(50));
      continue;
    }

    bool ok = writeRecord(record, recordLen);

    Lock lock;
    if (!isFinal(job->state)) {
      if (ok) {
        job->recordsWritten += 1;
        job->bytesWritten += recordLen;
        if (job->recordsWritten == 1) {
          reportLocked(*job);  // "printing" once writing starts.
        }
      } else {
        String error = String("UART write failed after ") + job->bytesWritten + " bytes";
        finishLocked(*job, State::Failed, error.c_str());
      }
    }
    // Release the chunk once consumed, or immediately if its job ended, so a
    // leftover never leaks into the next job.
    if (current.data && (offset >= current.len || isFinal(job->state))) {
      free(current.data);
      current = Chunk();
    }
  }
}

void initPrinter() {
  g_uart.setRxBufferSize(256);
  g_uart.begin(PC1_PRINTER_BAUD, SERIAL_8N1, PC1_PRINTER_RX_PIN, PC1_PRINTER_TX_PIN);
  // readme.md "Printer UART": flush any partial command (including
  // boot-time noise on the TX pin), reset, then select plain ASCII mode.
  const uint8_t flush[] = {0, 0, 0, 0, 0};
  g_uart.write(flush, sizeof(flush));
  g_uart.flush();
  delay(100);
  const uint8_t reset[] = {0x1B, 0x40};
  g_uart.write(reset, sizeof(reset));
  g_uart.flush();
  delay(300);
  const uint8_t setup[] = {0x1C, 0x2E, 0x1B, 0x52, 0x00, 0x1B, 0x74, 0x00};
  g_uart.write(setup, sizeof(setup));
  g_uart.flush();
  while (g_uart.available()) {
    g_uart.read();
  }
}

}  // namespace

uint8_t* allocBuffer(size_t len) {
  if (len == 0) {
    len = 1;
  }
  void* p = heap_caps_malloc(len, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!p) {
    p = malloc(len);
  }
  return static_cast<uint8_t*>(p);
}

void begin() {
  g_mu = xSemaphoreCreateMutex();
  uint64_t mac = ESP.getEfuseMac();
  snprintf(g_deviceId, sizeof(g_deviceId), "pc1-%02x%02x%02x", static_cast<uint8_t>(mac >> 24),
           static_cast<uint8_t>(mac >> 32), static_cast<uint8_t>(mac >> 40));
  snprintf(g_bootId, sizeof(g_bootId), "%08x%04x", esp_random(), esp_random() & 0xFFFF);
  g_bufferLimit = psramFound() ? 256 * 1024 : 32 * 1024;
  initPrinter();
  xTaskCreatePinnedToCore(writerTask, "uart-writer", 4096, nullptr, 3, &g_writer, 1);
}

const char* deviceId() { return g_deviceId; }
const char* bootId() { return g_bootId; }

Snapshot snapshot() {
  Lock lock;
  Snapshot snap;
  bool busy = g_active && !isFinal(g_active->state);
  snap.uartReady = g_writer != nullptr && static_cast<bool>(g_uart);
  snap.printerState = !snap.uartReady ? "error" : busy ? "busy" : "ready";
  if (busy) {
    snap.activeJobId = g_active->id;
    snap.activeState = stateName(g_active->state);
  }
  snap.lastJobSummary = g_lastSummary;
  snap.lastJobId = g_lastJobId;
  snap.lastJobState = g_lastJobState;
  snap.lastJobError = g_lastJobError;
  return snap;
}

int createJob(const char* body, size_t len, String& out) {
  JsonDocument doc;
  if (deserializeJson(doc, body, len) || !doc.is<JsonObject>()) {
    errorJson(out, "bad job");
    return 400;
  }
  const char* id = doc["job_id"].is<const char*>() ? doc["job_id"].as<const char*>() : "";
  const char* kind = doc["kind"].is<const char*>() ? doc["kind"].as<const char*>() : "receipt";
  if (!validJobId(id) || !doc["chunks"].is<long>() || doc["chunks"].as<long>() < 1 ||
      doc["bytes"].as<long>() < 0 || doc["records"].as<long>() < 0) {
    errorJson(out, "bad job");
    return 400;
  }
  long chunks = doc["chunks"].as<long>();
  long total = doc["bytes"] | 0L;
  long records = doc["records"] | 0L;

  Lock lock;
  if (Job* existing = findLocked(id)) {
    statusJson(*existing, out);  // Retried create: never a second job.
    return 200;
  }
  if (g_active && !isFinal(g_active->state)) {
    JsonDocument busy;
    busy["error"] = "busy";
    busy["active_job_id"] = g_active->local ? "" : g_active->id;
    out.clear();
    serializeJson(busy, out);
    return 409;
  }
  if (total > static_cast<long>(kMaxJobBytes)) {
    errorJson(out, "job too large");
    return 413;
  }
  Job* job = allocSlotLocked();
  if (!job) {
    errorJson(out, "no job slot");
    return 503;
  }
  *job = Job();
  job->used = true;
  strlcpy(job->id, id, sizeof(job->id));
  strlcpy(job->kind, kind, sizeof(job->kind));
  job->seq = ++g_seq;
  job->expectedChunks = chunks;
  job->expectedBytes = total;
  job->expectedRecords = records;
  job->createdMs = job->lastDataMs = millis();
  dropQueueLocked();
  g_active = job;
  statusJson(*job, out);
  uplink::requestHeartbeat();  // printer.state is now busy.
  wakeWriter();
  return 201;
}

int precheckChunk(const String& jobId, uint32_t index, size_t contentLen, int finalFlag, String& out,
                  uint32_t& retryAfterMs) {
  Lock lock;
  Job* job = findLocked(jobId);
  if (!job) {
    errorJson(out, "unknown job");
    return 404;
  }
  if (index < job->receivedChunks) {
    out = String("{\"received\":") + index + ",\"duplicate\":true}";
    return 200;
  }
  if (index != job->receivedChunks) {
    out = String("{\"error\":\"out of order\",\"expected\":") + job->receivedChunks + "}";
    return 409;
  }
  if (isFinal(job->state)) {
    errorJson(out, (String("job ") + stateName(job->state)).c_str());
    return 409;
  }
  if (index >= job->expectedChunks) {
    errorJson(out, "chunk index beyond job");
    return 400;
  }
  if (contentLen > kMaxChunkBytes) {
    errorJson(out, "chunk too large");
    return 413;
  }
  bool expectedFinal = index == job->expectedChunks - 1;
  if (finalFlag != (expectedFinal ? 1 : 0)) {
    errorJson(out, "final chunk flag mismatch");
    return 400;
  }
  if (!g_queue.empty() && g_queuedBytes + contentLen > g_bufferLimit) {
    retryAfterMs = 250;
    errorJson(out, "buffer full");
    return 429;
  }
  return 0;
}

int commitChunk(const String& jobId, uint32_t index, uint8_t* data, size_t len, bool finalFlag, String& out) {
  int records = countRecords(data, len);
  uint32_t retry = 0;
  int pre = precheckChunk(jobId, index, len, finalFlag ? 1 : 0, out, retry);
  if (pre != 0) {
    free(data);
    return pre;
  }
  if (records < 0) {
    free(data);
    errorJson(out, "malformed record framing");
    return 400;
  }
  Lock lock;
  Job* job = findLocked(jobId);
  if (!job || isFinal(job->state) || index != job->receivedChunks) {
    free(data);  // Changed between the checks (e.g. cancelled).
    errorJson(out, "job changed");
    return 409;
  }
  if (finalFlag && (job->receivedBytes + len != job->expectedBytes ||
                    job->receivedRecords + records != job->expectedRecords)) {
    free(data);
    errorJson(out, "job size or record count mismatch");
    return 400;
  }
  Chunk chunk;
  chunk.data = data;
  chunk.len = len;
  g_queue.push_back(chunk);
  g_queuedBytes += len;
  job->receivedChunks += 1;
  job->receivedRecords += records;
  job->receivedBytes += len;
  job->lastDataMs = millis();
  out = String("{\"received\":") + index + "}";
  wakeWriter();
  return 200;
}

int cancelJob(const String& jobId, String& out) {
  Lock lock;
  Job* job = findLocked(jobId);
  if (!job) {
    errorJson(out, "unknown job");
    return 404;
  }
  if (!isFinal(job->state)) {
    job->cancel = true;
    wakeWriter();
  }
  statusJson(*job, out);
  return 200;
}

int getJob(const String& jobId, String& out) {
  Lock lock;
  Job* job = findLocked(jobId);
  if (!job) {
    errorJson(out, "unknown job");
    return 404;
  }
  statusJson(*job, out);
  return 200;
}

int localPrint(const uint8_t* data, size_t len) {
  if (len == 0 || len > 0xFFFF) {
    return 400;
  }
  uint8_t* framed = allocBuffer(len + 2);
  if (!framed) {
    return 500;
  }
  framed[0] = static_cast<uint8_t>(len >> 8);
  framed[1] = static_cast<uint8_t>(len & 0xFF);
  memcpy(framed + 2, data, len);

  Lock lock;
  if (g_active && !isFinal(g_active->state)) {
    free(framed);
    return 409;
  }
  Job* job = allocSlotLocked();
  if (!job) {
    free(framed);
    return 409;
  }
  *job = Job();
  job->used = true;
  job->local = true;
  snprintf(job->id, sizeof(job->id), "local-%lu", static_cast<unsigned long>(++g_localSeq));
  strlcpy(job->kind, "test", sizeof(job->kind));
  job->seq = ++g_seq;
  job->expectedChunks = job->receivedChunks = 1;
  job->expectedRecords = job->receivedRecords = 1;
  job->expectedBytes = job->receivedBytes = len + 2;
  job->createdMs = job->lastDataMs = millis();
  dropQueueLocked();
  Chunk chunk;
  chunk.data = framed;
  chunk.len = len + 2;
  g_queue.push_back(chunk);
  g_queuedBytes = chunk.len;
  g_active = job;
  wakeWriter();
  return 200;
}

}  // namespace relay
