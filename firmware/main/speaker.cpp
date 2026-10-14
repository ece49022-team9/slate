#include "speaker.h"
#include "board.h"
#include "Arduino.h"
#include "driver/i2s_std.h"
#include <math.h>

static i2s_chan_handle_t speaker_tx;

//static constexpr int SPEAKER_SD = 4;
static constexpr int SPEAKER_RATE = 16000;

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

void speaker_stop() {
  //digitalWrite(SPEAKER_SD, LOW);

  if (speaker_tx != nullptr) {
    ESP_ERROR_CHECK(i2s_channel_disable(speaker_tx));
    ESP_ERROR_CHECK(i2s_del_channel(speaker_tx));
    speaker_tx = nullptr;
  }
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
