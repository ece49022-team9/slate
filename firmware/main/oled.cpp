#include <Arduino.h>
#include <atomic>
#include <Adafruit_SSD1351.h>
#include <SPI.h>
#include "board.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "display.h"
#include "oled.h"
#include "perf.h"

static Adafruit_SSD1351* oled;
static uint16_t pixels[DISPLAY_PIXELS];
static std::atomic<bool> stop_requested{false};
static std::atomic<bool> stopped{false};

static void draw(void*) {
  // Preserve the original OLED's 30 ms cadence and animation timebase.
  constexpr TickType_t frame_ticks = pdMS_TO_TICKS(30) > 0 ? pdMS_TO_TICKS(30) : 1;
  TickType_t next_frame = xTaskGetTickCount();
  uint32_t report_started = millis();
  uint32_t frames = 0;
  while (!stop_requested.load()) {
    uint32_t frame = xTaskGetTickCount() / frame_ticks;
    int64_t started = esp_timer_get_time();
    display_render(display_get_state(), frame, pixels);
    int64_t rendered = esp_timer_get_time();
    oled->drawRGBBitmap(0, 0, pixels, DISPLAY_WIDTH, DISPLAY_HEIGHT);
    perf_time(PerfTimer::RENDER, uint32_t(rendered - started));
    perf_time(PerfTimer::SPI, uint32_t(esp_timer_get_time() - rendered));
    perf_stack(PerfTask::OLED);
    ++frames;
    uint32_t elapsed = millis() - report_started;
    if (elapsed >= 2000) {
      Serial.printf("slate.oled: %lu frames in %lu ms (%.1f fps)\n",
                    static_cast<unsigned long>(frames),
                    static_cast<unsigned long>(elapsed),
                    frames * 1000.0f / elapsed);
      perf_report();
      report_started = millis();
      frames = 0;
    }
    vTaskDelayUntil(&next_frame, frame_ticks);
  }
  oled->fillScreen(0);
  SPI.end();
  stopped.store(true); // No further SPI operations after this acknowledgment.
  vTaskDelete(nullptr);
}

bool oled_stop() {
  if (!oled) return true;
  stop_requested.store(true);
  return stopped.load();
}

void oled_start() {
  stop_requested.store(false);
  stopped.store(false);
  SPI.begin(OLED_CLK, -1, OLED_DATA, OLED_CS);
  static Adafruit_SSD1351 panel(DISPLAY_WIDTH, DISPLAY_HEIGHT, &SPI,
                                OLED_CS, OLED_DC, OLED_RESET);
  oled = &panel;
  oled->begin(OLED_SPI_HZ);
#ifdef SLATE_BRINGUP
  // Temporary brightness comparison: driver default master contrast is 0x0F.
  const uint8_t master_contrast = 0x03;
  oled->sendCommand(SSD1351_CMD_CONTRASTMASTER, &master_contrast, 1);
#endif
  oled->setRotation(0);
  oled->fillScreen(0);
#ifdef SLATE_BRINGUP
  oled->fillRect(0, 0, 42, 32, 0xf800);
  oled->fillRect(42, 0, 43, 32, 0x07e0);
  oled->fillRect(85, 0, 43, 32, 0x001f);
  oled->setCursor(4, 48);
  oled->setTextColor(0xffff);
  oled->print("SLATE SSD1351");
  delay(1000);
#endif
  BaseType_t created = xTaskCreate(draw, "OLED", 4096, nullptr, 5, nullptr);
  if (created != pdPASS) {
    ESP_LOGE("slate.oled", "Could not create display task");
    oled->fillScreen(0);
    SPI.end();
    return;
  }
  ESP_LOGI("slate.oled", "SSD1351 ready: 128x128 RGB565");
}
