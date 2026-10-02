#include <Arduino.h>
#include <atomic>
#include <math.h>
#include <ctype.h>
#include <string.h>
#include "board.h"
#include "slate_state.h"
#include "mic.h"
#include "pdm.h"
#include "oled.h"
#include "speaker.h"
#include "power.h"
#include "display.h"
#include "perf.h"

static std::atomic<bool> speaker_test_running{false};

// Application-specific cleanup, polled while loop() remains quiescent.
static void finish_shutdown() {
  static bool requested = false;
  static bool finished = false;
  static uint32_t requested_at = 0;
  if (finished) return;
  if (!requested) {
    requested = true;
    requested_at = millis();
  }
  if (uint32_t(millis() - requested_at) >= 3000) {
    Serial.println("power: shutdown cleanup timed out; power retained");
    finished = true; // A failed shutdown, not permission to release the latch.
    return;
  }
#ifndef SLATE_SPEAKER_TEST
  // Request both tasks to stop even if one has not acknowledged yet.
  bool capture_stopped = pdm_stop();
  bool display_stopped = oled_stop();
  if (!capture_stopped || !display_stopped) return;
#endif
  if (speaker_test_running.load()) return;
#ifndef SLATE_SPEAKER_TEST
  mic_set_state(IDLE);
#endif
  if (!speaker_stop()) {
    Serial.println("power: speaker cleanup failed; power retained");
    finished = true;
    return;
  }
  power_finish_shutdown();
  finished = true;
}

#ifdef SLATE_SPEAKER_TEST
void setup() {
  power_start();
  Serial.begin(921600);
  speaker_start();
  Serial.println("slate.speaker: ready");
  speaker_test();
}

void loop() {
  if (power_update()) finish_shutdown();
  delay(POWER_ENABLED ? 20 : 500);
}
#else

static void play_speaker_test(void*) {
  speaker_test();
  speaker_test_running.store(false);
  vTaskDelete(nullptr);
}

void setup() {
  power_start();
  Serial.begin(921600);
  mic_start();
  slate_start();
  pdm_start();
  speaker_start();
  Serial.printf("slate.boot: %s PDM mic on CLK=%d DATA=%d\n", BOARD_MCU, MIC_CLK, MIC_DATA);
  oled_start();
  Serial.printf("slate.boot: OLED on CLK=%d MOSI=%d CS=%d DC=%d RESET=%d at %u Hz\n",
                OLED_CLK, OLED_DATA, OLED_CS, OLED_DC, OLED_RESET, OLED_SPI_HZ);
  Serial.println("slate.boot: Send 0-5 to change state, l/r/m to pick a mic, a/x to stream audio, t to test speaker");
  Serial.println("slate.state: 0");
}

static void send_audio(const int16_t* samples, size_t count) {
  uint16_t bytes = count * sizeof(int16_t);
  uint8_t header[] = {0, uint8_t(bytes), uint8_t(bytes >> 8)};
  Serial.write(header, sizeof(header));
  Serial.write(reinterpret_cast<const uint8_t*>(samples), bytes);
}

static void device_command(char* line) {
  char id[33] = {}, operation[8] = {}, argument[129] = {}, extra[2] = {};
  int consumed = 0;
  int fields = sscanf(line, "%32s %7s %128[^\n]%n", id, operation, argument, &consumed);
  if (strlen(id) != 32) {
    Serial.println("slate.device.error: invalid request id");
    return;
  }
  for (char key : id) {
    if (key && !isxdigit(key)) return;
  }
  bool accepted = false;
  const char* name = "get_status";
  if (fields == 3 && line[consumed]) {
    Serial.printf("slate.device:%s {\"error\":\"invalid command\"}\n", id);
    return;
  }
  if (fields == 2 && !strcmp(operation, "status")) {
    accepted = true;
  } else if (fields == 3 && !strcmp(operation, "orb")) {
    char hex[7] = {};
    float radius;
    unsigned color;
    if (sscanf(argument, "%6s %f %1s", hex, &radius, extra) == 2 && strlen(hex) == 6) {
      bool valid = true;
      for (unsigned i = 0; i < 6; ++i) valid &= isxdigit(hex[i]) != 0;
      if (valid && sscanf(hex, "%x", &color) == 1) accepted = display_set_orb(color, radius);
    }
    name = "set_orb";
  } else if (fields >= 2 && !strcmp(operation, "text")) {
    size_t length = strlen(argument);
    char text[65] = {};
    accepted = length <= 128 && length % 2 == 0;
    for (size_t i = 0; accepted && i < length; i += 2) {
      unsigned value;
      char pair[] = {argument[i], argument[i + 1], 0};
      accepted = isxdigit(pair[0]) && isxdigit(pair[1]) &&
                 sscanf(pair, "%x", &value) == 1 && value >= 32 && value <= 126;
      if (accepted) text[i / 2] = value;
    }
    if (accepted) display_set_text(text);
    name = "show_text";
  }
  if (!accepted) {
    Serial.printf("slate.device:%s {\"error\":\"invalid command\"}\n", id);
    return;
  }
  DisplayConfig settings;
  display_snapshot(settings);
  char hex[129] = {};
  for (unsigned i = 0; settings.text[i]; ++i) sprintf(hex + 2 * i, "%02x", unsigned(settings.text[i]));
  Serial.printf("slate.device:%s {\"request_id\":\"%s\",\"operation\":\"%s\","
                "\"revision\":%u,\"state\":%u,\"color\":\"#%06lx\",\"radius\":%.3f,"
                "\"text_hex\":\"%s\",\"custom\":%s}\n", id, id, name,
                settings.revision, unsigned(slate_get_state()), static_cast<unsigned long>(settings.color),
                settings.radius, hex, settings.custom ? "true" : "false");
}

void loop() {
  bool shutting_down = power_update();
  static SlateState reported_state = IDLE;
  static bool streaming = false;
  static uint32_t last_meter = 0;
  static uint32_t meter_samples = 0;
  if (shutting_down) {
    streaming = false;
    finish_shutdown();
    delay(20);
    return;
  }
  static uint64_t meter_energy = 0;
  static int32_t meter_peak = 0;
  static char command[192];
  static unsigned command_size = 0;
  static bool command_open = false, command_overflow = false;
  while (Serial.available()) {
    char key = Serial.read();
    if (command_open) {
      if (key == '\n') {
        if (!command_overflow) {
          command[command_size] = 0;
          device_command(command);
        } else {
          Serial.println("slate.device.error: command too long");
        }
        command_open = command_overflow = false;
        command_size = 0;
      } else if (command_size < sizeof(command) - 1) {
        command[command_size++] = key;
      } else {
        command_overflow = true;
      }
    } else if (key == '@') {
      command_open = true;
    } else if (key >= '0' && key <= '5') {
      slate_request_state(static_cast<SlateState>(key - '0'));
    } else if (key == 'l' || key == 'r' || key == 'm') {
      MicChannel channel = key == 'l' ? MicChannel::LEFT
                           : key == 'r' ? MicChannel::RIGHT : MicChannel::MIX;
      Serial.printf("slate.mic.channel: %c %s\n", key,
                    mic_configure(channel) ? "selected" : "stop listening first");
    } else if (key == 'a' || key == 'x') {
      streaming = key == 'a';
      Serial.printf("slate.audio: %s\n", streaming ? "streaming" : "off");
    } else if (key == 't' && !speaker_test_running.exchange(true)) {
      if (xTaskCreate(play_speaker_test, "speaker-test", 4096, nullptr, 5,
                      nullptr) != pdPASS) {
        speaker_test_running.store(false);
        Serial.println("slate.speaker: could not start test");
      }
    }
  }
  SlateState state = slate_get_state();
  if (state != reported_state) {
    Serial.printf("slate.state: %u\n", static_cast<unsigned>(state));
    reported_state = state;
  }
  if (state == SLATE_LISTEN || state == SLATE_TRANSCRIBE) {
    int16_t samples[MIC_FRAME_SAMPLES];
    size_t count;
    while ((count = mic_read(samples, MIC_FRAME_SAMPLES)) > 0) {
      if (streaming) send_audio(samples, count);
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
  perf_stack(PerfTask::LOOP);
  delay(20);
}
#endif
