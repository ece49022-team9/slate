#include "Arduino.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1351.h>
#include <SPI.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "math.h"


// SPI + control pins for the ESP32-WROOM-32E test board
#define SCLK_PIN 18
#define MISO_PIN 19
#define MOSI_PIN 23
#define CS_PIN    5
#define DC_PIN   16
#define RST_PIN  17

#define WIDTH  128
#define HEIGHT 128

// 16-bit RGB565 colors
#define BLACK   0x0000
#define RED     0xF800
#define GREEN   0x07E0
#define BLUE    0x001F
#define CYAN    0x07FF
#define MAGENTA 0xF81F
#define YELLOW  0xFFE0
#define WHITE   0xFFFF

Adafruit_SSD1351 oled(WIDTH, HEIGHT, &SPI, CS_PIN, DC_PIN, RST_PIN);
int prev[WIDTH];

extern "C" void app_main(void) {
  initArduino();

  SPI.begin(SCLK_PIN, MISO_PIN, MOSI_PIN, CS_PIN);
  oled.begin();
  oled.fillScreen(BLACK);

  float phase = 0.0f;
  float speed = 0.1f;

  // Draw initial waveform and record each column's y
  for (int i = 0; i < WIDTH; i++) {
    int j = round(63.5 + 32 * sin(2 * M_PI * i / 64));
    prev[i] = j;
    oled.drawPixel(i, j, RED);
  }

  while (true) {
    for (int i = 0; i < WIDTH; i++) {
      int j = round(63.5 + 32 * sin((2 * M_PI * i / 64) - phase));
      if (j != prev[i]) {
        oled.drawPixel(i, prev[i], BLACK);  // erase old
        oled.drawPixel(i, j, RED);          // draw new
        prev[i] = j;
      }
    }

    phase += speed;
    if (phase >= 2 * M_PI) phase -= 2 * M_PI;

    vTaskDelay(pdMS_TO_TICKS(30));
  }
}
// extern "C" void app_main(void) {
//   initArduino();

//   SPI.begin(SCLK_PIN, MISO_PIN, MOSI_PIN, CS_PIN);
//   oled.begin();
//   oled.fillScreen(BLACK);
//   //Draw intial Waveform across the screen
//   for(int i = 0; i< WIDTH; i++)
//   {
//     int j = round(63.5 + 32 * sin(2 * M_PI * i / 64));
//     oled.drawPixel(i, j, RED);
//   }

//   // float phase = 0.0;
//   // float speed = 0.1;
//   // vTaskDelay(pdMS_TO_TICKS(5000));
//   // oled.fillScreen(BLACK);
  
//   // while (true) {
//   //   //vTaskDelay(pdMS_TO_TICKS(1000)); // Delay for 1 second
    
//   //   for(int i = 0; i< WIDTH; i++)
//   //   {
//   //     int j = round(63.5 + 32 * sin((2 * M_PI * i / 64)  - phase));
//   //     oled.drawPixel(i, j, RED);
//   //   }

//   //   phase += speed;
//   //   if(phase >= 2 * M_PI)
//   //   {
//   //     phase -= 2 * M_PI;
//   //   }
//   //   vTaskDelay(pdMS_TO_TICKS(30));
    

//   // }

  
//}