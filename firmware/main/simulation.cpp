#include <atomic>
#include <initializer_list>
#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "slate_state.h"
#include "display.h"
#include "mic.h"

#if !CONFIG_COMPILER_OPTIMIZATION_ASSERTIONS_ENABLE
#error "The controller simulation requires assertions enabled"
#endif

static std::atomic<SlateState> display_state{SLATE_ERROR};
static std::atomic<unsigned> updates{0};

void display_set_state(SlateState state) {
  display_state.store(state);
  updates.fetch_add(1);
}

static void set_state(SlateState state) {
  configASSERT(slate_request_state(state));
  TickType_t start = xTaskGetTickCount();
  while (slate_get_state() != state) {
    configASSERT(xTaskGetTickCount() - start < pdMS_TO_TICKS(1000));
    vTaskDelay(1);
  }
}

static void check_pipeline() {
  configASSERT(!slate_request_state(SLATE_LISTEN));
  mic_start();
  slate_start();
  configASSERT(display_state.load() == IDLE);
  configASSERT(!slate_request_state(static_cast<SlateState>(255)));
  static int16_t stereo[MIC_FRAME_SAMPLES * 2] = {1000, -2000};
  static int16_t mono[MIC_FRAME_SAMPLES];
  const SlateState sequence[] = {
      SLATE_LISTEN, SLATE_MUTE, SLATE_LISTEN, SLATE_TRANSCRIBE,
      SLATE_RESPOND, IDLE, SLATE_ERROR, IDLE};
  unsigned expected_updates = 1;
  for (SlateState state : sequence) {
    set_state(state);
    configASSERT(display_state.load() == state);
    configASSERT(updates.load() == ++expected_updates);
    configASSERT(mic_submit(stereo, 1));
    size_t count = mic_read(mono, MIC_FRAME_SAMPLES);
    configASSERT(count == (state == SLATE_LISTEN ? 1 : 0));
    if (count) configASSERT(mono[0] == 1000);
  }
  set_state(IDLE);
  vTaskDelay(pdMS_TO_TICKS(50));
  configASSERT(updates.load() == expected_updates);
  for (MicChannel channel : {MicChannel::LEFT, MicChannel::RIGHT, MicChannel::MIX}) {
    configASSERT(mic_configure(channel));
    set_state(SLATE_LISTEN);
    configASSERT(mic_submit(stereo, 1));
    configASSERT(mic_read(mono, 1) == 1);
    int expected = channel == MicChannel::LEFT ? 1000 : channel == MicChannel::RIGHT ? -2000 : -500;
    configASSERT(mono[0] == expected);
    set_state(IDLE);
  }
  configASSERT(mic_configure(MicChannel::LEFT));
  set_state(SLATE_LISTEN);
  for (unsigned i = 0; i < MIC_BUFFER_SAMPLES / MIC_FRAME_SAMPLES; ++i) {
    configASSERT(mic_submit(stereo, MIC_FRAME_SAMPLES));
  }
  configASSERT(mic_buffered() == MIC_BUFFER_SAMPLES);
  configASSERT(!mic_submit(stereo, 1));
  configASSERT(mic_buffered() == MIC_BUFFER_SAMPLES);
  set_state(SLATE_MUTE);
  configASSERT(mic_buffered() == 0);
  set_state(SLATE_LISTEN);
  configASSERT(mic_submit(stereo, 1));
  set_state(SLATE_TRANSCRIBE);
  configASSERT(mic_read(mono, 1) == 1 && mono[0] == 1000);
  set_state(IDLE);
  ESP_LOGI("slate.sim", "PASS: controller, mic channels, capture gating, buffer overflow and turn reset");
}

static bool read_exact(void* destination, size_t size) {
  auto bytes = static_cast<uint8_t*>(destination);
  size_t received = 0;
  while (received < size) {
    int count = uart_read_bytes(UART_NUM_1, bytes + received, size - received, pdMS_TO_TICKS(5000));
    if (count <= 0) return false;
    received += count;
  }
  return true;
}

static void reply(uint8_t status, const int16_t* samples = nullptr, size_t count = 0) {
  size_t size = count * sizeof(int16_t);
  uint8_t header[] = {status, uint8_t(size), uint8_t(size >> 8)};
  uart_write_bytes(UART_NUM_1, header, sizeof(header));
  if (size) uart_write_bytes(UART_NUM_1, samples, size);
}

extern "C" void app_main() {
  ESP_LOGI("slate.sim", "PCM injection after PDM conversion; no physical I/O");
  check_pipeline();
  uart_config_t config = {};
  config.baud_rate = 921600;
  config.data_bits = UART_DATA_8_BITS;
  config.parity = UART_PARITY_DISABLE;
  config.stop_bits = UART_STOP_BITS_1;
  config.flow_ctrl = UART_HW_FLOWCTRL_DISABLE;
  config.source_clk = UART_SCLK_DEFAULT;
  ESP_ERROR_CHECK(uart_param_config(UART_NUM_1, &config));
  ESP_ERROR_CHECK(uart_driver_install(UART_NUM_1, 4096, 0, 0, nullptr, 0));
  ESP_LOGI("slate.sim", "Audio bridge ready on UART1");
  for (;;) {
    uint8_t header[3];
    if (!read_exact(header, sizeof(header))) {
      set_state(IDLE);
      uart_flush_input(UART_NUM_1);
      continue;
    }
    size_t bytes = header[1] | (size_t(header[2]) << 8);
    static int16_t stereo[MIC_FRAME_SAMPLES * 2];
    static int16_t mono[MIC_FRAME_SAMPLES];
    if (bytes > sizeof(stereo) || (bytes && !read_exact(stereo, bytes))) {
      set_state(IDLE);
      reply(1);
      uart_flush_input(UART_NUM_1);
      continue;
    }
    auto payload = reinterpret_cast<uint8_t*>(stereo);
    if (header[0] == 1 && bytes == 1 && slate_get_state() == IDLE && mic_configure(static_cast<MicChannel>(payload[0]))) {
      set_state(SLATE_LISTEN);
      reply(0);
    } else if (header[0] == 2 && bytes && bytes % 4 == 0 && slate_get_state() == SLATE_LISTEN) {
      if (mic_submit(stereo, bytes / 4)) {
        reply(0, mono, mic_read(mono, MIC_FRAME_SAMPLES));
      } else {
        set_state(SLATE_ERROR);
        reply(2);
      }
    } else if (header[0] == 3 && bytes == 0 && slate_get_state() == SLATE_LISTEN) {
      set_state(SLATE_TRANSCRIBE);
      reply(0, mono, mic_read(mono, MIC_FRAME_SAMPLES));
    } else if (header[0] == 4 && bytes == 0) {
      set_state(IDLE);
      reply(0);
    } else {
      reply(1);
    }
  }
}
