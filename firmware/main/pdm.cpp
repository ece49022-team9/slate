#include "driver/gpio.h"
#include "driver/i2s_pdm.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mic.h"
#include "pdm.h"

static i2s_chan_handle_t receiver;

static void capture(void*) {
  int16_t samples[MIC_FRAME_SAMPLES * 2];
  for (;;) {
    size_t bytes = 0;
    esp_err_t error = i2s_channel_read(receiver, samples, sizeof(samples), &bytes, 1000);
    if (error != ESP_OK || bytes == 0 || bytes % 4 != 0) {
      ESP_LOGE("slate.pdm", "Capture failed: %s, %u bytes", esp_err_to_name(error), unsigned(bytes));
      mic_set_state(SLATE_ERROR);
      slate_request_state(SLATE_ERROR);
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    if (!mic_submit(samples, bytes / 4)) {
      mic_set_state(SLATE_ERROR);
      slate_request_state(SLATE_ERROR);
    }
  }
}

void pdm_start(int clock_pin, int data_pin) {
  ESP_ERROR_CHECK(GPIO_IS_VALID_OUTPUT_GPIO(clock_pin) && GPIO_IS_VALID_GPIO(data_pin)
                      && clock_pin != data_pin ? ESP_OK : ESP_ERR_INVALID_ARG);
  i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  channel.dma_desc_num = 6;
  channel.dma_frame_num = MIC_FRAME_SAMPLES;
  ESP_ERROR_CHECK(i2s_new_channel(&channel, nullptr, &receiver));
  i2s_pdm_rx_config_t config = {};
  config.clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE);
  config.slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
  config.slot_cfg.slot_mask = I2S_PDM_SLOT_BOTH;
  config.gpio_cfg.clk = static_cast<gpio_num_t>(clock_pin);
  config.gpio_cfg.din = static_cast<gpio_num_t>(data_pin);
  ESP_ERROR_CHECK(i2s_channel_init_pdm_rx_mode(receiver, &config));
  ESP_ERROR_CHECK(i2s_channel_enable(receiver));
  BaseType_t created = xTaskCreate(capture, "PDM", 4096, nullptr, 6, nullptr);
  configASSERT(created == pdPASS);
  ESP_LOGI("slate.pdm", "Capturing left/right PCM at %u Hz", MIC_SAMPLE_RATE);
}
