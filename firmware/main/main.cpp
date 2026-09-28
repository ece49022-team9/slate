#include <Arduino.h>
#include <math.h>
#include "board.h"
#include "slate_state.h"
#include "mic.h"
#include "pdm.h"
#include "oled.h"

void setup() {
  Serial.begin(115200);
  mic_start();
  slate_start();
  pdm_start();
  Serial.printf("slate.boot: %s PDM mic on CLK=%d DATA=%d\n", BOARD_MCU, MIC_CLK, MIC_DATA);
  oled_start();
  Serial.printf("slate.boot: OLED on CLK=%d MOSI=%d CS=%d DC=%d RESET=%d at %u Hz\n",
                OLED_CLK, OLED_DATA, OLED_CS, OLED_DC, OLED_RESET, OLED_SPI_HZ);
  Serial.println("slate.boot: Send 0-5 over serial to change device state");
  Serial.println("slate.state: 0");
}

void loop() {
  static SlateState reported_state = IDLE;
  static uint32_t last_meter = 0;
  static uint32_t meter_samples = 0;
  static uint64_t meter_energy = 0;
  static int32_t meter_peak = 0;
  while (Serial.available()) {
    char key = Serial.read();
    if (key >= '0' && key <= '5') {
      slate_request_state(static_cast<SlateState>(key - '0'));
    } else if (key == 'l' || key == 'r' || key == 'm') {
      MicChannel channel = key == 'l' ? MicChannel::LEFT
                           : key == 'r' ? MicChannel::RIGHT : MicChannel::MIX;
      Serial.printf("slate.mic.channel: %c %s\n", key,
                    mic_configure(channel) ? "selected" : "stop listening first");
    }
  }
  SlateState state = slate_get_state();
  if (state != reported_state) {
    Serial.printf("slate.state: %u\n", static_cast<unsigned>(state));
    reported_state = state;
  }
  if (state == SLATE_LISTEN) {
    int16_t samples[MIC_FRAME_SAMPLES];
    size_t count;
    while ((count = mic_read(samples, MIC_FRAME_SAMPLES)) > 0) {
      for (size_t i = 0; i < count; ++i) {
        int32_t sample = samples[i];
        int32_t magnitude = sample < 0 ? -sample : sample;
        meter_energy += uint64_t(sample * sample);
        if (magnitude > meter_peak) meter_peak = magnitude;
      }
      meter_samples += count;
    }
    if (millis() - last_meter >= 500) {
      float rms = meter_samples ? sqrtf(float(meter_energy) / meter_samples) : 0;
      Serial.printf("slate.mic: %lu samples, rms=%.0f, peak=%d\n",
                    static_cast<unsigned long>(meter_samples), rms, meter_peak);
      last_meter = millis();
      meter_samples = 0;
      meter_energy = 0;
      meter_peak = 0;
    }
  } else {
    meter_samples = 0;
    meter_energy = 0;
    meter_peak = 0;
    last_meter = millis();
  }
  delay(20);
}
