#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/stream_buffer.h"
#include "mic.h"

static SemaphoreHandle_t lock;
static StreamBufferHandle_t buffer;
static MicChannel selected = MicChannel::LEFT;
static bool listening;
static float previous_input;
static float previous_output;

void mic_start() {
  configASSERT(!buffer);
  lock = xSemaphoreCreateMutex();
  buffer = xStreamBufferCreate(MIC_BUFFER_SAMPLES * sizeof(int16_t), 1);
  configASSERT(lock && buffer);
}

bool mic_configure(MicChannel channel) {
  if (channel > MicChannel::MIX) return false;
  xSemaphoreTake(lock, portMAX_DELAY);
  bool accepted = !listening;
  if (accepted) selected = channel;
  xSemaphoreGive(lock);
  return accepted;
}

void mic_set_state(SlateState state) {
  xSemaphoreTake(lock, portMAX_DELAY);
  bool next = state == SLATE_LISTEN;
  if ((next && !listening) || (!next && state != SLATE_TRANSCRIBE)) {
    xStreamBufferReset(buffer);
    previous_input = 0;
    previous_output = 0;
  }
  listening = next;
  xSemaphoreGive(lock);
}

bool mic_submit(const int16_t* stereo, size_t frames) {
  if (!stereo || frames == 0 || frames > MIC_FRAME_SAMPLES) return false;
  xSemaphoreTake(lock, portMAX_DELAY);
  if (!listening) {
    xSemaphoreGive(lock);
    return true;
  }
  size_t bytes = frames * sizeof(int16_t);
  if (xStreamBufferSpacesAvailable(buffer) < bytes) {
    xSemaphoreGive(lock);
    ESP_LOGE("slate.mic", "Capture buffer full; rejected %u samples", unsigned(frames));
    return false;
  }
  int16_t mono[MIC_FRAME_SAMPLES];
  for (size_t i = 0; i < frames; ++i) {
    float input;
    if (selected == MicChannel::MIX) {
      input = (int32_t(stereo[2 * i]) + int32_t(stereo[2 * i + 1])) / 2.0f;
    } else {
      input = stereo[2 * i + (selected == MicChannel::RIGHT)];
    }
    float output = input - previous_input + 0.995f * previous_output;
    previous_input = input;
    previous_output = output;
    if (output > 32767) output = 32767;
    if (output < -32768) output = -32768;
    mono[i] = static_cast<int16_t>(output);
  }
  size_t sent = xStreamBufferSend(buffer, mono, bytes, 0);
  configASSERT(sent == bytes);
  xSemaphoreGive(lock);
  return sent == bytes;
}

size_t mic_read(int16_t* mono, size_t capacity) {
  xSemaphoreTake(lock, portMAX_DELAY);
  size_t received = xStreamBufferReceive(buffer, mono, capacity * sizeof(int16_t), 0);
  xSemaphoreGive(lock);
  return received / sizeof(int16_t);
}

size_t mic_buffered() {
  xSemaphoreTake(lock, portMAX_DELAY);
  size_t samples = xStreamBufferBytesAvailable(buffer) / sizeof(int16_t);
  xSemaphoreGive(lock);
  return samples;
}
