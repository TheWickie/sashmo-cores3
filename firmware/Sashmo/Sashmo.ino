#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <M5Unified.h>
#include <WiFi.h>
#include <esp_heap_caps.h>

#include "config_private.h"
#include "splash_image.h"

constexpr uint32_t kSampleRate = 16000;
constexpr size_t kRecordSamples = kSampleRate * 6;
constexpr size_t kWakeSamples = kSampleRate * 2;
constexpr size_t kWakeTailSamples = kSampleRate;
constexpr size_t kWavHeaderBytes = 44;
constexpr size_t kMaxReplyBytes = 700000;

enum class ScreenState { Idle, Listening, Processing, Speaking, Details, Notifications, Disconnected, Error };

struct QuotaWindow {
  bool present = false;
  int remaining = 0;
  int durationMins = 0;
  String resetLocal;
};

ScreenState screenState = ScreenState::Disconnected;
QuotaWindow windows[2];
int windowCount = 0;
int resetCredits = -1;
int notificationCount = 0;
int mailUnreadCount = -1;
int calendarEventCount = -1;
String notificationTitle;
String notificationSummary;
int snapshotVersion = -1;
String planReported;
String clockText = "--:--";
String dateText;
String errorText;
bool codexConnected = false;
bool serverConnected = false;
bool dirty = true;
int batteryLevel = -1;
bool batteryPresent = false;
bool charging = false;
bool usbPowered = true;
uint32_t nextPulseMs = 0;
uint32_t lastWifiAttemptMs = 0;
uint32_t lastPowerReadMs = 0;
uint32_t retryMs = 5000;
bool wifiWasConnected = false;
uint32_t lastInteractionMs = 0;
uint8_t appliedBrightness = 0;
bool displaySleeping = false;
int16_t* wakeSamples = nullptr;
int16_t* wakeTail = nullptr;
bool wakeMicActive = false;
bool wakeRecording = false;
uint32_t wakeRecordingStartedMs = 0;

String baseUrl() {
  return String("http://") + kServerHost + ":" + String(kServerPort);
}

void setState(ScreenState state) {
  if (screenState != state) {
    screenState = state;
    dirty = true;
  }
}

String durationLabel(int minutes) {
  if (minutes == 10080) return "weekly";
  if (minutes > 0 && minutes % 1440 == 0) return String(minutes / 1440) + " day";
  if (minutes > 0 && minutes % 60 == 0) return String(minutes / 60) + " hour";
  return "reported";
}

String resetLabel(const String& value) {
  if (value.length() < 16) return "";
  static const char* months[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
  const int month = value.substring(5, 7).toInt();
  if (month < 1 || month > 12) return "";
  return value.substring(8, 10) + " " + months[month - 1] + " " + value.substring(11, 16);
}

void drawFrame() {
  M5.Display.fillScreen(0x0841);
  M5.Display.fillRoundRect(8, 34, 304, 113, 12, 0x18E3);
  M5.Display.setTextColor(0xBDF7, 0x0841);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(10, 7);
  M5.Display.print(clockText);
  M5.Display.setTextSize(1);
  M5.Display.setCursor(164, 14);
  M5.Display.print(dateText);
  M5.Display.setCursor(260, 14);
  if (!usbPowered && batteryLevel >= 0 && batteryLevel <= kLowBatteryPercent) {
    M5.Display.setTextColor(0xFBE0, 0x0841);
  }
  if (!batteryPresent) {
    M5.Display.print("NO BAT");
  } else if (batteryLevel >= 0) {
    M5.Display.printf("%d%%", batteryLevel);
  } else {
    M5.Display.print("--%");
  }
  if (charging) M5.Display.print("+");
  if (!usbPowered && batteryLevel >= 0 && batteryLevel <= kLowBatteryPercent) {
    M5.Display.setCursor(265, 25);
    M5.Display.print("LOW BAT");
  }
}

void drawIdle() {
  drawFrame();
  M5.Display.setTextColor(0xBDF7, 0x18E3);
  M5.Display.setTextSize(1);
  M5.Display.setCursor(20, 44);
  M5.Display.print("CODEX REMAINING");
  M5.Display.setCursor(250, 44);
  M5.Display.print(planReported);

  if (windowCount > 0 && windows[0].present) {
    M5.Display.setTextColor(0x7FEF, 0x18E3);
    M5.Display.setTextSize(5);
    M5.Display.setCursor(19, 59);
    M5.Display.printf("%d%%", windows[0].remaining);
    M5.Display.setTextSize(1);
    M5.Display.setTextColor(0xFFFF, 0x18E3);
    M5.Display.setCursor(214, 84);
    M5.Display.print(durationLabel(windows[0].durationMins));
    const String reset = resetLabel(windows[0].resetLocal);
    if (!reset.isEmpty()) {
      M5.Display.setCursor(20, 131);
      M5.Display.print("Resets ");
      M5.Display.print(reset);
    }
  } else {
    M5.Display.setTextColor(0xFFFF, 0x18E3);
    M5.Display.setTextSize(2);
    M5.Display.setCursor(20, 85);
    M5.Display.print("Quota unavailable");
  }

  M5.Display.setTextSize(1);
  M5.Display.setTextColor(serverConnected ? 0x7FEF : 0xFBE0, 0x0841);
  M5.Display.setCursor(12, 154);
  M5.Display.print(serverConnected ? "Desktop connected" : "Desktop disconnected");
  M5.Display.setCursor(215, 154);
  M5.Display.print(WiFi.status() == WL_CONNECTED ? "Wi-Fi OK" : "Wi-Fi offline");

  M5.Display.setCursor(12, 224);
  M5.Display.setTextColor(notificationCount > 0 ? 0xFBE0 : 0xBDF7, 0x0841);
  M5.Display.printf("ALERTS %d", notificationCount);
  M5.Display.setTextColor(0xBDF7, 0x0841);
  M5.Display.setCursor(205, 224);
  if (mailUnreadCount >= 0) M5.Display.printf("MAIL %d ", mailUnreadCount);
  if (calendarEventCount >= 0) M5.Display.printf("CAL %d", calendarEventCount);

  M5.Display.fillCircle(160, 194, 30, 0x25F6);
  M5.Display.drawCircle(160, 194, 31, 0xBDF7);
  M5.Display.setTextColor(0xFFFF, 0x25F6);
  M5.Display.fillRoundRect(154, 178, 12, 20, 6, 0xFFFF);
  M5.Display.drawArc(160, 187, 13, 11, 0, 180, 0xFFFF);
  M5.Display.drawLine(160, 201, 160, 207, 0xFFFF);
  M5.Display.drawLine(153, 207, 167, 207, 0xFFFF);
  M5.Display.setTextColor(0xBDF7, 0x0841);
  M5.Display.setTextSize(1);
  M5.Display.setCursor(116, 229);
  M5.Display.print(kWakeWordEnabled && (usbPowered || kWakeOnBattery)
                     ? "SAY HELLO SASHMO" : "TAP TO SPEAK");
}

void drawDetails() {
  drawFrame();
  M5.Display.setTextColor(0xFFFF, 0x18E3);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(20, 48);
  M5.Display.print("Codex limits");
  M5.Display.setTextSize(1);
  M5.Display.setCursor(20, 80);
  M5.Display.print("Reported plan: ");
  M5.Display.println(planReported);
  if (windowCount == 0) {
    M5.Display.setCursor(20, 110);
    M5.Display.print("No usage window reported");
  }
  for (int i = 0; i < windowCount && i < 2; ++i) {
    const int y = 106 + i * 32;
    M5.Display.setCursor(20, y);
    M5.Display.printf("%s: %d%% left", durationLabel(windows[i].durationMins).c_str(), windows[i].remaining);
    M5.Display.setCursor(20, y + 13);
    M5.Display.print(resetLabel(windows[i].resetLocal));
  }
  M5.Display.setTextColor(0xBDF7, 0x0841);
  M5.Display.setCursor(20, 174);
  if (resetCredits >= 0) {
    M5.Display.printf("Reset credits available: %d", resetCredits);
  }
  M5.Display.setCursor(20, 190);
  M5.Display.print(codexConnected ? "Codex connected" : "Codex unavailable");
  M5.Display.setTextColor(0xBDF7, 0x0841);
  M5.Display.setCursor(112, 216);
  M5.Display.print("TAP TO RETURN");
}

void drawNotifications() {
  drawFrame();
  M5.Display.setTextColor(0xFFFF, 0x18E3);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(20, 48);
  M5.Display.print("Alerts");
  M5.Display.setTextSize(1);
  M5.Display.setCursor(20, 86);
  M5.Display.printf("%d current alert%s", notificationCount, notificationCount == 1 ? "" : "s");
  M5.Display.setCursor(20, 109);
  M5.Display.print(notificationCount > 0 ? notificationTitle.substring(0, 46) : "Nothing needs attention");
  M5.Display.setCursor(20, 125);
  M5.Display.print(notificationSummary.substring(0, 46));
  if (notificationSummary.length() > 46) {
    M5.Display.setCursor(20, 140);
    M5.Display.print(notificationSummary.substring(46, 92));
  }
  M5.Display.setTextColor(0xBDF7, 0x0841);
  M5.Display.setCursor(112, 216);
  M5.Display.print("TAP TO RETURN");
}

void drawActivity(const char* title, const char* subtitle, uint16_t color) {
  drawFrame();
  M5.Display.setTextColor(color, 0x18E3);
  M5.Display.setTextSize(3);
  M5.Display.setCursor(20, 65);
  M5.Display.print(title);
  M5.Display.setTextColor(0xFFFF, 0x0841);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(20, 165);
  M5.Display.print(subtitle);
}

void render() {
  if (!dirty || displaySleeping) return;
  dirty = false;
  switch (screenState) {
    case ScreenState::Idle: drawIdle(); break;
    case ScreenState::Details: drawDetails(); break;
    case ScreenState::Notifications: drawNotifications(); break;
    case ScreenState::Listening: drawActivity("Listening", "Speak now", 0x7FEF); break;
    case ScreenState::Processing: drawActivity("Processing", "One moment", 0xFFE0); break;
    case ScreenState::Speaking: drawActivity("Speaking", "Playing reply", 0x7FEF); break;
    case ScreenState::Disconnected: drawActivity("Offline", "Reconnecting", 0xFBE0); break;
    case ScreenState::Error: drawActivity("Error", errorText.c_str(), 0xF800); break;
  }
}

void drawSplash() {
  M5.Display.fillScreen(0xC6F8);
  if (!M5.Display.drawJpg(kSplashJpeg, kSplashJpegBytes, 0, 0)) {
    Serial.println("Splash image unavailable");
  }
  M5.Display.setTextColor(0x1823, 0xC6F8);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(8, 218);
  M5.Display.print("SASHMO");
  M5.Display.setTextSize(1);
  M5.Display.setCursor(253, 225);
  M5.Display.print("STARTING");
}

void readPower() {
  const bool present = M5.Power.Axp2101.getBatState();
  const int level = present ? M5.Power.getBatteryLevel() : -1;
  const bool nowCharging = present && M5.Power.isCharging() == 1;
  const bool nowUsb = M5.Power.getVBUSVoltage() > 4000;
  const bool sourceChanged = nowUsb != usbPowered;
  if (level != batteryLevel || present != batteryPresent || nowCharging != charging || nowUsb != usbPowered) {
    batteryLevel = level;
    batteryPresent = present;
    charging = nowCharging;
    usbPowered = nowUsb;
    dirty = true;
  }
  if (sourceChanged) nextPulseMs = millis();
}

void applyPowerProfile() {
  if (displaySleeping) return;
  uint8_t target = kUsbBrightness;
  if (!usbPowered) {
    if (batteryLevel >= 0 && batteryLevel <= kLowBatteryPercent) {
      target = kLowBatteryBrightness;
    } else if (millis() - lastInteractionMs >= kBatteryDimAfterSeconds * 1000UL) {
      target = kBatteryIdleBrightness;
    } else {
      target = kBatteryBrightness;
    }
  }
  if (target != appliedBrightness) {
    M5.Display.setBrightness(target);
    appliedBrightness = target;
  }
}

void updateDisplayStandby(uint32_t now) {
  const bool activeVoice = screenState == ScreenState::Listening
                        || screenState == ScreenState::Processing
                        || screenState == ScreenState::Speaking;
  if (!displaySleeping && !activeVoice
      && now - lastInteractionMs >= kScreenStandbyAfterSeconds * 1000UL) {
    M5.Display.sleep();
    displaySleeping = true;
    dirty = true;
    Serial.println("DISPLAY standby");
  }
}

void wakeDisplay(uint32_t now) {
  M5.Display.wakeup();
  displaySleeping = false;
  lastInteractionMs = now;
  dirty = true;
  applyPowerProfile();
  Serial.println("DISPLAY awake");
}

uint32_t pulseIntervalMs() {
  if (!usbPowered && batteryLevel >= 0 && batteryLevel <= kLowBatteryPercent)
    return kLowPowerPulseSeconds * 1000UL;
  return (usbPowered ? kUsbPulseSeconds : kBatteryPulseSeconds) * 1000UL;
}

void parsePulse(const String& payload) {
  JsonDocument doc;
  if (deserializeJson(doc, payload)) {
    errorText = "Bad desktop data";
    setState(ScreenState::Error);
    return;
  }
  const int newVersion = doc["version"] | snapshotVersion;
  const String newTime = String(doc["time"] | "--:--");
  const String newDate = String(doc["date"] | "");
  if (newTime != clockText || newDate != dateText) {
    clockText = newTime;
    dateText = newDate;
    dirty = true;
  }
  if (doc["quota"].is<JsonObject>() || doc["codex_connected"].is<bool>()) {
    planReported = String(doc["quota"]["plan_reported"] | "unknown");
    codexConnected = doc["codex_connected"] | false;
    resetCredits = doc["quota"]["reset_credits_available"].is<int>()
                       ? doc["quota"]["reset_credits_available"].as<int>() : -1;
    windowCount = 0;
    for (JsonObject bucket : doc["quota"]["buckets"].as<JsonArray>()) {
      if (String(bucket["id"] | "") != "codex") continue;
      for (JsonObject window : bucket["windows"].as<JsonArray>()) {
        if (windowCount >= 2) break;
        windows[windowCount].present = true;
        windows[windowCount].remaining = window["remaining_percent"] | 0;
        windows[windowCount].durationMins = window["duration_mins"] | 0;
        windows[windowCount].resetLocal = String(window["resets_at_local"] | "");
        ++windowCount;
      }
      break;
    }
    dirty = true;
  }
  if (doc["notification_count"].is<int>()) {
    notificationCount = doc["notification_count"].as<int>();
    notificationTitle = "";
    notificationSummary = "";
    for (JsonObject item : doc["notifications"].as<JsonArray>()) {
      notificationTitle = String(item["title"] | "");
      notificationSummary = String(item["summary"] | "");
    }
    dirty = true;
  }
  if (doc.containsKey("gmail_unread_count")) {
    mailUnreadCount = doc["gmail_unread_count"].is<int>() ? doc["gmail_unread_count"].as<int>() : -1;
    dirty = true;
  }
  if (doc.containsKey("calendar_event_count")) {
    calendarEventCount = doc["calendar_event_count"].is<int>() ? doc["calendar_event_count"].as<int>() : -1;
    dirty = true;
  }
  snapshotVersion = newVersion;
}

void fetchPulse() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  const String url = baseUrl() + "/v1/pulse?since=" + String(snapshotVersion)
                   + "&battery=" + String(batteryLevel)
                   + "&charging=" + String(charging ? 1 : 0)
                   + "&usb=" + String(usbPowered ? 1 : 0)
                   + "&screen_sleeping=" + String(displaySleeping ? 1 : 0)
                   + "&wifi_rssi=" + String(WiFi.RSSI());
  http.begin(url);
  http.setTimeout(5000);
  http.addHeader("X-Sashmo-Token", kDeviceToken);
  const int status = http.GET();
  if (status == 200) {
    const String payload = http.getString();
    serverConnected = true;
    retryMs = 5000;
    parsePulse(payload);
    if (screenState == ScreenState::Disconnected || screenState == ScreenState::Error) setState(ScreenState::Idle);
    nextPulseMs = millis() + pulseIntervalMs();
    Serial.printf("PULSE ok version=%d windows=%d battery=%d%%\n", snapshotVersion, windowCount, batteryLevel);
  } else {
    serverConnected = false;
    setState(ScreenState::Disconnected);
    nextPulseMs = millis() + retryMs;
    retryMs = min(retryMs * 2, 300000UL);
    Serial.printf("PULSE failed http=%d retry_ms=%lu\n", status, retryMs);
  }
  http.end();
}

void put32(uint8_t* target, uint32_t value) {
  for (int i = 0; i < 4; ++i) target[i] = value >> (i * 8);
}

void put16(uint8_t* target, uint16_t value) {
  target[0] = value & 0xFF;
  target[1] = value >> 8;
}

void makeWavHeader(uint8_t* data, size_t samples) {
  const uint32_t payloadBytes = samples * sizeof(int16_t);
  memcpy(data, "RIFF", 4);
  put32(data + 4, payloadBytes + 36);
  memcpy(data + 8, "WAVEfmt ", 8);
  put32(data + 16, 16);
  put16(data + 20, 1);
  put16(data + 22, 1);
  put32(data + 24, kSampleRate);
  put32(data + 28, kSampleRate * 2);
  put16(data + 32, 2);
  put16(data + 34, 16);
  memcpy(data + 36, "data", 4);
  put32(data + 40, payloadBytes);
}

void failVoice(const String& message) {
  errorText = message;
  setState(ScreenState::Error);
  render();
  delay(1500);
  setState(serverConnected ? ScreenState::Idle : ScreenState::Disconnected);
}

void stopWakeListening() {
  if (wakeMicActive) M5.Mic.end();
  wakeMicActive = false;
  wakeRecording = false;
}

bool pushToTalkOnce(bool followUp) {
  stopWakeListening();
  if (WiFi.status() != WL_CONNECTED || !serverConnected) {
    failVoice("Desktop offline");
    return false;
  }

  const size_t wavBytes = kWavHeaderBytes + kRecordSamples * sizeof(int16_t);
  uint8_t* wav = static_cast<uint8_t*>(heap_caps_malloc(wavBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!wav) wav = static_cast<uint8_t*>(malloc(wavBytes));
  if (!wav) { failVoice("No audio memory"); return false; }

  M5.Speaker.end();
  setState(ScreenState::Listening);
  render();
  const bool micStarted = M5.Mic.begin();
  const bool queued = micStarted && M5.Mic.record(reinterpret_cast<int16_t*>(wav + kWavHeaderBytes), kRecordSamples, kSampleRate);
  if (!queued) {
    M5.Mic.end();
    free(wav);
    failVoice("Microphone failed");
    return false;
  }
  const uint32_t deadline = millis() + 8000;
  while (M5.Mic.isRecording() && static_cast<int32_t>(deadline - millis()) > 0) {
    M5.update();
    delay(10);
  }
  const bool complete = M5.Mic.isRecording() == 0;
  M5.Mic.end();
  if (!complete) {
    free(wav);
    failVoice("Mic timeout");
    return false;
  }

  int peak = 0;
  const int16_t* samples = reinterpret_cast<int16_t*>(wav + kWavHeaderBytes);
  for (size_t i = 0; i < kRecordSamples; ++i) peak = max(peak, abs(static_cast<int>(samples[i])));
  Serial.printf("MIC captured samples=%u peak=%d\n", static_cast<unsigned>(kRecordSamples), peak);
  if (followUp) {
    int activeBlocks = 0;
    for (size_t offset = 0; offset < kRecordSamples; offset += 1600) {
      uint32_t magnitude = 0;
      int blockPeak = 0;
      for (size_t i = offset; i < offset + 1600; ++i) {
        const int sample = abs(static_cast<int>(samples[i]));
        magnitude += sample;
        blockPeak = max(blockPeak, sample);
      }
      if (blockPeak >= 200 && magnitude / 1600 >= 30) ++activeBlocks;
    }
    if (activeBlocks < 2) {
      free(wav);
      setState(ScreenState::Idle);
      return false;
    }
  }
  makeWavHeader(wav, kRecordSamples);
  setState(ScreenState::Processing);
  render();

  HTTPClient http;
  http.begin(baseUrl() + "/v1/voice");
  http.setTimeout(30000);
  http.addHeader("X-Sashmo-Token", kDeviceToken);
  http.addHeader("Content-Type", "audio/wav");
  if (followUp) http.addHeader("X-Sashmo-Follow-Up", "1");
  const int status = http.POST(wav, wavBytes);
  free(wav);
  if (followUp && status == 204) {
    http.end();
    setState(ScreenState::Idle);
    return false;
  }
  if (status != 200) {
    Serial.printf("VOICE failed http=%d\n", status);
    http.end();
    failVoice("Voice service error");
    return false;
  }

  const int replySize = http.getSize();
  if (replySize < 44 || replySize > static_cast<int>(kMaxReplyBytes)) {
    http.end();
    failVoice("Reply too large");
    return false;
  }
  uint8_t* reply = static_cast<uint8_t*>(heap_caps_malloc(replySize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!reply) reply = static_cast<uint8_t*>(malloc(replySize));
  if (!reply) { http.end(); failVoice("No reply memory"); return false; }
  WiFiClient* stream = http.getStreamPtr();
  size_t received = 0;
  const uint32_t readDeadline = millis() + 30000;
  while (received < static_cast<size_t>(replySize) && static_cast<int32_t>(readDeadline - millis()) > 0) {
    const int available = stream->available();
    if (available > 0) {
      const size_t wanted = min(static_cast<size_t>(available), static_cast<size_t>(replySize) - received);
      const size_t count = stream->readBytes(reply + received, wanted);
      received += count;
    } else {
      delay(10);
    }
  }
  http.end();
  if (received != static_cast<size_t>(replySize)) {
    free(reply);
    failVoice("Reply timeout");
    return false;
  }

  setState(ScreenState::Speaking);
  render();
  M5.Speaker.setVolume(160);
  if (!M5.Speaker.begin() || !M5.Speaker.playWav(reply, replySize, 1, 0, true)) {
    free(reply);
    failVoice("Speaker failed");
    return false;
  }
  Serial.printf("SPEAKER playing bytes=%d\n", replySize);
  while (M5.Speaker.isPlaying()) {
    M5.update();
    delay(10);
  }
  M5.Speaker.end();
  free(reply);
  setState(ScreenState::Idle);
  nextPulseMs = millis();
  return true;
}

void pushToTalk() {
  for (int turn = 0; turn < 8; ++turn) {
    if (turn > 0) delay(250);  // Let speaker output settle before listening again.
    if (!pushToTalkOnce(turn > 0)) break;
  }
  lastInteractionMs = millis();
  nextPulseMs = millis();
}

void pollWakeWord(uint32_t now) {
  const bool allowedPower = usbPowered || (kWakeOnBattery &&
                            (batteryLevel < 0 || batteryLevel > kLowBatteryPercent));
  const bool shouldListen = kWakeWordEnabled && allowedPower && serverConnected
                         && WiFi.status() == WL_CONNECTED
                         && screenState != ScreenState::Listening
                         && screenState != ScreenState::Processing
                         && screenState != ScreenState::Speaking;
  if (!shouldListen) {
    stopWakeListening();
    return;
  }
  if (!wakeSamples) wakeSamples = static_cast<int16_t*>(heap_caps_malloc(
      kWakeSamples * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!wakeTail) {
    wakeTail = static_cast<int16_t*>(heap_caps_calloc(
        kWakeTailSamples, sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  }
  if (!wakeSamples || !wakeTail) return;
  static uint32_t nextAttemptMs = 0;
  if (!wakeMicActive) {
    if (static_cast<int32_t>(now - nextAttemptMs) < 0) return;
    M5.Speaker.end();
    wakeMicActive = M5.Mic.begin();
    if (!wakeMicActive) {
      nextAttemptMs = now + 5000;
      return;
    }
  }
  if (!wakeRecording) {
    wakeRecording = M5.Mic.record(wakeSamples, kWakeSamples, kSampleRate);
    wakeRecordingStartedMs = now;
    return;
  }
  if (M5.Mic.isRecording()) {
    if (now - wakeRecordingStartedMs > 5000) {
      stopWakeListening();
      nextAttemptMs = now + 5000;
    }
    return;
  }
  wakeRecording = false;
  uint64_t magnitude = 0;
  int peak = 0;
  for (size_t i = 0; i < kWakeSamples; ++i) {
    const int sample = abs(static_cast<int>(wakeSamples[i]));
    magnitude += sample;
    peak = max(peak, sample);
  }
  bool detected = false;
  if (peak >= 500 && magnitude / kWakeSamples >= 90) {
    const size_t totalSamples = kWakeTailSamples + kWakeSamples;
    const size_t wavBytes = kWavHeaderBytes + totalSamples * sizeof(int16_t);
    uint8_t* wav = static_cast<uint8_t*>(heap_caps_malloc(
        wavBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (wav) {
      makeWavHeader(wav, totalSamples);
      memcpy(wav + kWavHeaderBytes, wakeTail, kWakeTailSamples * sizeof(int16_t));
      memcpy(wav + kWavHeaderBytes + kWakeTailSamples * sizeof(int16_t),
             wakeSamples, kWakeSamples * sizeof(int16_t));
      HTTPClient http;
      http.begin(baseUrl() + "/v1/wake");
      http.setTimeout(5000);
      http.addHeader("X-Sashmo-Token", kDeviceToken);
      http.addHeader("Content-Type", "audio/wav");
      if (http.POST(wav, wavBytes) == 200) {
        JsonDocument answer;
        if (deserializeJson(answer, http.getString()) == DeserializationError::Ok)
          detected = answer["wake"] | false;
      }
      http.end();
      free(wav);
    }
  }
  memcpy(wakeTail, wakeSamples + kWakeSamples - kWakeTailSamples,
         kWakeTailSamples * sizeof(int16_t));
  if (detected) {
    Serial.println("WAKE phrase detected");
    stopWakeListening();
    wakeDisplay(millis());
    M5.Speaker.setVolume(120);
    if (M5.Speaker.begin()) {
      M5.Speaker.tone(880, 120);
      delay(150);
      M5.Speaker.end();
    }
    pushToTalk();
  }
}

void setup() {
  auto config = M5.config();
  M5.begin(config);
  M5.Power.setBatteryCharge(true);
  M5.Power.setChargeCurrent(200);
  Serial.begin(115200);
  if (M5.Display.width() < M5.Display.height()) M5.Display.setRotation(M5.Display.getRotation() ^ 1);
  M5.Speaker.end();
  M5.Mic.end();
  readPower();
  lastInteractionMs = millis();
  applyPowerProfile();
  Serial.printf("SASHMO boot battery=%d%% battery_mv=%d present=%d charging=%d vbus=%d mV\n",
                batteryLevel, M5.Power.getBatteryVoltage(), M5.Power.Axp2101.getBatState(),
                charging, M5.Power.getVBUSVoltage());
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(kWifiSsid, kWifiPassword);
  lastWifiAttemptMs = millis();
  drawSplash();
  delay(1600);
  render();
}

void loop() {
  M5.update();
  const uint32_t now = millis();

  if (now - lastPowerReadMs >= 15000) {
    lastPowerReadMs = now;
    readPower();
    applyPowerProfile();
    Serial.printf("POWER battery=%d%% battery_mv=%d present=%d charging=%d vbus=%d mV\n",
                  batteryLevel, M5.Power.getBatteryVoltage(), M5.Power.Axp2101.getBatState(),
                  charging, M5.Power.getVBUSVoltage());
  }
  const bool wifiConnected = WiFi.status() == WL_CONNECTED;
  if (wifiConnected && !wifiWasConnected) {
    nextPulseMs = now;
    retryMs = 5000;
    dirty = true;
  }
  wifiWasConnected = wifiConnected;
  if (!wifiConnected) {
    if (now - lastWifiAttemptMs >= 10000) {
      WiFi.reconnect();
      lastWifiAttemptMs = now;
      Serial.println("Wi-Fi reconnecting");
    }
    serverConnected = false;
    setState(ScreenState::Disconnected);
  } else if (static_cast<int32_t>(now - nextPulseMs) >= 0) {
    fetchPulse();
  }

  if (M5.Touch.getCount()) {
    const auto touch = M5.Touch.getDetail(0);
    if (touch.wasClicked()) {
      if (displaySleeping) {
        wakeDisplay(now);
        render();
        if (screenState == ScreenState::Idle && touch.y >= 160
            && touch.x >= 115 && touch.x <= 205) pushToTalk();
        return;
      }
      lastInteractionMs = now;
      applyPowerProfile();
      Serial.printf("TOUCH x=%d y=%d\n", touch.x, touch.y);
      if (screenState == ScreenState::Details || screenState == ScreenState::Notifications) {
        setState(ScreenState::Idle);
      } else if (screenState == ScreenState::Idle && touch.y >= 205 && touch.x <= 100) {
        setState(ScreenState::Notifications);
      } else if (screenState == ScreenState::Idle && touch.y >= 160 && touch.x >= 115 && touch.x <= 205) {
        pushToTalk();
      } else if (screenState == ScreenState::Idle && touch.y >= 34 && touch.y <= 147) {
        setState(ScreenState::Details);
      }
    }
  }
  updateDisplayStandby(now);
  pollWakeWord(now);
  applyPowerProfile();
  render();
  delay(20);
}
