#include <atomic>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "slate_state.h"
#include "display.h"
#include "mic.h"
#include "perf.h"

static QueueHandle_t state_queue;
static std::atomic<SlateState> current_state{IDLE};
static const char* names[] = {"IDLE", "LISTEN", "MUTE", "TRANSCRIBE", "RESPOND", "ERROR"};

bool slate_request_state(SlateState state) {
  if (state > SLATE_ERROR) {
    ESP_LOGW("slate.state", "Rejected invalid state %u", static_cast<unsigned>(state));
    return false;
  }
  if (!state_queue || xQueueSend(state_queue, &state, 0) != pdTRUE) {
    ESP_LOGE("slate.state", "Could not queue %s", names[state]);
    return false;
  }
  return true;
}

SlateState slate_get_state() { return current_state.load(); }

static void apply_state(SlateState state) {
  display_set_state(state);
  mic_set_state(state);
  current_state.store(state);
  ESP_LOGI("slate.state", "%s", names[state]);
}

static void control_task(void*) {
  SlateState state;
  for (;;) {
    if (xQueueReceive(state_queue, &state, portMAX_DELAY) == pdTRUE &&
        state != current_state.load()) {
      apply_state(state);
    }
    perf_stack(PerfTask::CONTROL);
  }
}

void slate_start() {
  configASSERT(!state_queue);
  state_queue = xQueueCreate(8, sizeof(SlateState));
  configASSERT(state_queue);
  apply_state(IDLE);
  BaseType_t created = xTaskCreate(control_task, "Control", 4096, nullptr, 4, nullptr);
  configASSERT(created == pdPASS);
}
