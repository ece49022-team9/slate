#include "speaker.h"
#include "board.h"
#include "Arduino.h"
#include "perf.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
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

// Release SD_MODE to the board's external channel-select bias after init.
// Never force it high: resistor networks on breakout boards can select channels.
static void amplifier(bool enabled) {
#if defined(SLATE_BOARD_S3) && !defined(SLATE_PCM5102)
  if (enabled) pinMode(SPEAKER_SD, INPUT);
  else { gpio_set_level(static_cast<gpio_num_t>(SPEAKER_SD), 0); pinMode(SPEAKER_SD, OUTPUT); }
#endif
}
bool speaker_ready() { return speaker_tx != nullptr && !stopping.load(); }
static constexpr size_t REPLY_BUFFER_BYTES = 8192;
static constexpr size_t REPLY_BLOCK_SAMPLES = 128;
static constexpr uint32_t REPLY_PREBUFFER_MS = 160;
static constexpr size_t REPLY_PREBUFFER_BYTES =
    SPEAKER_RATE * sizeof(int16_t) * REPLY_PREBUFFER_MS / 1000;
static constexpr uint32_t REPLY_SETTLE_MS = 100;

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

  amplifier(false);
  stopping.store(false);
  i2s_chan_config_t channel_config =
      I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  // Match the tone producer's blocks so DMA never transmits a partially
  // filled 511-frame default buffer when playback starts after boot.
  channel_config.dma_frame_num = 128;
  channel_config.auto_clear = true;

  esp_err_t error = i2s_new_channel(&channel_config, &speaker_tx, nullptr);
  if (error != ESP_OK) {
    Serial.printf("slate.speaker: allocate I2S1 failed: %s\n", esp_err_to_name(error));
    return;
  }

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

#ifdef SLATE_PCM5102
  // At 16 kHz the PCM5102's BCK PLL requires 64 clocks per stereo frame.
  // Expand PCM explicitly to full-width words before DMA.
  config.slot_cfg.data_bit_width = I2S_DATA_BIT_WIDTH_32BIT;
  config.slot_cfg.slot_bit_width = I2S_SLOT_BIT_WIDTH_32BIT;
  config.slot_cfg.ws_width = 32;
#endif
  error = i2s_channel_init_std_mode(speaker_tx, &config);
  if (error == ESP_OK) error = i2s_channel_enable(speaker_tx);
  if (error != ESP_OK) {
    Serial.printf("slate.speaker: initialize failed: %s\n", esp_err_to_name(error));
    i2s_del_channel(speaker_tx);
    speaker_tx = nullptr;
    return;
  }

  if (!reply_audio) {
    reply_audio = xStreamBufferCreate(REPLY_BUFFER_BYTES, 1);
    configASSERT(reply_audio);
    configASSERT(xTaskCreate(play_replies, "Speaker", 4096, nullptr, 3, nullptr) == pdPASS);
  }

  amplifier(true);
#ifdef SLATE_PCM5102
  Serial.println("slate.speaker: PCM5102 explicit 32-bit PCM, LRCLK=16000 BCLK=1024000; GPIO16 untouched");
#endif

}

size_t speaker_write(const uint8_t* data, size_t bytes) {
  if (speaker_tx == nullptr || data == nullptr || bytes % 4 != 0) {
    return 0;
  }
  size_t written = 0;

#ifdef SLATE_PCM5102
  int32_t expanded[128];
  size_t consumed = 0;
  while (consumed < bytes) {
    size_t count = min(size_t(128), (bytes - consumed) / sizeof(int16_t));
    for (size_t i = 0; i < count; ++i) {
      int16_t sample;
      memcpy(&sample, data + consumed + i * sizeof(sample), sizeof(sample));
      expanded[i] = int32_t(sample) * 65536;
    }
    esp_err_t error = i2s_channel_write(speaker_tx, expanded, count * sizeof(int32_t), &written, 1000);
    if (error != ESP_OK || written != count * sizeof(int32_t)) {
      Serial.printf("slate.speaker: PCM5102 write failed: %s bytes=%u\n", esp_err_to_name(error), unsigned(written));
      return consumed;
    }
    consumed += count * sizeof(int16_t);
  }
  return consumed;
#else
  esp_err_t error = i2s_channel_write(
      speaker_tx,
      data,
      bytes,
      &written,
      1000);

  if (error != ESP_OK) {
    Serial.printf("slate.speaker: write failed: %s\n", esp_err_to_name(error));
    return 0;
  }

  return written;
#endif
}

void speaker_play(const uint8_t* pcm, size_t bytes) {
  if (!reply_audio || stopping.load() || bytes % sizeof(int16_t)) return;
  reply_arrived_at.store(millis());
  if (xStreamBufferSend(reply_audio, pcm, bytes, 0) < bytes) {
    static uint32_t last_overflow;
    if (millis() - last_overflow >= 1000) {
      Serial.println("slate.speaker: reply buffer full, dropping audio");
      last_overflow = millis();
    }
  }
}

void speaker_flush() {
  reply_flush.store(true);
}

bool speaker_stop() {
  amplifier(false);
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
  const int duration_ms = 500;

  const int samples = SPEAKER_RATE * duration_ms / 1000;

  int16_t buffer[256];

  for (int i = 0; i < samples; i += 128) {
    int count = min(128, samples - i);

    for (int j = 0; j < count; j++) {
      float t = (float)(i + j) / SPEAKER_RATE;
      int16_t sample =
          (int16_t)(sin(2.0 * PI * frequency * t) * 2000);

      buffer[j * 2] = sample;
      buffer[j * 2 + 1] = sample;
    }

    size_t bytes = count * 2 * sizeof(int16_t);
    if (speaker_write((const uint8_t*)buffer, bytes) != bytes) {
      Serial.println("slate.speaker: test tone incomplete");
      return;
    }
  }
}
