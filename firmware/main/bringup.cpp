#ifdef SLATE_BRINGUP
#include <Arduino.h>
#include <cmath>
#include "board.h"
#include "display.h"
#include "mic.h"
#include "pdm.h"
#include "speaker.h"
#include "oled.h"

// Offline state controller: no cloud, power, I2C, haptics or SD initialization.
static SlateState state = IDLE;
void slate_start() {}
SlateState slate_get_state() { return state; }
bool slate_request_state(SlateState next) {
  state = next;
  display_set_state(next);
  mic_set_state(next);
  return true;
}
static bool microphone, screen;
static bool continuous_demo;
static uint32_t last_tone;
static int16_t demo_tone[SPEAKER_RATE / 5];
static uint32_t deadline, last_report;
static uint64_t energy;
static uint32_t samples, peak;
static float min_rms = INFINITY, max_rms;

static void mcu() {
  Serial.printf("slate.mcu: chip=%s rev=%u cores=%u flash=%u psram=%u free_heap=%u free_psram=%u\n",
    ESP.getChipModel(), ESP.getChipRevision(), ESP.getChipCores(),
    ESP.getFlashChipSize(), ESP.getPsramSize(), ESP.getFreeHeap(), ESP.getFreePsram());
  if (ESP.getFlashChipSize() != 16 * 1024 * 1024 || ESP.getPsramSize() != 8 * 1024 * 1024)
    Serial.println("slate.mcu: ERROR expected 16 MB flash and 8 MB initialized PSRAM");
}
static bool stop() {
  continuous_demo = false;
  bool ok = true;
  uint32_t started = millis();
  while (!pdm_stop()) {
    if (millis() - started > 2000) { ok = false; break; }
    delay(10);
  }
  if (screen) {
    started = millis();
    while (!oled_stop()) {
      if (millis() - started > 2000) { ok = false; break; }
      delay(10);
    }
  }
  ok = speaker_stop() && ok;
  if (ok) {
    microphone = screen = false;
    mic_set_state(IDLE);
    deadline = 0;
  }
  Serial.printf("slate.test: cleanup %s\n", ok ? "complete" : "ERROR timed out; reset before retry");
  return ok;
}
static void run(char key) {
  bool continuous = key == 'd';
  if (continuous) key = 'c';
  if (!stop()) return;
  energy = samples = peak = 0;
  min_rms = INFINITY; max_rms = 0;
  if (key == 'm') { mcu(); return; }
  if (key == 'x') return;
  if (key == 'o' || key == 'c') {
    display_set_state(IDLE);
    oled_start();
    screen = true;
  }
  if (key == 'i' || key == 'c') {
    mic_configure(MicChannel::LEFT);
    mic_set_state(SLATE_LISTEN);
    if (screen) display_set_state(SLATE_LISTEN);
    pdm_start();
    microphone = pdm_ready();
    Serial.printf("slate.test: I2S0 PDM 16000 Hz left %s\n", microphone ? "initialized" : "ERROR");
  }
  if (key == 's' || key == 'c') {
    speaker_start();
    Serial.printf("slate.test: I2S1 Philips stereo 16000 Hz %s\n", speaker_ready() ? "initialized" : "ERROR");
    if (speaker_ready()) speaker_test();
    // Combined mode retains I2S1 while capture and display continue.
    if (key == 's') { delay(50); stop(); return; }
  }
  deadline = continuous ? 0 : millis() + 10000;
  continuous_demo = continuous;
  last_tone = millis();
  last_report = millis();
  Serial.println(continuous ? "slate.test: continuous demo; x stops" : "slate.test: running for 10 seconds; x stops early");
}
void setup() {
  Serial.begin(921600);
  delay(1500);
  mic_start();
  for (unsigned i = 0; i < SPEAKER_RATE / 5; ++i)
    demo_tone[i] = int16_t(2000 * sinf(2.0f * PI * 440 * i / SPEAKER_RATE));
  display_set_state(IDLE);
  mcu();
  Serial.println("slate.test: m=MCU i=microphone s=speaker o=OLED c=combined d=continuous demo x=stop");
  Serial.println("slate.test: AMP_SD releases to external bias; verify shutdown wiring before speaker test");
}
void loop() {
  while (Serial.available()) {
    char key = Serial.read();
    if (key == 'm' || key == 'i' || key == 's' || key == 'o' || key == 'c' || key == 'd' || key == 'x') run(key);
  }
  if (microphone) {
    int16_t block[MIC_FRAME_SAMPLES];
    size_t count;
    while ((count = mic_read(block, MIC_FRAME_SAMPLES))) {
      for (size_t i = 0; i < count; ++i) {
        int32_t value = block[i];
        energy += int64_t(value) * value;
        uint32_t magnitude = abs(value);
        if (magnitude > peak) peak = magnitude;
      }
      samples += count;
    }
    if (millis() - last_report >= 500) {
      float rms = samples ? sqrtf(float(energy) / samples) : 0;
      min_rms = fminf(min_rms, rms); max_rms = fmaxf(max_rms, rms);
      Serial.printf("slate.mic: %u samples rms=%.1f peak=%u rms_range=%.1f..%.1f%s\n",
        samples, rms, peak, min_rms, max_rms, samples ? "" : " ERROR no samples");
      samples = peak = 0; energy = 0; last_report = millis();
    }
  }
  if (continuous_demo && speaker_ready() && millis() - last_tone >= 3000) {
    speaker_play(reinterpret_cast<const uint8_t*>(demo_tone), sizeof(demo_tone));
    last_tone = millis();
    Serial.println("slate.test: headphone tone queued (440 Hz, 200 ms)");
  }
  if (deadline && int32_t(millis() - deadline) >= 0) {
    if (microphone) Serial.printf("slate.mic: level variation %s; speak/tap and inspect RMS, silence alone is inconclusive\n",
      max_rms - min_rms > 10 ? "observed" : "not observed");
    stop();
  }
  static uint32_t heartbeat;
  if (millis() - heartbeat >= 2000) { mcu(); heartbeat = millis(); }
  delay(5);
}
#endif
