#include <Arduino.h>
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

static void draw(void*) {
  TickType_t next_frame = xTaskGetTickCount();
  uint32_t report_started = millis();
  uint32_t frames = 0;
  for (;;) {
    uint32_t frame = xTaskGetTickCount() / pdMS_TO_TICKS(30);
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
    vTaskDelayUntil(&next_frame, pdMS_TO_TICKS(30));
  }
}

void oled_start() {
  SPI.begin(OLED_CLK, -1, OLED_DATA, OLED_CS);
  static Adafruit_SSD1351 panel(DISPLAY_WIDTH, DISPLAY_HEIGHT, &SPI,
                                OLED_CS, OLED_DC, OLED_RESET);
  oled = &panel;
  oled->begin(OLED_SPI_HZ);
  oled->setRotation(0);
  oled->fillScreen(0);
  BaseType_t created = xTaskCreate(draw, "OLED", 4096, nullptr, 5, nullptr);
  configASSERT(created == pdPASS);
  ESP_LOGI("slate.oled", "SSD1351 ready: 128x128 RGB565");
}
