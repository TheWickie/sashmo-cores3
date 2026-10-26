#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <M5Unified.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiMulti.h>
#include <esp_heap_caps.h>

#include "config_private.h"
#include "splash_image.h"

constexpr uint32_t kSampleRate = 16000;
constexpr size_t kRecordSamples = kSampleRate * 20;
constexpr size_t kRecordChunkSamples = kSampleRate / 4;
constexpr size_t kStartTimeoutSamples = kSampleRate * 6;
constexpr int kEndSilenceChunks = 10;
constexpr size_t kWakeSamples = kSampleRate * 2;
constexpr size_t kWakeTailSamples = kSampleRate;
constexpr size_t kWavHeaderBytes = 44;
constexpr size_t kMaxReplyBytes = 1000000;

enum class ScreenState { Idle, Listening, Processing, Speaking, Details, Notifications, Volume, Disconnected, Error };
enum class ReplyPlayback { Completed, Stopped, Failed };

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
struct NotificationItem { String source; String title; String summary; };
NotificationItem notificationItems[12];
int notificationItemsCount = 0;
int tickerIndex = 0;
int tickerOffset = 0;
int notificationScroll = 0;
bool notificationPaused = false;
uint32_t nextAnimationMs = 0;
int mailUnreadCount = -1;
int calendarEventCount = -1;
String notificationTitle;
String notificationSummary;
int snapshotVersion = -1;
String planReported;
String clockText = "--:--";
String dateText;
String apiSpent;
String apiBudgetLeft;
String errorText;
bool codexConnected = false;
bool serverConnected = false;
bool wakeWordEnabled = false;
bool deckRemoteEnabled = false;
bool dirty = true;
M5Canvas uiCanvas(&M5.Display);
lgfx::LovyanGFX* ui = &M5.Display;
bool uiCanvasReady = false;
WiFiMulti wifiMulti;
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
uint32_t screenStandbyAfterSeconds = kScreenStandbyAfterSeconds;
uint32_t lastRemoteTalkSeq = 0;
uint32_t pendingRemoteTalkSeq = 0;
String remoteTalkResult;
int volumePercent = 63;
int replyPart = 1;
int replyParts = 1;
String conversationMemory;
int16_t* wakeSamples = nullptr;
int16_t* wakeTail = nullptr;
bool wakeMicActive = false;
bool wakeRecording = false;
uint32_t wakeRecordingStartedMs = 0;
void stopWakeListening();
void wakeDisplay(uint32_t now);
void pushToTalk();

String baseUrl() {
  return String("http://") + kServerHost + ":" + String(kServerPort);
}

void setState(ScreenState state) {
  if (screenState != state) {
    screenState = state;
    dirty = true;
  }
}

int speakerVolume() {
  return (volumePercent * 255 + 50) / 100;
}

void setVolumePercent(int value) {
  const int next = constrain(value, 0, 100);
  if (next == volumePercent) return;
  volumePercent = next;
  Preferences preferences;
  if (preferences.begin("sashmo", false)) {
    preferences.putUChar("volume", static_cast<uint8_t>(volumePercent));
    preferences.end();
  }
  dirty = true;
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
  ui->fillScreen(0x0841);
  ui->fillRoundRect(8, 34, 304, 113, 12, 0x18E3);
  ui->setTextColor(0xBDF7, 0x0841);
  ui->setTextSize(2);
  ui->setCursor(10, 7);
  ui->print(clockText);
  ui->setTextSize(1);
  ui->setCursor(164, 14);
  ui->print(dateText);
  ui->setCursor(260, 14);
  if (!usbPowered && batteryLevel >= 0 && batteryLevel <= kLowBatteryPercent) {
    ui->setTextColor(0xFBE0, 0x0841);
  }
  if (!batteryPresent) {
    ui->print("NO BAT");
  } else if (batteryLevel >= 0) {
    ui->printf("%d%%", batteryLevel);
  } else {
    ui->print("--%");
  }
  if (charging) ui->print("+");
  if (!usbPowered && batteryLevel >= 0 && batteryLevel <= kLowBatteryPercent) {
    ui->setCursor(265, 25);
    ui->print("LOW BAT");
  }
}

void drawIdle() {
  ui->fillScreen(0x0841);
  ui->fillRoundRect(7, 5, 208, 69, 9, 0x18E3);
  ui->setTextColor(0xBDF7, 0x18E3);
  ui->setTextSize(1);
  ui->setCursor(16, 11);
  ui->print("CODEX LEFT");
  if (windowCount > 0 && windows[0].present) {
    ui->setTextColor(0x7FEF, 0x18E3);
    ui->setTextSize(4);
    ui->setCursor(15, 22);
    ui->printf("%d%%", windows[0].remaining);
    ui->setTextSize(1);
    ui->setTextColor(0xBDF7, 0x18E3);
    ui->setCursor(115, 32);
    ui->print(durationLabel(windows[0].durationMins));
  } else {
    ui->setTextSize(2);
    ui->setCursor(15, 30);
    ui->print("--%");
  }
  ui->setTextSize(1);
  ui->setTextColor(0xFFFF, 0x18E3);
  ui->setCursor(114, 57);
  ui->print("TAP TO TALK");
  ui->setTextColor(0xFFFF, 0x0841);
  ui->setTextSize(2);
  ui->setCursor(227, 15);
  ui->print(clockText);
  ui->setTextSize(1);
  ui->setCursor(226, 48);
  if (batteryLevel >= 0) ui->printf("BAT %d%%%s", batteryLevel, charging ? "+" : "");
  else ui->print("BAT --");

  ui->fillRoundRect(7, 79, 306, 80, 10, 0x41A3);
  ui->fillRoundRect(10, 82, 300, 74, 8, 0x20A2);
  ui->setTextColor(0xFBE0, 0x20A2);
  ui->setTextSize(1);
  ui->setCursor(18, 87);
  ui->printf("UPDATES %d  <", notificationCount);
  ui->setClipRect(13, 103, 294, 48);
  if (notificationItemsCount > 0) {
    const NotificationItem& item = notificationItems[tickerIndex % notificationItemsCount];
    const int x = 310 - tickerOffset;
    ui->setTextColor(0xFFE0, 0x20A2);
    ui->setTextSize(2);
    ui->setCursor(x, 106);
    ui->print(item.source + "  " + item.title);
    ui->setTextColor(0xFFFF, 0x20A2);
    ui->setTextSize(1);
    ui->setCursor(x, 132);
    ui->print(item.summary);
  } else {
    ui->setTextColor(0xFFFF, 0x20A2);
    ui->setTextSize(2);
    ui->setCursor(30, 116);
    ui->print("No new updates");
  }
  ui->clearClipRect();

  ui->setTextColor(0xBDF7, 0x0841);
  ui->setTextSize(1);
  ui->setCursor(12, 169);
  ui->printf("Wi-Fi %s    PC %s    BAT %s", WiFi.status() == WL_CONNECTED ? "OK" : "OFF",
                    serverConnected ? "OK" : "OFF", batteryLevel >= 0 ? String(batteryLevel).c_str() : "--");
  ui->setCursor(12, 188);
  ui->printf("MAIL %s   CAL %s   VOL %d%%", mailUnreadCount >= 0 ? String(mailUnreadCount).c_str() : "--",
                    calendarEventCount >= 0 ? String(calendarEventCount).c_str() : "--", volumePercent);
  ui->setCursor(12, 207);
  if (!apiSpent.isEmpty()) ui->printf("API $%s   ", apiSpent.c_str());
  ui->print("Tap ticker for details");
  ui->fillRoundRect(245, 213, 68, 24, 5, 0x18E3);
  ui->setTextColor(0xFFFF, 0x18E3);
  ui->setCursor(253, 221);
  ui->print("VOL +/-");
}

void drawVolume() {
  drawFrame();
  ui->setTextColor(0xFFFF, 0x18E3);
  ui->setTextSize(2);
  ui->setCursor(20, 48);
  ui->print("Volume");
  ui->setTextColor(0x7FEF, 0x18E3);
  ui->setTextSize(4);
  ui->setCursor(102, 84);
  ui->printf("%d%%", volumePercent);
  ui->fillRoundRect(227, 40, 75, 36, 8, 0x25F6);
  ui->setTextColor(0xFFFF, 0x25F6);
  ui->setTextSize(1);
  ui->setCursor(247, 54);
  ui->print("BACK");
  ui->fillRoundRect(20, 158, 128, 62, 10, 0x25F6);
  ui->fillRoundRect(172, 158, 128, 62, 10, 0x25F6);
  ui->setTextColor(0xFFFF, 0x25F6);
  ui->setTextSize(3);
  ui->setCursor(74, 175);
  ui->print("-");
  ui->setCursor(224, 175);
  ui->print("+");
}

void drawDetails() {
  drawFrame();
  ui->setTextColor(0xFFFF, 0x18E3);
  ui->setTextSize(2);
  ui->setCursor(20, 48);
  ui->print("Codex limits");
  ui->setTextSize(1);
  ui->setCursor(20, 80);
  ui->print("Reported plan: ");
  ui->println(planReported);
  if (windowCount == 0) {
    ui->setCursor(20, 110);
    ui->print("No usage window reported");
  }
  for (int i = 0; i < windowCount && i < 2; ++i) {
    const int y = 106 + i * 32;
    ui->setCursor(20, y);
    ui->printf("%s: %d%% left", durationLabel(windows[i].durationMins).c_str(), windows[i].remaining);
    ui->setCursor(20, y + 13);
    ui->print(resetLabel(windows[i].resetLocal));
  }
  ui->setTextColor(0xBDF7, 0x0841);
  ui->setCursor(20, 174);
  if (resetCredits >= 0) {
    ui->printf("Reset credits available: %d", resetCredits);
  }
  ui->setCursor(20, 190);
  ui->print(codexConnected ? "Codex connected" : "Codex unavailable");
  ui->setTextColor(0xBDF7, 0x0841);
  ui->setCursor(112, 216);
  ui->print("TAP TO RETURN");
}

void drawNotifications() {
  ui->fillScreen(0x0841);
  ui->setTextColor(0xFBE0, 0x0841);
  ui->setTextSize(2);
  ui->setCursor(10, 8);
  ui->printf("UPDATES %d", notificationCount);
  ui->setTextSize(1);
  ui->setCursor(163, 15);
  ui->print(notificationPaused ? "PAUSED" : "AUTO SCROLL");
  ui->fillRoundRect(280, 2, 38, 32, 5, 0xC124);
  ui->setTextColor(0xFFFF, 0xC124);
  ui->setTextSize(2);
  ui->setCursor(292, 8);
  ui->print("X");
  ui->setClipRect(0, 38, 320, 200);
  if (notificationItemsCount == 0) {
    ui->setTextColor(0xFFFF, 0x0841);
    ui->setTextSize(2);
    ui->setCursor(28, 105);
    ui->print("Nothing needs attention");
  }
  for (int i = 0; i < notificationItemsCount; ++i) {
    const int y = 42 + i * 112 - notificationScroll;
    if (y < -110 || y > 238) continue;
    ui->fillRoundRect(8, y, 304, 105, 8, 0x18E3);
    ui->setTextColor(0xFBE0, 0x18E3);
    ui->setTextSize(1);
    ui->setCursor(18, y + 9);
    ui->print(notificationItems[i].source);
    ui->setTextColor(0xFFFF, 0x18E3);
    ui->setTextSize(2);
    ui->setCursor(18, y + 26);
    ui->print(notificationItems[i].title.substring(0, 24));
    ui->setTextSize(1);
    ui->setCursor(18, y + 57);
    ui->print(notificationItems[i].summary.substring(0, 47));
    ui->setCursor(18, y + 73);
    ui->print(notificationItems[i].summary.substring(47, 94));
  }
  ui->clearClipRect();
}

void drawActivity(const char* title, const char* subtitle, uint16_t color) {
  drawFrame();
  ui->setTextColor(color, 0x18E3);
  ui->setTextSize(3);
  ui->setCursor(20, 65);
  ui->print(title);
  ui->setTextColor(0xFFFF, 0x0841);
  ui->setTextSize(2);
  ui->setCursor(20, 165);
  ui->print(subtitle);
  if (screenState == ScreenState::Speaking) {
    ui->fillRoundRect(228, 5, 84, 43, 9, 0xC124);
    ui->setTextColor(0xFFFF, 0xC124);
    ui->setTextSize(2);
    ui->setCursor(239, 18);
    ui->print("STOP");
    if (replyParts > 1) {
      ui->setTextColor(0xBDF7, 0x0841);
      ui->setTextSize(1);
      ui->setCursor(20, 199);
      ui->printf("Part %d of %d", replyPart, replyParts);
    }
  }
}

void render() {
  if (!dirty || displaySleeping) return;
  dirty = false;
  switch (screenState) {
    case ScreenState::Idle: drawIdle(); break;
    case ScreenState::Details: drawDetails(); break;
    case ScreenState::Notifications: drawNotifications(); break;
    case ScreenState::Volume: drawVolume(); break;
    case ScreenState::Listening: drawActivity("Listening", "Speak now", 0x7FEF); break;
    case ScreenState::Processing: drawActivity("Processing", "One moment", 0xFFE0); break;
    case ScreenState::Speaking: drawActivity("Speaking", "Playing reply", 0x7FEF); break;
    case ScreenState::Disconnected: drawActivity("Offline", "Reconnecting", 0xFBE0); break;
    case ScreenState::Error: drawActivity("Error", errorText.c_str(), 0xF800); break;
  }
  if (uiCanvasReady) uiCanvas.pushSprite(0, 0);
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
  if (sourceChanged && !nowUsb && !displaySleeping) {
    stopWakeListening();
    M5.Display.sleep();
    displaySleeping = true;
    dirty = true;
    Serial.println("DISPLAY off on battery");
  } else if (sourceChanged && nowUsb && displaySleeping) {
    wakeDisplay(millis());
  }
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
  if (usbPowered) return;
  const bool activeVoice = screenState == ScreenState::Listening
                        || screenState == ScreenState::Processing
                        || screenState == ScreenState::Speaking;
  if (!displaySleeping && !activeVoice
      && now - lastInteractionMs >= screenStandbyAfterSeconds * 1000UL) {
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
  if (deckRemoteEnabled) return usbPowered ? 4000UL : 12000UL;
  return (usbPowered ? kUsbPulseSeconds : kBatteryPulseSeconds) * 1000UL;
}

void parsePulse(const String& payload) {
  JsonDocument doc;
  if (deserializeJson(doc, payload)) {
    errorText = "Bad desktop data";
    setState(ScreenState::Error);
    return;
  }
  if (doc["wake_word_enabled"].is<bool>()) {
    const bool enabled = doc["wake_word_enabled"].as<bool>();
    if (enabled != wakeWordEnabled) {
      wakeWordEnabled = enabled;
      dirty = true;
      if (!enabled) stopWakeListening();
    }
  }
  deckRemoteEnabled = doc["deck_remote_enabled"] | false;
  if (doc["screen_standby_after_seconds"].is<uint32_t>()) {
    const uint32_t seconds = doc["screen_standby_after_seconds"].as<uint32_t>();
    if (seconds >= 60 && seconds <= 3600) screenStandbyAfterSeconds = seconds;
  }
  if (doc["api_billing"].is<JsonObject>()) {
    const String spent = String(doc["api_billing"]["spent_usd"] | "");
    const String left = String(doc["api_billing"]["budget_left_usd"] | "");
    if (spent != apiSpent || left != apiBudgetLeft) {
      apiSpent = spent;
      apiBudgetLeft = left;
      dirty = true;
    }
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
    notificationItemsCount = 0;
    notificationTitle = "";
    notificationSummary = "";
    for (JsonObject item : doc["notifications"].as<JsonArray>()) {
      if (notificationItemsCount >= 12) break;
      NotificationItem& target = notificationItems[notificationItemsCount++];
      target.source = String(item["source"] | "UPDATE");
      target.title = String(item["title"] | "");
      target.summary = String(item["summary"] | "");
      notificationTitle = String(item["title"] | "");
      notificationSummary = String(item["summary"] | "");
    }
    tickerIndex = 0;
    tickerOffset = 0;
    notificationScroll = 0;
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
  if (doc["deck_command"].is<JsonObject>()) {
    JsonObject command = doc["deck_command"].as<JsonObject>();
    const uint32_t seq = command["seq"] | 0;
    if (seq != 0 && String(command["kind"] | "") == "talk") {
      if (seq == lastRemoteTalkSeq) remoteTalkResult = "started";
      else if (pendingRemoteTalkSeq == 0) pendingRemoteTalkSeq = seq;
    }
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
  const bool sendingAck = lastRemoteTalkSeq != 0 && remoteTalkResult.length();
  const String requestUrl = sendingAck ? url + "&command_ack=" + String(lastRemoteTalkSeq)
      + "&command_result=" + remoteTalkResult : url;
  http.begin(requestUrl);
  http.setTimeout(5000);
  http.addHeader("X-Sashmo-Token", kDeviceToken);
  const int status = http.GET();
  if (status == 200) {
    const String payload = http.getString();
    serverConnected = true;
    retryMs = 5000;
    parsePulse(payload);
    if (sendingAck) remoteTalkResult = "";
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

ReplyPlayback playHttpReply(HTTPClient& http) {
  const int replySize = http.getSize();
  if (replySize < 44 || replySize > static_cast<int>(kMaxReplyBytes)) {
    http.end();
    failVoice("Reply too large");
    return ReplyPlayback::Failed;
  }
  uint8_t* reply = static_cast<uint8_t*>(heap_caps_malloc(replySize, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!reply) reply = static_cast<uint8_t*>(malloc(replySize));
  if (!reply) {
    http.end();
    failVoice("No reply memory");
    return ReplyPlayback::Failed;
  }
  WiFiClient* stream = http.getStreamPtr();
  size_t received = 0;
  const uint32_t readDeadline = millis() + 60000;
  while (received < static_cast<size_t>(replySize) && static_cast<int32_t>(readDeadline - millis()) > 0) {
    const int available = stream->available();
    if (available > 0) {
      const size_t wanted = min(static_cast<size_t>(available), static_cast<size_t>(replySize) - received);
      received += stream->readBytes(reply + received, wanted);
    } else {
      delay(10);
    }
  }
  http.end();
  if (received != static_cast<size_t>(replySize)) {
    free(reply);
    failVoice("Reply timeout");
    return ReplyPlayback::Failed;
  }

  dirty = true;
  setState(ScreenState::Speaking);
  render();
  M5.Speaker.setVolume(speakerVolume());
  if (!M5.Speaker.begin() || !M5.Speaker.playWav(reply, replySize, 1, 0, true)) {
    free(reply);
    failVoice("Speaker failed");
    return ReplyPlayback::Failed;
  }
  Serial.printf("SPEAKER playing part=%d/%d bytes=%d\n", replyPart, replyParts, replySize);
  bool stopped = false;
  while (M5.Speaker.isPlaying()) {
    M5.update();
    if (M5.Touch.getCount()) {
      const auto touch = M5.Touch.getDetail(0);
      if (touch.wasClicked() && touch.x >= 228 && touch.y <= 55) {
        stopped = true;
        M5.Speaker.stop();
        break;
      }
    }
    delay(10);
  }
  M5.Speaker.end();
  free(reply);
  return stopped ? ReplyPlayback::Stopped : ReplyPlayback::Completed;
}

void cancelReply(const String& replyId) {
  if (replyId.isEmpty() || WiFi.status() != WL_CONNECTED) return;
  HTTPClient cancel;
  cancel.begin(baseUrl() + "/v1/voice/reply/" + replyId);
  cancel.setTimeout(3000);
  cancel.addHeader("X-Sashmo-Token", kDeviceToken);
  cancel.sendRequest("DELETE");
  cancel.end();
}

bool pushToTalkOnce(bool followUp) {
  stopWakeListening();
  if (WiFi.status() != WL_CONNECTED || !serverConnected) {
    failVoice("Desktop offline");
    return false;
  }

  const size_t capacity = kWavHeaderBytes + kRecordSamples * sizeof(int16_t);
  uint8_t* wav = static_cast<uint8_t*>(heap_caps_malloc(capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!wav) wav = static_cast<uint8_t*>(malloc(capacity));
  if (!wav) { failVoice("No audio memory"); return false; }

  M5.Speaker.end();
  setState(ScreenState::Listening);
  render();
  const bool micStarted = M5.Mic.begin();
  if (!micStarted) {
    M5.Mic.end();
    free(wav);
    failVoice("Microphone failed");
    return false;
  }
  int16_t* samples = reinterpret_cast<int16_t*>(wav + kWavHeaderBytes);
  size_t recordedSamples = 0;
  int activeChunks = 0;
  int silentChunks = 0;
  bool captureFailed = false;
  while (recordedSamples + kRecordChunkSamples <= kRecordSamples) {
    if (!M5.Mic.record(samples + recordedSamples, kRecordChunkSamples, kSampleRate)) {
      captureFailed = true;
      break;
    }
    const uint32_t deadline = millis() + 2000;
    while (M5.Mic.isRecording() && static_cast<int32_t>(deadline - millis()) > 0) {
      M5.update();
      delay(10);
    }
    if (M5.Mic.isRecording()) {
      captureFailed = true;
      break;
    }
    uint32_t magnitude = 0;
    int peak = 0;
    for (size_t i = recordedSamples; i < recordedSamples + kRecordChunkSamples; ++i) {
      const int value = abs(static_cast<int>(samples[i]));
      magnitude += value;
      peak = max(peak, value);
    }
    recordedSamples += kRecordChunkSamples;
    if (peak >= 200 && magnitude / kRecordChunkSamples >= 30) {
      ++activeChunks;
      silentChunks = 0;
    } else if (activeChunks > 0) {
      ++silentChunks;
    }
    if (activeChunks > 0 && silentChunks >= kEndSilenceChunks) break;
    if (activeChunks == 0 && recordedSamples >= kStartTimeoutSamples) break;
  }
  M5.Mic.end();
  if (captureFailed) {
    free(wav);
    failVoice("Mic timeout");
    return false;
  }
  Serial.printf("MIC captured samples=%u active_chunks=%d\n",
                static_cast<unsigned>(recordedSamples), activeChunks);
  if (followUp && activeChunks < 2) {
    free(wav);
    setState(ScreenState::Idle);
    return false;
  }
  makeWavHeader(wav, recordedSamples);
  const size_t wavBytes = kWavHeaderBytes + recordedSamples * sizeof(int16_t);
  setState(ScreenState::Processing);
  render();

  HTTPClient http;
  http.begin(baseUrl() + "/v1/voice");
  http.setTimeout(60000);
  const char* responseHeaders[] = {"X-Sashmo-Volume-Percent", "X-Sashmo-Memory",
                                   "X-Sashmo-Error", "X-Sashmo-Reply-Id", "X-Sashmo-More",
                                   "X-Sashmo-Part", "X-Sashmo-Parts"};
  http.collectHeaders(responseHeaders, 7);
  http.addHeader("X-Sashmo-Token", kDeviceToken);
  http.addHeader("Content-Type", "audio/wav");
  if (!conversationMemory.isEmpty()) http.addHeader("X-Sashmo-Memory", conversationMemory);
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
    const String replyError = http.header("X-Sashmo-Error");
    http.end();
    failVoice(replyError.isEmpty() ? "Voice service error" : replyError.c_str());
    return false;
  }

  const String requestedVolume = http.header("X-Sashmo-Volume-Percent");
  if (!requestedVolume.isEmpty()) {
    const int requested = requestedVolume.toInt();
    if (requested >= 0 && requested <= 100) setVolumePercent(requested);
  }
  const String newMemory = http.header("X-Sashmo-Memory");
  if (!newMemory.isEmpty() && newMemory.length() <= 900 && newMemory != conversationMemory) {
    conversationMemory = newMemory;
    Preferences preferences;
    if (preferences.begin("sashmo", false)) {
      preferences.putString("memory", conversationMemory);
      preferences.end();
    }
  }
  String replyId = http.header("X-Sashmo-Reply-Id");
  String more = http.header("X-Sashmo-More");
  replyPart = max(1, static_cast<int>(http.header("X-Sashmo-Part").toInt()));
  replyParts = max(1, static_cast<int>(http.header("X-Sashmo-Parts").toInt()));
  ReplyPlayback playback = playHttpReply(http);
  if (playback != ReplyPlayback::Completed) {
    cancelReply(replyId);
    setState(ScreenState::Idle);
    return false;
  }
  while (more == "1") {
    if (replyId.isEmpty() || replyId.length() > 64) {
      failVoice("Reply lost");
      return false;
    }
    HTTPClient next;
    next.begin(baseUrl() + "/v1/voice/reply/" + replyId);
    next.setTimeout(60000);
    const char* nextHeaders[] = {"X-Sashmo-More", "X-Sashmo-Part", "X-Sashmo-Parts", "X-Sashmo-Error"};
    next.collectHeaders(nextHeaders, 4);
    next.addHeader("X-Sashmo-Token", kDeviceToken);
    const int nextStatus = next.GET();
    if (nextStatus != 200) {
      const String nextError = next.header("X-Sashmo-Error");
      next.end();
      cancelReply(replyId);
      failVoice(nextError.isEmpty() ? "Reply unavailable" : nextError.c_str());
      return false;
    }
    more = next.header("X-Sashmo-More");
    replyPart = max(1, static_cast<int>(next.header("X-Sashmo-Part").toInt()));
    replyParts = max(1, static_cast<int>(next.header("X-Sashmo-Parts").toInt()));
    playback = playHttpReply(next);
    if (playback != ReplyPlayback::Completed) {
      cancelReply(replyId);
      setState(ScreenState::Idle);
      return false;
    }
  }
  replyPart = 1;
  replyParts = 1;
  setState(ScreenState::Idle);
  nextPulseMs = millis();
  return true;
}

void pushToTalk() {
  for (int turn = 0; turn < 8; ++turn) {
    if (turn > 0) delay(250);  // Let speaker output settle before listening again.
    if (!pushToTalkOnce(turn > 0)) break;
    if (!wakeWordEnabled) break;
  }
  lastInteractionMs = millis();
  nextPulseMs = millis();
}

void pollWakeWord(uint32_t now) {
  const bool allowedPower = usbPowered;
  const bool shouldListen = wakeWordEnabled && allowedPower && serverConnected
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
    M5.Speaker.setVolume(speakerVolume() * 3 / 4);
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
  uiCanvas.setColorDepth(16);
  uiCanvasReady = uiCanvas.createSprite(M5.Display.width(), M5.Display.height()) != nullptr;
  if (uiCanvasReady) ui = &uiCanvas;
  else Serial.println("Off-screen display buffer unavailable");
  M5.Speaker.end();
  M5.Mic.end();
  Preferences preferences;
  if (preferences.begin("sashmo", true)) {
    volumePercent = min(100, static_cast<int>(preferences.getUChar("volume", 63)));
    conversationMemory = preferences.getString("memory", "");
    lastRemoteTalkSeq = preferences.getUInt("deckseq", 0);
    if (conversationMemory.length() > 900) conversationMemory = "";
    preferences.end();
  }
  readPower();
  lastInteractionMs = millis();
  applyPowerProfile();
  Serial.printf("SASHMO boot battery=%d%% battery_mv=%d present=%d charging=%d vbus=%d mV\n",
                batteryLevel, M5.Power.getBatteryVoltage(), M5.Power.Axp2101.getBatState(),
                charging, M5.Power.getVBUSVoltage());
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  wifiMulti.addAP(kWifiSsid, kWifiPassword);
  if (kWifiFallbackSsid[0]) wifiMulti.addAP(kWifiFallbackSsid, kWifiPassword);
  WiFi.begin(kWifiSsid, kWifiPassword);
  lastWifiAttemptMs = millis();
  drawSplash();
  delay(1600);
  render();
}

void loop() {
  M5.update();
  const uint32_t now = millis();

  if (now - lastPowerReadMs >= 1000) {
    lastPowerReadMs = now;
    readPower();
    applyPowerProfile();
    // Power transitions should blank or restore the screen promptly. The
    // periodic battery report comes from the authenticated Wi-Fi pulse.
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
      wifiMulti.run(5000);
      lastWifiAttemptMs = now;
      Serial.println("Wi-Fi reconnecting");
    }
    serverConnected = false;
    setState(ScreenState::Disconnected);
  } else if (static_cast<int32_t>(now - nextPulseMs) >= 0) {
    fetchPulse();
  }

  if (pendingRemoteTalkSeq != 0) {
    const uint32_t seq = pendingRemoteTalkSeq;
    pendingRemoteTalkSeq = 0;
    lastRemoteTalkSeq = seq;
    const bool ready = serverConnected && screenState == ScreenState::Idle;
    remoteTalkResult = ready ? "started" : "busy";
    Preferences preferences;
    if (preferences.begin("sashmo", false)) {
      preferences.putUInt("deckseq", seq);
      preferences.end();
    }
    if (ready) {
      stopWakeListening();
      wakeDisplay(millis());
      pushToTalk();
    }
    nextPulseMs = millis();
  }

  if (M5.Touch.getCount()) {
    const auto touch = M5.Touch.getDetail(0);
    if (touch.wasClicked()) {
      if (displaySleeping) {
        wakeDisplay(now);
        render();
        return;
      }
      lastInteractionMs = now;
      applyPowerProfile();
      Serial.printf("TOUCH x=%d y=%d\n", touch.x, touch.y);
      if (screenState == ScreenState::Details) {
        setState(ScreenState::Idle);
      } else if (screenState == ScreenState::Notifications) {
        if (touch.x >= 278 && touch.y <= 38) setState(ScreenState::Idle);
        else { notificationPaused = !notificationPaused; dirty = true; }
      } else if (screenState == ScreenState::Volume) {
        if (touch.y >= 158 && touch.x <= 150) setVolumePercent(volumePercent - 10);
        else if (touch.y >= 158 && touch.x >= 170) setVolumePercent(volumePercent + 10);
        else if (touch.y >= 34 && touch.y <= 85 && touch.x >= 220) setState(ScreenState::Idle);
      } else if (screenState == ScreenState::Idle && touch.y >= 79 && touch.y <= 159) {
        notificationScroll = 0;
        notificationPaused = false;
        setState(ScreenState::Notifications);
      } else if (screenState == ScreenState::Idle && touch.y <= 75 && touch.x <= 215) {
        pushToTalk();
      } else if (screenState == ScreenState::Idle && touch.y >= 210 && touch.x >= 240) {
        setState(ScreenState::Volume);
      } else if (screenState == ScreenState::Idle && touch.y >= 160 && touch.y < 210) {
        setState(ScreenState::Details);
      }
    }
  }
  if (!displaySleeping && now >= nextAnimationMs) {
    nextAnimationMs = now + 100;
    if (screenState == ScreenState::Idle && notificationItemsCount > 0) {
      tickerOffset += 3;
      const NotificationItem& item = notificationItems[tickerIndex % notificationItemsCount];
      const int titleWidth = 12 * (item.source.length() + item.title.length() + 2);
      const int summaryWidth = 6 * item.summary.length();
      if (tickerOffset > 320 + max(titleWidth, summaryWidth)) {
        tickerOffset = 0;
        tickerIndex = (tickerIndex + 1) % notificationItemsCount;
      }
      dirty = true;
    } else if (screenState == ScreenState::Notifications && !notificationPaused
               && notificationItemsCount > 0) {
      notificationScroll += 1;
      if (notificationScroll > notificationItemsCount * 112 + 190) notificationScroll = 0;
      dirty = true;
    }
  }
  updateDisplayStandby(now);
  pollWakeWord(now);
  applyPowerProfile();
  render();
  delay(20);
}
