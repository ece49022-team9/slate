#pragma once
#include <math.h>
#include <stdint.h>

enum class PowerChargerState : uint8_t {
  Unknown, ExternalAbsent, Charging, Inactive, Invalid
};

struct PowerStatus {
  float battery_percent = NAN;
  float battery_voltage = NAN;
  bool battery_valid = false; // Last attempt succeeded and is less than 10 s old.
  uint32_t last_success_ms = 0;
  PowerChargerState charger = PowerChargerState::Unknown;
  bool fuel_gauge_alert = false;
};

// Application-task APIs. Disabled boards never access power hardware.
void power_start(); // Call first in setup(): asserts HOLD before peripheral startup.
bool power_update(); // True means shutdown requested; remains true after cutoff.
PowerStatus power_status(); // Retains last-good values even when invalid/stale.
void power_finish_shutdown(); // Call ONLY after all peripheral cleanup succeeds.
