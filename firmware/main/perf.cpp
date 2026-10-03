#include <Arduino.h>
#include <string.h>
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "perf.h"

struct Window {
  uint32_t count;
  uint64_t total;
  uint32_t max;
};

static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static Window windows[size_t(PerfTimer::COUNT)];
static uint32_t stacks[size_t(PerfTask::COUNT)];

void perf_time(PerfTimer timer, uint32_t microseconds) {
  portENTER_CRITICAL(&lock);
  Window& window = windows[size_t(timer)];
  ++window.count;
  window.total += microseconds;
  if (microseconds > window.max) window.max = microseconds;
  portEXIT_CRITICAL(&lock);
}

void perf_stack(PerfTask task) {
  uint32_t remaining = uxTaskGetStackHighWaterMark(nullptr);
  portENTER_CRITICAL(&lock);
  uint32_t& lowest = stacks[size_t(task)];
  if (!lowest || remaining < lowest) lowest = remaining;
  portEXIT_CRITICAL(&lock);
}

static unsigned long named_stack(const char* name) {
  TaskHandle_t task = xTaskGetHandle(name);
  return task ? uxTaskGetStackHighWaterMark(task) : 0;
}

static unsigned long average(const Window& window) {
  return window.count ? static_cast<unsigned long>(window.total / window.count) : 0;
}

void perf_report() {
  Window timers[size_t(PerfTimer::COUNT)];
  uint32_t remaining[size_t(PerfTask::COUNT)];
  portENTER_CRITICAL(&lock);
  memcpy(timers, windows, sizeof(timers));
  memcpy(remaining, stacks, sizeof(remaining));
  memset(windows, 0, sizeof(windows));
  portEXIT_CRITICAL(&lock);
  const Window& render = timers[size_t(PerfTimer::RENDER)];
  const Window& spi = timers[size_t(PerfTimer::SPI)];
  const Window& audio = timers[size_t(PerfTimer::AUDIO)];
  Serial.printf(
      "slate.perf: render_us=%lu/%lu spi_us=%lu/%lu audio_us=%lu/%lu "
      "heap_free=%u heap_min=%u heap_block=%u heap_total=%u "
      "stack_loop=%lu stack_control=%lu stack_oled=%lu stack_pdm=%lu stack_cloud=%lu stack_eth=%lu stack_tcpip=%lu stack_events=%lu stack_sys_event=%lu stack_wifi=%lu\n",
      average(render), static_cast<unsigned long>(render.max), average(spi),
      static_cast<unsigned long>(spi.max), average(audio),
      static_cast<unsigned long>(audio.max),
      unsigned(heap_caps_get_free_size(MALLOC_CAP_8BIT)),
      unsigned(heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT)),
      unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_8BIT)),
      unsigned(heap_caps_get_total_size(MALLOC_CAP_8BIT)),
      static_cast<unsigned long>(remaining[size_t(PerfTask::LOOP)]),
      static_cast<unsigned long>(remaining[size_t(PerfTask::CONTROL)]),
      static_cast<unsigned long>(remaining[size_t(PerfTask::OLED)]),
      static_cast<unsigned long>(remaining[size_t(PerfTask::PDM)]),
      static_cast<unsigned long>(remaining[size_t(PerfTask::CLOUD)]),
      named_stack("emac_rx"), named_stack("tiT"), named_stack("arduino_events"),
      named_stack("sys_evt"), named_stack("wifi"));
}
