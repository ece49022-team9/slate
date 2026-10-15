#include <Arduino.h>
#include <Wire.h>
#include <cassert>
#include "board.h"
#include "power.h"
#include "slate_state.h"
#include "mic.h"

uint32_t fake_now = 0;
std::array<int, 64> fake_levels;
std::array<int, 64> fake_modes;
std::vector<std::pair<int, int>> fake_writes;
FakeSerial Serial;
FakeWire Wire;
void (*fake_pending_task)(void*) = nullptr;
static bool capture_done = false;
static bool display_done = false;
static bool speaker_ok = true;
static int capture_stops = 0, display_stops = 0, speaker_stops = 0;
static bool producer_done = false;
static bool expect_producer = false;
static void check_startup_hold() {
  if (POWER_ENABLED) assert(fake_writes.size() == 1 && fake_writes[0].second == HIGH);
  else assert(fake_writes.empty());
  assert(Wire.transactions == 0); // No gauge reads during startup.
  assert(Wire.begins == (POWER_ENABLED ? 1 : 0));
  if (POWER_ENABLED) assert(Wire.sda == 21 && Wire.scl == 22);
}
void mic_start() { check_startup_hold(); }
void slate_start() { check_startup_hold(); }
void pdm_start() { check_startup_hold(); }
void oled_start() { check_startup_hold(); }
void speaker_start() { check_startup_hold(); }
bool pdm_stop() { ++capture_stops; return capture_done; }
bool oled_stop() { ++display_stops; return display_done; }
bool speaker_stop() {
  if (expect_producer) assert(producer_done);
  ++speaker_stops;
  return speaker_ok;
}
void mic_set_state(SlateState state) { assert(state == IDLE && capture_done); }
bool mic_configure(MicChannel) { return true; }
size_t mic_read(int16_t*, size_t) { return 0; }
bool slate_request_state(SlateState) { return true; }
SlateState slate_get_state() { return IDLE; }
void speaker_test() { producer_done = true; }
void setup();
void loop();
static void tick(uint32_t now) { fake_now = now; loop(); }
static void press_shutdown() {
  tick(30);
  fake_levels[POWER_SENSE] = LOW;
  tick(100); tick(130); tick(2130);
}
int main(int argc, char** argv) {
  assert(argc == 2);
  std::string test = argv[1];
  fake_levels.fill(HIGH); fake_modes.fill(-1);
  setup();
  if (!POWER_ENABLED) {
    press_shutdown();
    assert(fake_writes.empty() && capture_stops == 0 && speaker_stops == 0);
    return 0;
  }
  if (test == "producer") {
    expect_producer = true;
    Serial.input = "t"; tick(0);
    assert(fake_pending_task && !producer_done);
  }
  if (test == "speaker-failure") speaker_ok = false;
  press_shutdown();
#ifndef SLATE_SPEAKER_TEST
  assert(capture_stops > 0 && display_stops > 0);
  assert(speaker_stops == 0 && fake_writes.size() == 1);
  // Inputs queued after the request must not resume normal operation.
  Serial.input = "t1a";
  tick(2200); assert(Serial.input == "t1a");
  if (test == "timeout") {
    tick(5130); tick(6000);
    assert(fake_writes.size() == 1);
    assert(Serial.output.find("cleanup timed out; power retained") != std::string::npos);
    capture_done = display_done = true;
    tick(7000); assert(fake_writes.size() == 1 && speaker_stops == 0);
    return 0;
  }
  capture_done = true;
  tick(2300); assert(speaker_stops == 0 && fake_writes.size() == 1);
  display_done = true;
  if (test == "producer") {
    tick(2400); assert(speaker_stops == 0);
    fake_pending_task(nullptr); // Let the producer finish before teardown.
  }
  tick(2500);
#endif
  if (test == "speaker-failure") {
    assert(fake_writes.size() == 1);
    assert(Serial.output.find("speaker cleanup failed; power retained") != std::string::npos);
  } else {
    assert(fake_writes.size() == 2 && fake_writes.back().second == LOW);
    assert(speaker_stops == 1);
  }
  tick(10000); tick(20000);
  assert(speaker_stops == 1); // No repeated cleanup or cutoff on a retained supply.
}
