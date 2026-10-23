#include "speaker.h"
#include "board.h"
#include "Arduino.h"
#include "perf.h"
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"
#include <atomic>
#include <math.h>

static i2s_chan_handle_t speaker_tx;
static StreamBufferHandle_t reply_audio;
static std::atomic<bool> reply_flush{false};
static std::atomic<bool> reply_writing{false};
static std::atomic<uint32_t> reply_arrived_at{0};
static std::atomic<bool> stopping{false};

//static constexpr int SPEAKER_SD = 4;
static constexpr size_t REPLY_BUFFER_BYTES = 8192;
static constexpr size_t REPLY_BLOCK_SAMPLES = 128;
static constexpr uint32_t REPLY_PREBUFFER_MS = 160;
static constexpr size_t REPLY_PREBUFFER_BYTES =
    SPEAKER_RATE * sizeof(int16_t) * REPLY_PREBUFFER_MS / 1000;
static constexpr uint32_t REPLY_SETTLE_MS = 100;
static constexpr uint32_t REPLY_WAIT_MS = 100;

static void play_replies(void*) {
  int16_t mono[REPLY_BLOCK_SAMPLES];
  int16_t stereo[REPLY_BLOCK_SAMPLES * 2];
  bool playing = false;
  for (;;) {
    if (reply_flush.exchange(false)) {
      while (xStreamBufferReceive(reply_audio, mono, sizeof(mono), 0)) {}
      playing = false;
    }
    size_t buffered = xStreamBufferBytesAvailable(reply_audio);
    bool settled = buffered && millis() - reply_arrived_at.load() >= REPLY_SETTLE_MS;
    playing = playing || buffered >= REPLY_PREBUFFER_BYTES || settled;
    size_t bytes = playing ? xStreamBufferReceive(reply_audio, mono, sizeof(mono), 0) : 0;
    if (!bytes) {
      playing = false;
      perf_stack(PerfTask::SPEAKER);
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    size_t count = bytes / sizeof(int16_t);
    for (size_t i = 0; i < count; ++i) stereo[2 * i] = stereo[2 * i + 1] = mono[i];
    reply_writing.store(true);
    if (count && !stopping.load()) speaker_write(reinterpret_cast<const uint8_t*>(stereo), count * 4);
    reply_writing.store(false);
    perf_stack(PerfTask::SPEAKER);
  }
}

void speaker_start() {

  if (speaker_tx != nullptr) {
    return;
  }

  //pinMode(SPEAKER_SD, OUTPUT);
  //digitalWrite(SPEAKER_SD, LOW);
  i2s_chan_config_t channel_config =
      I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  // Match the tone producer's blocks so DMA never transmits a partially
  // filled 511-frame default buffer when playback starts after boot.
  channel_config.dma_frame_num = 128;
  channel_config.auto_clear = true;

  ESP_ERROR_CHECK(
      i2s_new_channel(&channel_config, &speaker_tx, nullptr));

  i2s_std_config_t config = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SPEAKER_RATE),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
        I2S_DATA_BIT_WIDTH_16BIT,
        I2S_SLOT_MODE_STEREO),
      .gpio_cfg = {
          .mclk = I2S_GPIO_UNUSED,
          .bclk = static_cast<gpio_num_t>(SPEAKER_BCLK),
          .ws = static_cast<gpio_num_t>(SPEAKER_LRCLK),
          .dout = static_cast<gpio_num_t>(SPEAKER_DIN),
          .din = I2S_GPIO_UNUSED,
          .invert_flags = {
              .mclk_inv = false,
              .bclk_inv = false,
              .ws_inv = false,
          },
      },
  };


  ESP_ERROR_CHECK(
      i2s_channel_init_std_mode(speaker_tx, &config));

  ESP_ERROR_CHECK(i2s_channel_enable(speaker_tx));

  if (!reply_audio) {
    reply_audio = xStreamBufferCreate(REPLY_BUFFER_BYTES, 1);
    configASSERT(reply_audio);
    configASSERT(xTaskCreate(play_replies, "Speaker", 2048, nullptr, 3, nullptr) == pdPASS);
  }

  //digitalWrite(SPEAKER_SD, HIGH);

}

size_t speaker_write(const uint8_t* data, size_t bytes) {
  if (speaker_tx == nullptr || data == nullptr || bytes % 4 != 0) {
    return 0;
  }
  size_t written = 0;

  esp_err_t error = i2s_channel_write(
      speaker_tx,
      data,
      bytes,
      &written,
      portMAX_DELAY);

  if (error != ESP_OK) {
    return 0;
  }

  return written;
}

void speaker_play(const uint8_t* pcm, size_t bytes) {
  if (!reply_audio || stopping.load() || bytes % sizeof(int16_t)) return;
  reply_arrived_at.store(millis());
  size_t sent = xStreamBufferSend(reply_audio, pcm, bytes, pdMS_TO_TICKS(REPLY_WAIT_MS));
  if (sent < bytes) {
    static uint32_t last_overflow;
    if (millis() - last_overflow >= 1000) {
      Serial.printf("slate.speaker: reply buffer stayed full for %u ms, dropped %u bytes\n",
                    unsigned(REPLY_WAIT_MS), unsigned(bytes - sent));
      last_overflow = millis();
    }
  }
}

void speaker_flush() {
  reply_flush.store(true);
}

bool speaker_stop() {
  if (!speaker_tx) return true;
  stopping.store(true);
  speaker_flush();
  for (int waited = 0; reply_writing.load() && waited < 100; ++waited) delay(1);
  if (reply_writing.load()) return false;
  if (i2s_channel_disable(speaker_tx) != ESP_OK) return false;
  if (i2s_del_channel(speaker_tx) != ESP_OK) return false;
  speaker_tx = nullptr;
  return true;
}
void speaker_test() {
  const int frequency = 440;
  const int duration_ms = 1000;

  const int samples = SPEAKER_RATE * duration_ms / 1000;

  int16_t buffer[256];

  for (int i = 0; i < samples; i += 128) {
    int count = min(128, samples - i);

    for (int j = 0; j < count; j++) {
      float t = (float)(i + j) / SPEAKER_RATE;
      int16_t sample =
          (int16_t)(sin(2.0 * PI * frequency * t) * 20000);

      buffer[j * 2] = sample;
      buffer[j * 2 + 1] = sample;
    }

    speaker_write(
        (const uint8_t*)buffer,
        count * 2 * sizeof(int16_t));
  }
}
