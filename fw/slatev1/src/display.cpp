#include "Arduino.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1351.h>
#include <SPI.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "math.h"

#include "HardwareSerial.h"




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


//STATES (CHANGES VIA API CALLS)
#define IDLE 0
#define SLATE_LISTEN 1
#define SLATE_MUTE 2
#define SLATE_TRANSCRIBE 3
#define SLATE_RESPOND 4
#define SLATE_ERROR 5

Adafruit_SSD1351 oled(WIDTH, HEIGHT, &SPI, CS_PIN, DC_PIN, RST_PIN);
int prev[WIDTH];

static void init_display() {
  SPI.begin(SCLK_PIN, MISO_PIN, MOSI_PIN, CS_PIN);
  oled.begin();
  oled.fillScreen(BLACK);
}

static void init_wave()
{
  for (int i = 0; i < WIDTH; i++) {
    int j = round(63.5 + 32 * sin(2 * M_PI * i / 64));
    prev[i] = j;
    oled.drawPixel(i, j, RED);
  }
}

static void drawWaveFrame(uint16_t color, float speed) {
  static float phase = 0.0f;
  static uint16_t lastColor = BLACK;
  bool colorChanged = (color != lastColor);
  
  for (int i = 0; i < WIDTH; i++) {
    int j = round(63.5 + 32 * sin((2 * M_PI * i / 64) - phase));
    if (j != prev[i] || colorChanged) {
      oled.drawPixel(i, prev[i], BLACK);  // erase old
      oled.drawPixel(i, j, color);        // draw new
      prev[i] = j;
    }
  }

  lastColor = color;
  phase += speed;
  if (phase >= 2 * M_PI) phase -= 2 * M_PI;
}

volatile int currentState = IDLE;   //API callbacks can write this

static void Slate_Task(void *pvParameters) {
  while (true) {
    // poll serial EVERY frame
    if (Serial.available()) {
      char c = Serial.read();
      Serial.printf("got: %c\n", c);   //echo
      switch (c) {
        case '0': currentState = IDLE;             break;
        case '1': currentState = SLATE_LISTEN;     break;
        case '2': currentState = SLATE_MUTE;       break;
        case '3': currentState = SLATE_TRANSCRIBE; break;
        case '4': currentState = SLATE_RESPOND;    break;
        case '5': currentState = SLATE_ERROR;      break;
      }
    }
    //delete polling once API callbacks are implemented to change currentState

    switch (currentState) {
      case IDLE:             drawWaveFrame(WHITE,   0.1f); break;
      case SLATE_LISTEN:     drawWaveFrame(GREEN,   0.1f); break;
      case SLATE_MUTE:       drawWaveFrame(BLACK,   0.1f); break;
      case SLATE_TRANSCRIBE: drawWaveFrame(BLUE,    0.1f); break;
      case SLATE_RESPOND:    drawWaveFrame(YELLOW,  0.1f); break;
      case SLATE_ERROR:      drawWaveFrame(MAGENTA, 0.1f); break;
      default: break;
    }
    vTaskDelay(pdMS_TO_TICKS(30));
  }
}

extern "C" void app_main(void) {
  initArduino();
  Serial.begin(115200);
  delay(1500);
  Serial.println("boot ok"); 
  init_display();
  init_wave();
  xTaskCreate(Slate_Task, "Slate_Task", 4096, NULL, 5, NULL);
}