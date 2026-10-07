#include "speaker.h"
#include "Arduino.h"
#include "driver/i2s.h"

static constexpr i2s_port_t SPEAKER_I2S = I2S_NUM_1;

static constexpr int SPEAKER_BCLK = 6;
static constexpr int SPEAKER_DATA = 15;
static constexpr int SPEAKER_LRCLK = 17;
static constexpr int SPEAKER_SD = 16;

static constexpr int SPEAKER_RATE = 16000;

void speaker_start() {
  pinMode(SPEAKER_SD, OUTPUT);
  digitalWrite(SPEAKER_SD, LOW);

  i2s_config_t config = {
      .mode = static_cast<i2s_mode_t>(
          I2S_MODE_MASTER | I2S_MODE_TX),
      .sample_rate = SPEAKER_RATE,
      .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
      .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
      .communication_format = I2S_COMM_FORMAT_I2S,
      .intr_alloc_flags = 0,
      .dma_buf_count = 8,
      .dma_buf_len = 256,
      .use_apll = false,
      .tx_desc_auto_clear = true,
      .fixed_mclk = 0,
  };

  i2s_pin_config_t pins = {
      .bck_io_num = SPEAKER_BCLK,
      .ws_io_num = SPEAKER_LRCLK,
      .data_out_num = SPEAKER_DATA,
      .data_in_num = I2S_PIN_NO_CHANGE,
  };

  i2s_driver_install(SPEAKER_I2S, &config, 0, nullptr);
  i2s_set_pin(SPEAKER_I2S, &pins);
  i2s_zero_dma_buffer(SPEAKER_I2S);

  digitalWrite(SPEAKER_SD, HIGH);
}

size_t speaker_write(const uint8_t* data, size_t bytes) {
  size_t written = 0;

  i2s_write(
      SPEAKER_I2S,
      data,
      bytes,
      &written,
      portMAX_DELAY
  );

  return written;
}

void speaker_stop() {
  digitalWrite(SPEAKER_SD, LOW);
  i2s_zero_dma_buffer(SPEAKER_I2S);
}