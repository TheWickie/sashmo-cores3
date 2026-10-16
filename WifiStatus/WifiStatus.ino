#include <Arduino.h>
#include <M5Unified.h>
#include <WiFi.h>

#include "wifi_credentials.h"

constexpr unsigned long kRetryIntervalMs = 15000;
constexpr unsigned long kDisplayIntervalMs = 1000;

unsigned long lastAttemptMs = 0;
unsigned long lastDisplayMs = 0;
wl_status_t previousStatus = WL_IDLE_STATUS;

void drawStatus() {
  M5.Display.fillScreen(BLACK);
  M5.Display.setTextColor(WHITE, BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(12, 12);
  M5.Display.println("CoreS3 Wi-Fi");
  M5.Display.println();
  M5.Display.println(kWifiSsid);
  M5.Display.println();

  if (WiFi.status() == WL_CONNECTED) {
    M5.Display.setTextColor(GREEN, BLACK);
    M5.Display.println("Connected");
    M5.Display.setTextColor(WHITE, BLACK);
    M5.Display.println(WiFi.localIP().toString());
    M5.Display.printf("Signal: %d dBm\n", WiFi.RSSI());
  } else {
    M5.Display.setTextColor(YELLOW, BLACK);
    M5.Display.println("Connecting...");
    M5.Display.setTextColor(WHITE, BLACK);
    M5.Display.printf("Status: %d\n", static_cast<int>(WiFi.status()));
  }
}

void startConnection() {
  lastAttemptMs = millis();
  Serial.printf("Connecting to %s\n", kWifiSsid);
  WiFi.begin(kWifiSsid, kWifiPassword);
  drawStatus();
}

void setup() {
  auto config = M5.config();
  M5.begin(config);
  Serial.begin(115200);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  startConnection();
}

void loop() {
  M5.update();
  const wl_status_t status = WiFi.status();

  if (status != previousStatus) {
    previousStatus = status;
    if (status == WL_CONNECTED) {
      Serial.printf("Connected. IP: %s; RSSI: %d dBm\n",
                    WiFi.localIP().toString().c_str(), WiFi.RSSI());
    } else {
      Serial.printf("Wi-Fi status: %d\n", static_cast<int>(status));
    }
    drawStatus();
  }

  if (status != WL_CONNECTED && millis() - lastAttemptMs >= kRetryIntervalMs) {
    startConnection();
  }

  if (millis() - lastDisplayMs >= kDisplayIntervalMs) {
    lastDisplayMs = millis();
    drawStatus();
  }

  delay(50);
}
