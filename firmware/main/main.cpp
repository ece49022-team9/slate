#include <Arduino.h>
#include "slate_state.h"
#include "mic.h"
#include "pdm.h"

void setup() {
  Serial.begin(115200);
  mic_start();
  slate_start();
#if defined(SLATE_PDM_CLK) && defined(SLATE_PDM_DATA)
  pdm_start(SLATE_PDM_CLK, SLATE_PDM_DATA);
#else
  Serial.println("slate.boot: Physical PDM capture disabled pending S3 pin mapping");
#endif
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
