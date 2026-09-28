#include "Arduino.h"
#include <Wire.h>
#include "Adafruit_DRV2605.h"

#include "haptics.h"

#define HAPTIC_SDA_PIN 21
#define HAPTIC_SCL_PIN 22

static Adafruit_DRV2605 drv;
static bool haptics_ok = false;

static void haptic_play(uint8_t effect) {
  if (!haptics_ok) return;
  drv.setWaveform(0, effect);
  drv.setWaveform(1, 0);
  drv.go();
}

void haptics_start() {
  Wire.begin(HAPTIC_SDA_PIN, HAPTIC_SCL_PIN);
  if (!drv.begin()) {
    Serial.println("[haptics] DRV2605 not found, haptics disabled");
    return;
  }
  drv.selectLibrary(1);
  drv.setMode(DRV2605_MODE_INTTRIG);
  haptics_ok = true;
  Serial.println("[haptics] ok");
}

void haptics_set_state(SlateState s) {
  switch (s) {
    case SLATE_LISTEN:  haptic_play(1);  break;
    case SLATE_MUTE:    haptic_play(10); break;
    case SLATE_RESPOND: haptic_play(7);  break;
    case SLATE_ERROR:   haptic_play(14); break;
    default: break;
  }
}
