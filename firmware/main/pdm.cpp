#include <atomic>
#include "driver/gpio.h"
#include "driver/i2s_pdm.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mic.h"
#include "pdm.h"
#include "perf.h"

static i2s_chan_handle_t receiver;
static std::atomic<bool> stop_requested{false};
static std::atomic<bool> stopped{false};

static void capture(void*) {
  int16_t samples[MIC_FRAME_SAMPLES * 2];
  for (;;) {
    if (stop_requested.load()) break;
    size_t bytes = 0;
    esp_err_t error = i2s_channel_read(receiver, samples, sizeof(samples), &bytes, 1000);
    if (stop_requested.load()) break;
    if (error != ESP_OK || bytes == 0 || bytes % 4 != 0) {
      ESP_LOGE("slate.pdm", "Capture failed: %s, %u bytes", esp_err_to_name(error), unsigned(bytes));
      mic_set_state(SLATE_ERROR);
      slate_request_state(SLATE_ERROR);
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }
    int64_t started = esp_timer_get_time();
    bool accepted = mic_submit(samples, bytes / 4);
    perf_time(PerfTimer::AUDIO, uint32_t(esp_timer_get_time() - started));
    perf_stack(PerfTask::PDM);
    if (!accepted) {
      mic_set_state(SLATE_ERROR);
      slate_request_state(SLATE_ERROR);
    }
  }
  esp_err_t error = i2s_channel_disable(receiver);
  if (error == ESP_OK) error = i2s_del_channel(receiver);
  if (error == ESP_OK) stopped.store(true);
  else ESP_LOGE("slate.pdm", "Stop failed: %s", esp_err_to_name(error));
  vTaskDelete(nullptr);
}

bool pdm_stop() {
  if (!receiver) return true;
  stop_requested.store(true);
  if (!stopped.load()) return false;
  receiver = nullptr;
  return true;
}

bool pdm_ready() { return receiver && !stop_requested.load(); }

void pdm_start() {
  if (receiver) return;
  stop_requested.store(false);
  stopped.store(false);
  i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  channel.dma_desc_num = 6;
  channel.dma_frame_num = MIC_FRAME_SAMPLES;
  esp_err_t error = i2s_new_channel(&channel, nullptr, &receiver);
  if (error != ESP_OK) {
    ESP_LOGE("slate.pdm", "Allocate I2S0: %s", esp_err_to_name(error));
    return;
  }
  i2s_pdm_rx_config_t config = {};
  config.clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_HZ);
  config.slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
  config.slot_cfg.slot_mask = I2S_PDM_SLOT_BOTH;
  config.gpio_cfg.clk = static_cast<gpio_num_t>(MIC_CLK);
  config.gpio_cfg.din = static_cast<gpio_num_t>(MIC_DATA);
  // 16 kHz * 64 = 1.024 MHz PDM clock, supported by MP34DT01.
  config.clk_cfg.dn_sample_mode = I2S_PDM_DSR_8S;
  error = i2s_channel_init_pdm_rx_mode(receiver, &config);
  if (error == ESP_OK) error = i2s_channel_enable(receiver);
  if (error != ESP_OK) {
    ESP_LOGE("slate.pdm", "Initialize I2S0: %s", esp_err_to_name(error));
    i2s_del_channel(receiver);
    receiver = nullptr;
    return;
  }
  BaseType_t created = xTaskCreate(capture, "PDM", 4096, nullptr, 6, nullptr);
  if (created != pdPASS) {
    ESP_LOGE("slate.pdm", "Could not create capture task");
    i2s_channel_disable(receiver);
    i2s_del_channel(receiver);
    receiver = nullptr;
    return;
  }
  ESP_LOGI("slate.pdm", "Capturing left/right PCM at %u Hz", MIC_SAMPLE_HZ);
}
