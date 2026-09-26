#include <Adafruit_SSD1351.h>
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "display.h"
#include "oled.h"

static Adafruit_SSD1351* oled;
static uint16_t pixels[DISPLAY_PIXELS];

static void draw(void*) {
  for (;;) {
    uint32_t frame = xTaskGetTickCount() / pdMS_TO_TICKS(30);
    display_render(display_get_state(), frame, pixels);
    oled->drawRGBBitmap(0, 0, pixels, DISPLAY_WIDTH, DISPLAY_HEIGHT);
    vTaskDelay(pdMS_TO_TICKS(30));
  }
}

void oled_start(int clock_pin, int data_pin, int cs_pin, int dc_pin, int reset_pin) {
  int pins[] = {clock_pin, data_pin, cs_pin, dc_pin, reset_pin};
  for (unsigned i = 0; i < 5; ++i) {
    ESP_ERROR_CHECK(GPIO_IS_VALID_OUTPUT_GPIO(pins[i]) ? ESP_OK : ESP_ERR_INVALID_ARG);
    for (unsigned j = 0; j < i; ++j) {
      ESP_ERROR_CHECK(pins[i] != pins[j] ? ESP_OK : ESP_ERR_INVALID_ARG);
    }
  }
  static Adafruit_SSD1351 panel(DISPLAY_WIDTH, DISPLAY_HEIGHT, cs_pin, dc_pin,
                               data_pin, clock_pin, reset_pin);
  oled = &panel;
  oled->begin();
  oled->setRotation(0);
  oled->fillScreen(0);
  BaseType_t created = xTaskCreate(draw, "OLED", 4096, nullptr, 5, nullptr);
  configASSERT(created == pdPASS);
  ESP_LOGI("slate.oled", "SSD1351 ready: 128x128 RGB565");
}
