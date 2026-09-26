#include <atomic>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "slate_state.h"
#include "display.h"
#include "mic.h"

#if !CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_ENABLE
#error "The controller simulation requires assertions enabled"
#endif

static std::atomic<SlateState> display_state{SLATE_ERROR};
static std::atomic<bool> streaming{false};
static std::atomic<unsigned> updates{0};

void display_set_state(SlateState state) {
  display_state.store(state);
  updates.fetch_add(1);
}

void mic_set_state(SlateState state) { streaming.store(state == SLATE_LISTEN); }

extern "C" void app_main() {
  ESP_LOGI("slate.sim", "ESP32-S3 controller simulation; no physical I/O");
  configASSERT(!slate_request_state(SLATE_LISTEN));
  slate_start();
  configASSERT(display_state.load() == IDLE);
  configASSERT(!streaming.load());
  configASSERT(!slate_request_state(static_cast<SlateState>(255)));

  const SlateState sequence[] = {
      SLATE_LISTEN, SLATE_MUTE, SLATE_LISTEN, SLATE_TRANSCRIBE,
      SLATE_RESPOND, IDLE, SLATE_ERROR, IDLE};
  unsigned expected_updates = 1;
  for (SlateState state : sequence) {
    configASSERT(slate_request_state(state));
    TickType_t start = xTaskGetTickCount();
    while (slate_get_state() != state) {
      configASSERT(xTaskGetTickCount() - start < pdMS_TO_TICKS(1000));
      vTaskDelay(1);
    }
    configASSERT(display_state.load() == state);
    configASSERT(streaming.load() == (state == SLATE_LISTEN));
    configASSERT(updates.load() == ++expected_updates);
  }
  configASSERT(slate_request_state(IDLE));
  vTaskDelay(pdMS_TO_TICKS(50));
  configASSERT(updates.load() == expected_updates);
  configASSERT(!streaming.load());
  ESP_LOGI("slate.sim", "PASS: state transitions, display fan-out, listen gating, invalid and duplicate requests");
}
