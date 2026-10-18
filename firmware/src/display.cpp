#include "display.h"

#include <U8g2lib.h>
#include <WiFi.h>
#include <Wire.h>

#include <vector>

#include "channels.h"
#include "inputs.h"
#include "netmgr.h"
#include "relay.h"
#include "timekeeping.h"
#include "uplink.h"

namespace display {
namespace {

constexpr int kWidth = 128;
constexpr int kHeight = 64;
constexpr uint32_t kFrameMs = 100;
constexpr uint32_t kChannelShowMs = 3000;   // Channel screen after the last turn.
constexpr uint32_t kDimAfterMs = 60000;     // No input and no screen change.
constexpr uint8_t kContrastNormal = 0xCF;
constexpr uint8_t kContrastDim = 0x01;

// Fonts: _te = Latin incl. extended (U+0020-U+02BD); unsupported characters
// are drawn as '?'.
const uint8_t* const kSmall = u8g2_font_6x10_tf;
const uint8_t* const kMedium = u8g2_font_helvB10_te;
const uint8_t* const kLarge = u8g2_font_helvB14_te;
const uint8_t* const kClock = u8g2_font_logisoso32_tn;

// No pins here: Wire is started on the detected pins in begin(). Pins given to U8g2
// are also reset by its GPIO init (pinMode), detaching them from the I2C peripheral.
U8G2_SSD1306_128X64_NONAME_F_HW_I2C g_oled(PC1_OLED_ROTATION == 2 ? U8G2_R2 : U8G2_R0, U8X8_PIN_NONE);
const char* g_status = "absent";
volatile bool g_resetting = false;
volatile bool g_resettingDrawn = false;
uint8_t g_lastFrame[kWidth * kHeight / 8];

enum class Screen { Resetting, Reset, Channel, Setup, Printing, Problem, Idle };

// --- text helpers -------------------------------------------------------------

std::vector<uint16_t> decodeUtf8(const String& s) {
  std::vector<uint16_t> out;
  const uint8_t* p = reinterpret_cast<const uint8_t*>(s.c_str());
  while (*p) {
    uint32_t cp;
    int extra;
    if (*p < 0x80) { cp = *p; extra = 0; }
    else if ((*p & 0xE0) == 0xC0) { cp = *p & 0x1F; extra = 1; }
    else if ((*p & 0xF0) == 0xE0) { cp = *p & 0x0F; extra = 2; }
    else if ((*p & 0xF8) == 0xF0) { cp = *p & 0x07; extra = 3; }
    else { ++p; out.push_back('?'); continue; }
    ++p;
    bool ok = true;
    for (int i = 0; i < extra; ++i) {
      if ((*p & 0xC0) != 0x80) { ok = false; break; }
      cp = (cp << 6) | (*p++ & 0x3F);
    }
    out.push_back(ok && cp <= 0xFFFF ? static_cast<uint16_t>(cp) : '?');
  }
  return out;
}

uint16_t drawable(uint16_t cp) { return u8g2_IsGlyph(g_oled.getU8g2(), cp) ? cp : '?'; }

int glyphWidth(uint16_t cp) { return u8g2_GetGlyphWidth(g_oled.getU8g2(), drawable(cp)); }

int textWidth(const std::vector<uint16_t>& text) {
  int w = 0;
  for (uint16_t cp : text) w += glyphWidth(cp);
  return w;
}

void drawText(int x, int y, const std::vector<uint16_t>& text) {
  for (uint16_t cp : text) x += g_oled.drawGlyph(x, y, drawable(cp));
}

void drawCentered(int y, const std::vector<uint16_t>& text) {
  drawText((kWidth - textWidth(text)) / 2, y, text);
}

void drawCentered(int y, const String& text) { drawCentered(y, decodeUtf8(text)); }

// Greedy word wrap into at most maxLines lines of kWidth pixels. Returns false
// (and ends the last line with "...") when the text does not fit.
bool wrap(const std::vector<uint16_t>& text, size_t maxLines, std::vector<std::vector<uint16_t>>* lines) {
  lines->clear();
  std::vector<uint16_t> line;
  size_t i = 0;
  while (i < text.size()) {
    // Next token (with its leading space if the line is not empty).
    size_t end = i;
    while (end < text.size() && text[end] != ' ') ++end;
    std::vector<uint16_t> token(text.begin() + i, text.begin() + end);
    std::vector<uint16_t> candidate = line;
    if (!candidate.empty()) candidate.push_back(' ');
    candidate.insert(candidate.end(), token.begin(), token.end());
    if (textWidth(candidate) <= kWidth) {
      line = candidate;
    } else if (!line.empty()) {
      lines->push_back(line);
      line.clear();
      continue;  // Retry the token on a fresh line.
    } else {
      // A single token wider than the screen: break it by characters.
      for (uint16_t cp : token) {
        if (!line.empty() && textWidth(line) + glyphWidth(cp) > kWidth) {
          lines->push_back(line);
          line.clear();
        }
        line.push_back(cp);
      }
    }
    i = end;
    while (i < text.size() && text[i] == ' ') ++i;
  }
  if (!line.empty()) lines->push_back(line);
  if (lines->size() <= maxLines) return true;

  lines->resize(maxLines);
  std::vector<uint16_t>& last = lines->back();
  const std::vector<uint16_t> dots = {'.', '.', '.'};
  while (!last.empty() && textWidth(last) + textWidth(dots) > kWidth) last.pop_back();
  last.insert(last.end(), dots.begin(), dots.end());
  return false;
}

// --- drawing ------------------------------------------------------------------

void header(const char* text) {
  g_oled.setFont(kSmall);
  g_oled.drawStr(0, 9, text);
  g_oled.drawHLine(0, 12, kWidth);
}

int signalBars(int32_t rssi) {
  if (rssi >= -55) return 4;
  if (rssi >= -65) return 3;
  if (rssi >= -75) return 2;
  if (rssi >= -85) return 1;
  return 0;
}

// Four ascending bars, 15x9 px, top-left at (x, y).
void drawSignal(int x, int y, int bars) {
  for (int i = 0; i < 4; ++i) {
    int h = 3 + i * 2;
    int bx = x + i * 4;
    int by = y + 9 - h;
    if (i < bars) {
      g_oled.drawBox(bx, by, 3, h);
    } else {
      g_oled.drawHLine(bx, y + 8, 3);  // Baseline only for missing bars.
    }
  }
}

void drawPositionDots(int position) {
  const int size = 4, gap = 6;
  int x = (kWidth - (channels::kCount * size + (channels::kCount - 1) * gap)) / 2;
  for (int i = 1; i <= channels::kCount; ++i, x += size + gap) {
    if (i == position) g_oled.drawBox(x, 59, size, size);
    else g_oled.drawFrame(x, 59, size, size);
  }
}

// No header: the name (or number) and the position dots are the whole screen.
// Text is centred vertically in the area above the dots (y 0-55).
void drawChannel(int position) {
  String name = channels::name(position);
  if (name.isEmpty()) {
    g_oled.setFont(kClock);
    char digits[4];
    snprintf(digits, sizeof(digits), "%d", position);
    g_oled.drawStr((kWidth - g_oled.getStrWidth(digits)) / 2, 44, digits);
  } else {
    std::vector<std::vector<uint16_t>> lines;
    auto text = decodeUtf8(name);
    g_oled.setFont(kLarge);
    if (wrap(text, 2, &lines)) {
      int top = lines.size() == 1 ? 35 : 26;
      for (size_t i = 0; i < lines.size(); ++i) drawCentered(top + i * 17, lines[i]);
    } else {
      g_oled.setFont(kMedium);
      wrap(text, 3, &lines);
      int top = lines.size() == 3 ? 20 : 26;
      for (size_t i = 0; i < lines.size(); ++i) drawCentered(top + i * 12, lines[i]);
    }
  }
  drawPositionDots(position);
}

void drawSetup() {
  header("WIFI SETUP");
  g_oled.setFont(kSmall);
  drawCentered(26, String("Join network"));
  g_oled.setFont(kMedium);
  drawCentered(41, netmgr::apSsid());
  g_oled.setFont(kSmall);
  drawCentered(58, String("then open ") + netmgr::apIp());
}

void drawPrinting(uint32_t now) {
  header("PRINTING");
  g_oled.setFont(kLarge);
  static const char* const kDots[] = {"Printing", "Printing.", "Printing..", "Printing..."};
  const char* text = kDots[(now / 400) % 4];
  // Left edge fixed at the width of "Printing..." so the word does not jump.
  int x = (kWidth - g_oled.getStrWidth(kDots[3])) / 2;
  g_oled.drawStr(x, 38, text);
  g_oled.setFont(kSmall);
  drawCentered(58, String("Hold button to cancel"));
}

void drawProblem(const char* title, const String& big, const String& detail) {
  header(title);
  g_oled.setFont(kLarge);
  drawCentered(36, big);
  g_oled.setFont(kSmall);
  std::vector<std::vector<uint16_t>> lines;
  wrap(decodeUtf8(detail), 2, &lines);
  for (size_t i = 0; i < lines.size(); ++i) drawCentered(50 + i * 11, lines[i]);
}

void drawReset(int step) {
  header("RESET");
  static const char* const kSteps[] = {"", "Turn clockwise", "Turn anticlockwise", "Click to reset"};
  g_oled.setFont(kMedium);
  drawCentered(34, String(kSteps[step]));
  g_oled.setFont(kSmall);
  char line[24];
  snprintf(line, sizeof(line), "Step %d of 3", step);
  drawCentered(50, String(line));
  drawCentered(61, String("Wait 10 s to cancel"));
}

void drawResetting() {
  g_oled.setFont(kLarge);
  drawCentered(30, String("Resetting"));
  g_oled.setFont(kSmall);
  drawCentered(48, String("Settings erased,"));
  drawCentered(59, String("restarting..."));
}

// Burn-in protection: the idle layout moves through these offsets, one per minute.
constexpr int8_t kShift[][2] = {{0, 0}, {1, 1}, {2, 0}, {1, -1}, {0, 1}, {-1, 0}, {-2, 1}, {-1, -1}};

void drawIdle(uint32_t now) {
  struct tm t;
  bool haveTime = timekeeping::localTime(&t);
  int shiftIndex = (haveTime ? t.tm_min : now / 60000) % (sizeof(kShift) / sizeof(kShift[0]));
  int dx = kShift[shiftIndex][0], dy = kShift[shiftIndex][1];

  drawSignal(kWidth - 17 + dx, 1 + dy, signalBars(WiFi.RSSI()));

  if (!haveTime) {
    g_oled.setFont(kLarge);
    g_oled.drawStr((kWidth - g_oled.getStrWidth("--:--")) / 2 + dx, 40 + dy, "--:--");
    g_oled.setFont(kSmall);
    const char* note = "Waiting for time";
    g_oled.drawStr((kWidth - g_oled.getStrWidth(note)) / 2 + dx, 58 + dy, note);
    return;
  }
  char digits[8];
  const char* suffix = nullptr;
  if (timekeeping::use24h()) {
    snprintf(digits, sizeof(digits), "%02d:%02d", t.tm_hour, t.tm_min);
  } else {
    int hour = t.tm_hour % 12;
    snprintf(digits, sizeof(digits), "%d:%02d", hour == 0 ? 12 : hour, t.tm_min);
    suffix = t.tm_hour < 12 ? "AM" : "PM";
  }
  g_oled.setFont(kClock);
  int clockWidth = g_oled.getStrWidth(digits);
  g_oled.setFont(kSmall);
  int suffixWidth = suffix ? g_oled.getStrWidth(suffix) + 3 : 0;
  int x = (kWidth - clockWidth - suffixWidth) / 2 + dx;
  int baseline = 50 + dy;
  g_oled.setFont(kClock);
  g_oled.drawStr(x, baseline, digits);
  if (suffix) {
    g_oled.setFont(kSmall);
    g_oled.drawStr(x + clockWidth + 3, baseline, suffix);
  }
}

// --- task -------------------------------------------------------------------

void displayTask(void*) {
  // Initialised here, not in setup(): a misbehaving bus must not block boot.
  uint32_t initStart = millis();
  g_oled.begin();
  g_oled.setContrast(kContrastNormal);
  memset(g_lastFrame, 0xFF, sizeof(g_lastFrame));  // Force the first frame out.
  Serial.printf("[display] initialised in %lu ms\n", static_cast<unsigned long>(millis() - initStart));
  Screen lastScreen = Screen::Idle;
  uint32_t screenChangedMs = millis();
  bool dimmed = false;
  for (;;) {
    uint32_t now = millis();
    Screen screen;
    int position = inputs::currentPosition();
    int resetStep = inputs::resetStep();
    uint32_t lastTurn = inputs::lastTurnMs();
    relay::Snapshot printer = relay::snapshot();
    uplink::LinkState link = uplink::state();
    bool wifi = netmgr::staConnected();

    if (g_resetting) screen = Screen::Resetting;
    else if (resetStep > 0) screen = Screen::Reset;
    else if (lastTurn && now - lastTurn < kChannelShowMs) screen = Screen::Channel;
    else if (netmgr::apActive()) screen = Screen::Setup;
    else if (printer.uartReady && printer.activeJobId.length()) screen = Screen::Printing;
    else if (!wifi || !link.ok || !printer.uartReady) screen = Screen::Problem;
    else screen = Screen::Idle;

    if (screen != lastScreen) {
      lastScreen = screen;
      screenChangedMs = now;
    }

    g_oled.clearBuffer();
    switch (screen) {
      case Screen::Resetting: drawResetting(); break;
      case Screen::Reset: drawReset(resetStep); break;
      case Screen::Channel: drawChannel(position); break;
      case Screen::Setup: drawSetup(); break;
      case Screen::Printing: drawPrinting(now); break;
      case Screen::Problem:
        if (!printer.uartReady) drawProblem("ERROR", "Printer error", "UART writer unavailable");
        else if (!wifi) drawProblem("DISCONNECTED", "No WiFi", "Reconnecting...");
        else if (link.message == "starting") drawProblem("CONNECTING", "Connecting", "Waiting for the server");
        else drawProblem("DISCONNECTED", "Server offline", link.message);
        break;
      case Screen::Idle: drawIdle(now); break;
    }

    // Only push changed frames: a full transfer takes ~25 ms at 400 kHz.
    uint8_t* frame = g_oled.getBufferPtr();
    if (memcmp(frame, g_lastFrame, sizeof(g_lastFrame)) != 0) {
      memcpy(g_lastFrame, frame, sizeof(g_lastFrame));
      g_oled.sendBuffer();
    }
    if (screen == Screen::Resetting) g_resettingDrawn = true;

    uint32_t activity = inputs::lastActivityMs();
    uint32_t awakeSince = screenChangedMs;
    if (activity && static_cast<int32_t>(activity - awakeSince) > 0) awakeSince = activity;
    bool wantDim = now - awakeSince > kDimAfterMs && screen != Screen::Resetting;
    if (wantDim != dimmed) {
      dimmed = wantDim;
      g_oled.setContrast(dimmed ? kContrastDim : kContrastNormal);
    }
    vTaskDelay(pdMS_TO_TICKS(kFrameMs));
  }
}

// Logs every responding 7-bit address on the given pins (wiring diagnostics).
String scanBus(int sda, int scl) {
  Wire.end();
  Wire.begin(sda, scl, 100000);
  String found;
  for (uint8_t a = 0x08; a < 0x78; ++a) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) found += String(found.isEmpty() ? "" : " ") + "0x" + String(a, HEX);
  }
  return found.isEmpty() ? String("nothing") : found;
}

// Starts Wire on these pins and returns the SSD1306 address, or 0 (Wire stopped).
uint8_t probe(int sda, int scl, int attempts, uint32_t hz) {
  Wire.end();
  Wire.begin(sda, scl, hz);
  for (int attempt = 0; attempt < attempts; ++attempt) {
    if (attempt) delay(100);  // Some modules need a moment after power-up.
    for (uint8_t candidate : {0x3C, 0x3D}) {
      Wire.beginTransmission(candidate);
      if (Wire.endTransmission() == 0) return candidate;
    }
  }
  Wire.end();
  return 0;
}

}  // namespace

void begin() {
  int sda = PC1_OLED_SDA_PIN, scl = PC1_OLED_SCL_PIN;
  uint32_t startMs = millis();
  uint32_t hz = 400000;
  uint8_t address = probe(sda, scl, 5, hz);
  if (!address && (address = probe(sda, scl, 1, 100000))) {
    hz = 100000;  // Weak pull-ups or long wires: run the bus slower.
    Serial.println("[display] no reply at 400 kHz; using 100 kHz");
  }
  if (!address && (address = probe(scl, sda, 1, 100000))) {
    hz = 100000;
    // SDA and SCL wired the other way round: use them as wired.
    std::swap(sda, scl);
    Serial.printf("[display] SDA/SCL are swapped relative to the build flags; using SDA=GPIO%d SCL=GPIO%d\n",
                  sda, scl);
  }
  // Wire stays running on the detected pins; U8g2's own Wire.begin() is then a no-op.
  if (!address) {
    String direct = scanBus(PC1_OLED_SDA_PIN, PC1_OLED_SCL_PIN);
    String swapped = scanBus(PC1_OLED_SCL_PIN, PC1_OLED_SDA_PIN);
    Wire.end();
    Serial.printf("[display] no SSD1306 on SDA=GPIO%d SCL=GPIO%d; running without a display "
                  "(I2C scan: %s; with SDA/SCL swapped: %s)\n",
                  PC1_OLED_SDA_PIN, PC1_OLED_SCL_PIN, direct.c_str(), swapped.c_str());
    return;
  }
  Serial.printf("[display] found 0x%02X after %lu ms; initialising\n", address,
                static_cast<unsigned long>(millis() - startMs));
  Wire.setTimeOut(50);
  g_oled.setI2CAddress(address << 1);
  g_oled.setBusClock(hz);
  g_status = "ok";
  Serial.printf("[display] SSD1306 at 0x%02X (SDA=GPIO%d SCL=GPIO%d, %lu kHz)\n", address, sda, scl,
                static_cast<unsigned long>(hz / 1000));
  xTaskCreatePinnedToCore(displayTask, "display", 6144, nullptr, 1, nullptr, 1);
}

const char* status() { return g_status; }

void showResetting() {
  if (strcmp(g_status, "ok") != 0) return;
  g_resetting = true;
  for (int i = 0; i < 30 && !g_resettingDrawn; ++i) delay(10);
}

}  // namespace display
