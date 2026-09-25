#include "Arduino.h"
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1351.h>
#include <SPI.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>

#include "display.h"

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

// States now come from slate_state.h (don't #define them here)

//static Adafruit_SSD1351 oled(WIDTH, HEIGHT, &SPI, CS_PIN, DC_PIN, RST_PIN);
static Adafruit_SSD1351 oled(WIDTH, HEIGHT, CS_PIN, DC_PIN, MOSI_PIN, SCLK_PIN, RST_PIN);
static int prev[WIDTH];
static volatile SlateState dispState = IDLE;   // written by main via display_set_state()

static void init_display() {
  SPI.begin(SCLK_PIN, MISO_PIN, MOSI_PIN, CS_PIN);
  oled.begin();
  oled.fillScreen(BLACK);
}

static void init_wave() {
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

static void Display_Task(void *pvParameters) {
  while (true) {
    switch (dispState) {
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

// ---------------------------------------------------------------------------
//  Public API
// ---------------------------------------------------------------------------
void display_set_state(SlateState s) { dispState = s; }

void display_start() {
  Serial.println("[display] init");
  init_display();
  Serial.println("[display] begin done");

  oled.fillScreen(RED);        // solid red flash = SPI + panel working
  delay(500);
  oled.fillScreen(BLACK);

  init_wave();
  BaseType_t ok = xTaskCreate(Display_Task, "Display_Task", 4096, NULL, 5, NULL);
  Serial.printf("[display] task %s\n", ok == pdPASS ? "started" : "FAILED");
}