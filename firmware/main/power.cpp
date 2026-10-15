#include "power.h"
#include <Arduino.h>
#include <Wire.h>
#include "board.h"

static constexpr uint8_t MAX17048_ADDR = 0x36;
static constexpr uint32_t SAMPLE_MS = 5000;
static constexpr uint32_t STALE_MS = 10000;
static constexpr uint32_t DEBOUNCE_MS = 30;
static constexpr uint32_t LONG_PRESS_MS = 2000;

static PowerStatus status;
static bool started = false;
static bool bus_ready = false;
static bool shutdown_requested = false;
static bool latch_released = false;
static uint32_t last_attempt_ms = 0;
static bool raw_pressed = false;
static bool stable_pressed = false;
static bool button_armed = false;
static uint32_t raw_changed_at = 0;
static uint32_t press_at = 0;

static bool read_register(uint8_t reg, uint16_t& value) {
  Wire.beginTransmission(MAX17048_ADDR);
  if (Wire.write(reg) != 1) {
    Wire.endTransmission(); // Release
    return false;
  }
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(MAX17048_ADDR, uint8_t(2)) != 2 || Wire.available() != 2) {
    while (Wire.available()) Wire.read();
    return false;
  }
  int high = Wire.read();
  int low = Wire.read();
  if (high < 0 || low < 0) return false;
  value = (uint16_t(high) << 8) | uint16_t(low);
  return true;
}

static void read_battery() {
  uint16_t soc, vcell;
  status.battery_valid = bus_ready &&
      read_register(0x04, soc) && read_register(0x02, vcell);
  if (status.battery_valid) {
    // Commit only a complete pair
    status.battery_percent = float(soc) / 256.0f;
    status.battery_voltage = float(vcell) * 0.000078125f;
    status.last_success_ms = millis();
  }
  Serial.printf("power: valid=%u battery=%.1f%% voltage=%.3fV last_ok_ms=%lu\n",
                status.battery_valid, status.battery_percent, status.battery_voltage,
                static_cast<unsigned long>(status.last_success_ms));
}

static void read_button(uint32_t now) {
  bool pressed = digitalRead(POWER_SENSE) == LOW;
  if (pressed != raw_pressed) {
    raw_pressed = pressed;
    raw_changed_at = now;
  }
  if (uint32_t(now - raw_changed_at) < DEBOUNCE_MS) return;
  if (!button_armed) {
    if (!raw_pressed) button_armed = true;
    stable_pressed = raw_pressed;
    return;
  }
  if (stable_pressed != raw_pressed) {
    stable_pressed = raw_pressed;
    if (stable_pressed) press_at = now;
  }
  if (stable_pressed && raw_pressed && uint32_t(now - press_at) >= LONG_PRESS_MS) {
    shutdown_requested = true;
    Serial.println("power: shutdown requested");
  }
}

void power_start() {
  if (!POWER_ENABLED || started) return;
  pinMode(POWER_HOLD, OUTPUT);
  digitalWrite(POWER_HOLD, HIGH);
  started = true;
  pinMode(POWER_SENSE, INPUT);
  pinMode(POWER_FG_ALRT, INPUT);
  pinMode(POWER_CHG_STAT, INPUT);
  pinMode(POWER_PGOOD, INPUT);
  raw_pressed = digitalRead(POWER_SENSE) == LOW;
  stable_pressed = raw_pressed;
  raw_changed_at = millis();
  bus_ready = Wire.begin(POWER_SDA, POWER_SCL);
  if (bus_ready) Wire.setTimeOut(20); 
  last_attempt_ms = millis() - SAMPLE_MS; 
}

bool power_update() {
  if (!POWER_ENABLED || !started) return false;
  if (shutdown_requested) return true;
  uint32_t now = millis();
  bool external = digitalRead(POWER_PGOOD) == LOW;
  bool charging = digitalRead(POWER_CHG_STAT) == LOW;
  status.charger = external ? (charging ? PowerChargerState::Charging : PowerChargerState::Inactive)
                            : (charging ? PowerChargerState::Invalid : PowerChargerState::ExternalAbsent);
  status.fuel_gauge_alert = digitalRead(POWER_FG_ALRT) == LOW;
  read_button(now);
  if (!shutdown_requested && uint32_t(now - last_attempt_ms) >= SAMPLE_MS) {
    last_attempt_ms = now;
    read_battery();
  }
  return shutdown_requested;
}

PowerStatus power_status() {
  PowerStatus result = status;
  result.battery_valid = result.battery_valid &&
      uint32_t(millis() - result.last_success_ms) < STALE_MS;
  return result;
}

void power_finish_shutdown() {
  if (!POWER_ENABLED || !started || !shutdown_requested || latch_released) return;
  digitalWrite(POWER_HOLD, LOW);
  latch_released = true;
  Serial.println("power: latch released; staying stopped if external power remains");
}
