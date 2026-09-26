#include <Arduino.h>
#include "slate_state.h"

void setup() {
  Serial.begin(115200);
  Serial.println("slate.boot: ESP32-S3; physical peripherals disabled pending pin mapping");
  slate_start();
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
