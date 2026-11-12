#pragma once
#include <array>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>
#define INPUT 1
#define OUTPUT 2
#define LOW 0
#define HIGH 1
extern uint32_t fake_now;
extern std::array<int, 64> fake_levels;
extern std::array<int, 64> fake_modes;
extern std::vector<std::pair<int, int>> fake_writes;
inline uint32_t millis() { return fake_now; }
inline void pinMode(int pin, int mode) { fake_modes.at(pin) = mode; }
inline void digitalWrite(int pin, int value) {
  assert(fake_modes.at(pin) == OUTPUT); // Arduino 3.x ignores writes before pinMode.
  fake_writes.emplace_back(pin, value);
}
inline int digitalRead(int pin) { return fake_levels.at(pin); }
struct FakeSerial {
  std::string input;
  std::string output;
  size_t written = 0;
  void begin(int) {}
  int available() { return input.size(); }
  char read() { char value = input.front(); input.erase(0, 1); return value; }
  size_t write(const uint8_t*, size_t size) { written += size; return size; }
  void println(const char* value) { output += std::string(value) + "\n"; }
  template <typename... Args> void printf(const char* format, Args... args) {
    char text[512];
    std::snprintf(text, sizeof(text), format, args...);
    output += text;
  }
};
inline void delay(uint32_t ms) { fake_now += ms; }
using BaseType_t = int;
constexpr int pdPASS = 1;
extern void (*fake_pending_task)(void*);
inline int xTaskCreate(void (*task)(void*), const char*, int, void*, int, void*) {
  fake_pending_task = task;
  return pdPASS;
}
inline void vTaskDelete(void*) {}
extern FakeSerial Serial;
