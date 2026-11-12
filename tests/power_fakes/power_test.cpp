#include <Arduino.h>
#include <Wire.h>
#include <cassert>
#include <cmath>
#include <string>
#include "board.h"
#include "power.h"

uint32_t fake_now = 0;
std::array<int, 64> fake_levels;
std::array<int, 64> fake_modes;
std::vector<std::pair<int, int>> fake_writes;
FakeSerial Serial;
FakeWire Wire;
static bool tick(uint32_t now) { fake_now = now; return power_update(); }
static void sample(uint32_t now, float percent) {
  Wire.soc = uint16_t(percent * 256);
  tick(now);
}
static void request_shutdown() {
  tick(fake_now + 30); // Arm after a stable startup release.
  fake_levels[POWER_SENSE] = LOW;
  tick(fake_now + 1);
  tick(fake_now + 30);
  assert(!tick(fake_now + 1999));
  assert(tick(fake_now + 1));
}

int main(int argc, char** argv) {
  assert(argc == 2);
  std::string test = argv[1];
  fake_levels.fill(HIGH);
  fake_modes.fill(-1);
  if (test == "boot-held") fake_levels[POWER_SENSE] = LOW;
  if (test == "missing") Wire.present = false;
  if (test == "bus-failure") Wire.bus_ok = false;
  if (test == "wrap" || test == "button-wrap") fake_now = UINT32_MAX - 100;
  power_finish_shutdown(); // No request, so never release.
  assert(fake_writes.empty());
  power_start();
  power_start();
  assert(Wire.transactions == 0); // HOLD comes before bus operations.
#if !POWER_ENABLED
  assert(!tick(5000));
  power_finish_shutdown();
  assert(Wire.begins == 0 && fake_writes.empty());
  for (int mode : fake_modes) assert(mode == -1);
  assert(std::isnan(power_status().battery_percent));
  assert(!power_status().battery_valid && power_status().charger == PowerChargerState::Unknown);
  return 0;
#else
  assert(fake_modes[POWER_SENSE] == INPUT);
  assert(fake_modes[POWER_FG_ALRT] == INPUT);
  assert(fake_modes[POWER_CHG_STAT] == INPUT);
  assert(fake_modes[POWER_PGOOD] == INPUT);
  assert(fake_modes[POWER_HOLD] == OUTPUT);
  assert(fake_writes.size() == 1 && fake_writes[0].second == HIGH);
  if (test == "bus-failure") {
    assert(Wire.begins == 1 && Wire.transactions == 0);
    tick(0); tick(5000);
    assert(!power_status().battery_valid && std::isnan(power_status().battery_percent));
    request_shutdown(); // A failed bus must not prevent the shutdown button working.
    assert(fake_writes.size() == 1);
    return 0;
  }
  if (test == "partial-first") Wire.voltage_length = 1;
  tick(fake_now);
  if (test == "missing" || test == "partial-first") {
    assert(!power_status().battery_valid);
    assert(std::isnan(power_status().battery_percent) && std::isnan(power_status().battery_voltage));
    Wire.present = true; Wire.bus_ok = true; Wire.voltage_length = 2;
    sample(5000, 42);
    assert(power_status().battery_valid && power_status().battery_percent == 42);
  } else {
    auto first = power_status();
    assert(first.battery_valid && first.battery_percent == 50 && first.battery_voltage == 4);
    assert(first.last_success_ms == fake_now);
    assert(Wire.sda == POWER_SDA && Wire.scl == POWER_SCL && Wire.timeout == 20);
    assert(Wire.begins == 1);
    if (test == "good") {
      tick(4999); assert(power_status().last_success_ms == 0);
      sample(5000, 60); assert(power_status().battery_percent == 60);
    } else if (test == "partial" || test == "pointer-fail" || test == "write-fail" ||
               test == "negative-byte" || test == "missing-later" || test == "soc-partial") {
      if (test == "partial") Wire.voltage_length = 1;
      if (test == "soc-partial") Wire.soc_length = 1;
      if (test == "pointer-fail") Wire.pointer_ok = false;
      if (test == "write-fail") Wire.write_ok = false;
      if (test == "negative-byte") Wire.negative_byte = true;
      if (test == "missing-later") Wire.present = false;
      sample(5000, 0);
      auto failed = power_status();
      assert(!failed.battery_valid && failed.battery_percent == first.battery_percent);
      assert(failed.battery_voltage == first.battery_voltage && failed.last_success_ms == first.last_success_ms);
      assert(!power_update()); // Communication failure never requests shutdown.
      Wire.voltage_length = 2; Wire.soc_length = 2; Wire.pointer_ok = true; Wire.write_ok = true;
      Wire.negative_byte = false; Wire.present = true;
      sample(10000, 48); assert(power_status().battery_valid && power_status().battery_percent == 48);
    } else if (test == "fractional-register") {
      Wire.voltage = 0xC801;
      sample(5000, 50.5f);
      assert(power_status().battery_valid && power_status().battery_percent == 50.5f);
      assert(std::abs(power_status().battery_voltage - 4.000078125f) < 0.000001f);
    } else if (test == "stale") {
      fake_now = 10000; // Snapshot freshness works even without update calls.
      assert(!power_status().battery_valid && power_status().battery_percent == 50);
      sample(10000, 45); assert(power_status().battery_valid);
    } else if (test == "charger") {
      assert(power_status().charger == PowerChargerState::ExternalAbsent);
      fake_levels[POWER_PGOOD] = LOW;
      tick(1); assert(power_status().charger == PowerChargerState::Inactive);
      fake_levels[POWER_CHG_STAT] = LOW;
      tick(2); assert(power_status().charger == PowerChargerState::Charging);
      fake_levels[POWER_PGOOD] = HIGH;
      tick(3); assert(power_status().charger == PowerChargerState::Invalid);
      fake_levels[POWER_CHG_STAT] = HIGH;
      fake_levels[POWER_FG_ALRT] = LOW;
      tick(4); assert(power_status().charger == PowerChargerState::ExternalAbsent);
      assert(power_status().fuel_gauge_alert);
    } else if (test == "short" || test == "bounce" || test == "release-edge") {
      tick(30);
      fake_levels[POWER_SENSE] = LOW; tick(100);
      if (test == "bounce") {
        fake_levels[POWER_SENSE] = HIGH; tick(110);
        fake_levels[POWER_SENSE] = LOW; tick(120);
        assert(!tick(149));
        fake_levels[POWER_SENSE] = HIGH; tick(150); assert(!tick(180));
      } else {
        tick(130);
        uint32_t release = test == "short" ? 500 : 2129;
        fake_levels[POWER_SENSE] = HIGH;
        assert(!tick(release)); assert(!tick(release + 30));
      }
    } else if (test == "long" || test == "button-wrap" || test == "release") {
      request_shutdown();
      assert(power_update());
      assert(fake_writes.size() == 1); // Request alone never lowers HOLD.
      int transactions = Wire.transactions;
      if (test == "release") {
        power_finish_shutdown(); power_finish_shutdown();
        assert(fake_writes.size() == 2 && fake_writes.back().second == LOW);
      }
      assert(tick(fake_now + 10000)); // Retained supply: remain quiescent.
      assert(Wire.transactions == transactions);
    } else if (test == "boot-held") {
      assert(!tick(3000));
      fake_levels[POWER_SENSE] = HIGH; tick(3100); assert(!tick(3130));
      request_shutdown();
    } else if (test == "wrap") {
      uint32_t first_time = fake_now;
      sample(fake_now + 5000, 45);
      assert(power_status().battery_valid && uint32_t(fake_now - first_time) == 5000);
    } else if (test == "zero-reading") {
      Wire.voltage = 0;
      sample(5000, 0);
      assert(power_status().battery_valid && power_status().battery_percent == 0);
      assert(power_status().battery_voltage == 0 && !power_update());
    } else {
      assert(false && "unknown test scenario");
    }
  }
  if (test != "release") assert(fake_writes.size() == 1);
#endif
}
