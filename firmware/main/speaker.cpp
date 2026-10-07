#include "speaker.h"
#include "Arduino.h"
#include "driver/i2s_std.h"

static i2s_chan_handle_t speaker_tx;

static constexpr gpio_num_t SPEAKER_BCLK = GPIO_NUM_26;
static constexpr gpio_num_t SPEAKER_DATA = GPIO_NUM_27;
static constexpr gpio_num_t SPEAKER_LRCLK = GPIO_NUM_25;

//static constexpr int SPEAKER_SD = 4;
static constexpr int SPEAKER_RATE = 16000;

void speaker_start() {
  Serial.println("SPEAKER 1");

  //pinMode(SPEAKER_SD, OUTPUT);
  //digitalWrite(SPEAKER_SD, LOW);

  Serial.println("SPEAKER 2");

  i2s_chan_config_t channel_config =
      I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);

  Serial.println("SPEAKER 3");

  ESP_ERROR_CHECK(
      i2s_new_channel(&channel_config, &speaker_tx, nullptr));

  Serial.println("SPEAKER 4");

  i2s_std_config_t config = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SPEAKER_RATE),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
          I2S_DATA_BIT_WIDTH_16BIT,
          I2S_SLOT_MODE_MONO),
      .gpio_cfg = {
          .mclk = I2S_GPIO_UNUSED,
          .bclk = SPEAKER_BCLK,
          .ws = SPEAKER_LRCLK,
          .dout = SPEAKER_DATA,
          .din = I2S_GPIO_UNUSED,
          .invert_flags = {
              .mclk_inv = false,
              .bclk_inv = false,
              .ws_inv = false,
          },
      },
  };

  config.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

  Serial.println("SPEAKER 5");

  ESP_ERROR_CHECK(
      i2s_channel_init_std_mode(speaker_tx, &config));

  Serial.println("SPEAKER 6");

  ESP_ERROR_CHECK(i2s_channel_enable(speaker_tx));

  Serial.println("SPEAKER 7");

  //digitalWrite(SPEAKER_SD, HIGH);

  Serial.println("SPEAKER 8");
}

size_t speaker_write(const uint8_t* data, size_t bytes) {
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
    i2s_channel_disable(speaker_tx);
  }
}