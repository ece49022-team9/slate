#include "Arduino.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/stream_buffer.h"
#include "driver/i2s_pdm.h"
#include <math.h>

#include "mic.h"

// Avoid OLED SPI pins 5,16,17,18,19,23 and strapping pins 0,2,12,15
#define PDM_CLK_PIN  GPIO_NUM_26
#define PDM_DATA_PIN GPIO_NUM_32

#define MIC_SAMPLE_RATE   16000                      // 16 kHz mono = standard for speech-to-text
#define MIC_FRAME_SAMPLES 320                        // 20 ms per frame @ 16 kHz
#define MIC_GAIN          4                          // digital gain, tune
#define MIC_STREAM_BYTES  (MIC_SAMPLE_RATE * 2 * 1)  // ~1 s of 16-bit audio

#define MIC_METER 1   // 1 = print level meter while in LISTEN, 0 = silent

static i2s_chan_handle_t    rx_chan      = NULL;
static StreamBufferHandle_t mic_stream   = NULL;   // consumer (Wi-Fi task) reads from here
static volatile bool        micStreaming = false;  // set by mic_set_state()

static void mic_init() {
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num  = 6;
  chan_cfg.dma_frame_num = 240;
  ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx_chan));

  i2s_pdm_rx_config_t pdm_cfg = {};
  pdm_cfg.clk_cfg  = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE);
  pdm_cfg.slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
  // SEL is fixed on the 4-pin JST breakout. If you read pure silence, swap LEFT <-> RIGHT.
  pdm_cfg.slot_cfg.slot_mask = I2S_PDM_SLOT_LEFT;

  pdm_cfg.gpio_cfg.clk = PDM_CLK_PIN;
  pdm_cfg.gpio_cfg.din = PDM_DATA_PIN;
  pdm_cfg.gpio_cfg.invert_flags.clk_inv = false;

  ESP_ERROR_CHECK(i2s_channel_init_pdm_rx_mode(rx_chan, &pdm_cfg));
  ESP_ERROR_CHECK(i2s_channel_enable(rx_chan));

  mic_stream = xStreamBufferCreate(MIC_STREAM_BYTES, MIC_FRAME_SAMPLES * sizeof(int16_t));
  configASSERT(mic_stream);
}

static void mic_process(int16_t *buf, size_t n, float &rms, int &peak) {
  static float x_prev = 0.0f, y_prev = 0.0f;
  const float R = 0.995f;  // ~13 Hz high-pass at 16 kHz
  double sumSq = 0.0;
  peak = 0;

  for (size_t i = 0; i < n; i++) {
    float x = (float)buf[i];
    float y = x - x_prev + R * y_prev;  // y[n] = x[n] - x[n-1] + R*y[n-1]
    x_prev = x;
    y_prev = y;

    int32_t s = (int32_t)(y * MIC_GAIN);
    if (s >  32767) s =  32767;
    if (s < -32768) s = -32768;
    buf[i] = (int16_t)s;

    sumSq += (double)s * s;
    int a = abs(s);
    if (a > peak) peak = a;
  }
  rms = sqrtf(sumSq / n);
}

// Mic pipeline: read -> process -> stream -> debug print
static void Mic_Task(void *pvParameters) {
  static int16_t buf[MIC_FRAME_SAMPLES];
  size_t bytes_read = 0;
  uint32_t lastPrint = 0;

  while (true) {
    if (i2s_channel_read(rx_chan, buf, sizeof(buf), &bytes_read, portMAX_DELAY) != ESP_OK) {
      continue;
    }
    size_t n = bytes_read / sizeof(int16_t);

    float rms; int peak;
    mic_process(buf, n, rms, peak);

    if (micStreaming) {
      xStreamBufferSend(mic_stream, buf, n * sizeof(int16_t), 0);  // drops if nobody reads yet
    }

#if MIC_METER
    if (micStreaming && millis() - lastPrint > 250) {
      lastPrint = millis();
      float dbfs = (rms > 0) ? 20.0f * log10f(rms / 32768.0f) : -96.0f;
      int bars = constrain((int)((dbfs + 60.0f) / 2.0f), 0, 30);  // -60..0 dBFS -> 0..30
      Serial.printf("rms=%6.0f peak=%5d %6.1f dBFS |", rms, peak, dbfs);
      for (int i = 0; i < bars; i++) Serial.print('#');
      Serial.println();
    }
#endif
  }
}

// ---------------------------------------------------------------------------
//  Public API
// ---------------------------------------------------------------------------
void mic_start() {
  mic_init();
  xTaskCreate(Mic_Task, "Mic_Task", 4096, NULL, 6, NULL);  // above display (5): audio can't drop
}

void mic_set_state(SlateState s) { micStreaming = (s == SLATE_LISTEN); }

StreamBufferHandle_t mic_get_stream() { return mic_stream; }

// TODO (later), consumer side in the Wi-Fi/STT task:
//   int16_t chunk[320];
//   size_t got = xStreamBufferReceive(mic_get_stream(), chunk, sizeof(chunk), pdMS_TO_TICKS(50));
//   if (got) { /* send chunk over websocket/HTTP */ }