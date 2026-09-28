#include <Arduino.h>
#include "slate_state.h"
#include "mic.h"
#include "pdm.h"
#include "oled.h"

void setup() {
  Serial.begin(115200);
  mic_start();
  slate_start();
#if defined(SLATE_PDM_CLK) && defined(SLATE_PDM_DATA)
  pdm_start(SLATE_PDM_CLK, SLATE_PDM_DATA);
#else
  Serial.println("slate.boot: Physical PDM capture disabled pending ESP32 pin mapping");
#endif
#if defined(SLATE_OLED_CLK) && defined(SLATE_OLED_DATA) && defined(SLATE_OLED_CS) && defined(SLATE_OLED_DC) && defined(SLATE_OLED_RESET)
  oled_start(SLATE_OLED_CLK, SLATE_OLED_DATA, SLATE_OLED_CS, SLATE_OLED_DC, SLATE_OLED_RESET);
#else
  Serial.println("slate.boot: OLED disabled pending ESP32 pin mapping");
#endif
  Serial.println("slate.boot: Send 0-5 over serial to change device state");
}

void loop() {
  while (Serial.available()) {
    char key = Serial.read();
    if (key >= '0' && key <= '5') {
      slate_request_state(static_cast<SlateState>(key - '0'));
    }
  }
  delay(20);
}
