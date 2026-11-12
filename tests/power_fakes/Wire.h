#pragma once
#include <cassert>
#include <cstddef>
#include <cstdint>
struct FakeWire {
  bool bus_ok = true;
  bool initialized = false;
  bool in_transaction = false;
  bool present = true;
  bool pointer_ok = true;
  bool write_ok = true;
  bool negative_byte = false;
  uint16_t soc = 50 * 256;
  uint16_t voltage = 0xC800; // 4.000 V.
  int soc_length = 2;
  int voltage_length = 2;
  int length = 0;
  int consumed = 0;
  int reg = -1;
  int begins = 0;
  int sda = -1;
  int scl = -1;
  int timeout = 0;
  int transactions = 0;
  bool begin(int data = -1, int clock = -1) {
    if (initialized) return true; // Arduino Wire.begin() from Adafruit BusIO is idempotent.
    assert(!fake_writes.empty() && fake_writes.back().first == 13 &&
           fake_writes.back().second == HIGH); // HOLD precedes bus initialization.
    ++begins; sda = data; scl = clock;
    initialized = bus_ok;
    return bus_ok;
  }
  void setTimeOut(uint16_t ms) { timeout = ms; }
  void beginTransmission(uint8_t addr) {
    assert(!in_transaction && addr == 0x36);
    in_transaction = true; reg = -1;
  }
  size_t write(uint8_t value) { reg = value; return write_ok ? 1 : 0; }
  int endTransmission(bool stop = true) {
    ++transactions;
    int error = present && (reg < 0 || pointer_ok) ? 0 : 2;
    if (stop || error) in_transaction = false;
    return error;
  }
  uint8_t requestFrom(uint8_t addr, uint8_t count) {
    assert(in_transaction && addr == 0x36 && count == 2);
    in_transaction = false;
    ++transactions;
    length = reg == 4 ? soc_length : voltage_length;
    consumed = 0;
    return length;
  }
  int available() { return length - consumed; }
  int read() {
    ++consumed;
    if (negative_byte) return -1;
    uint16_t value = reg == 4 ? soc : voltage;
    return consumed == 1 ? value >> 8 : value & 255;
  }
};
extern FakeWire Wire;
