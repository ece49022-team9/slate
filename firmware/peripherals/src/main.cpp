// ============================================================================
//  Slate — main orchestrator
//  Owns app_main and the state machine. Components never change state
//  themselves; they request it, and the control task pushes it to everyone.
// ============================================================================
#include "Arduino.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "slate_state.h"
#include "display.h"
#include "mic.h"
// #include "haptics.h"   // later
// #include "speaker.h"   // later
// #include "net.h"       // later: Wi-Fi + cloud API

static QueueHandle_t       state_q = NULL;
static volatile SlateState g_state = IDLE;

// ---------------------------------------------------------------------------
//  Public API (declared in slate_state.h). Safe to call from any task.
// ---------------------------------------------------------------------------
void slate_request_state(SlateState s) {
  if (state_q) xQueueSend(state_q, &s, 0);
}

SlateState slate_get_state() { return g_state; }

// ---------------------------------------------------------------------------
//  Fan-out: every component that cares about state gets one line here.
// ---------------------------------------------------------------------------
static const char* state_name(SlateState s) {
  switch (s) {
    case IDLE:             return "IDLE";
    case SLATE_LISTEN:     return "LISTEN";
    case SLATE_MUTE:       return "MUTE";
    case SLATE_TRANSCRIBE: return "TRANSCRIBE";
    case SLATE_RESPOND:    return "RESPOND";
    case SLATE_ERROR:      return "ERROR";
    default:               return "?";
  }
}

static void apply_state(SlateState s) {
  g_state = s;
  display_set_state(s);
  mic_set_state(s);
  // haptics_set_state(s);
  // speaker_set_state(s);
  Serial.printf("[state] -> %s\n", state_name(s));
}

// Only this task ever changes state, so there are no races between modules.
static void Control_Task(void*) {
  SlateState s;
  for (;;) {
    if (xQueueReceive(state_q, &s, portMAX_DELAY) == pdTRUE && s != g_state) {
      apply_state(s);
    }
  }
}

// Stand-in for the API until networking exists: type 0-5 in the monitor.
// Later, the network task calls slate_request_state() the same way.
static void Serial_Task(void*) {
  for (;;) {
    while (Serial.available()) {
      char c = Serial.read();
      if (c >= '0' && c <= '5') slate_request_state((SlateState)(c - '0'));
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

extern "C" void app_main(void) {
  initArduino();
  Serial.begin(115200);
  delay(1000);
  Serial.println("slate boot");

  state_q = xQueueCreate(8, sizeof(SlateState));
  configASSERT(state_q);

  // Comment out a line to test components individually.
  display_start();
  //mic_start();

  apply_state(IDLE);

  xTaskCreate(Control_Task, "Control", 4096, NULL, 4, NULL);
  xTaskCreate(Serial_Task,  "Serial",  3072, NULL, 3, NULL);
}
