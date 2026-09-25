#include "Arduino.h"
#include <Wire.h>
#include "Adafruit_DRV2605.h"

#include "haptics.h"

// Default ESP32 I2C pins, clear of the OLED (5,16-19,23) and mic (26,32)
#define HAPTIC_SDA_PIN 21
#define HAPTIC_SCL_PIN 22

static Adafruit_DRV2605 drv;
static bool haptics_ok = false;

static void haptic_play(uint8_t effect) {
  if (!haptics_ok) return;
  drv.setWaveform(0, effect);  // slot 0 = the effect
  drv.setWaveform(1, 0);       // slot 1 = end marker
  drv.go();
}

// ---------------------------------------------------------------------------
//  Public API
// ---------------------------------------------------------------------------
void haptics_start() {
  Wire.begin(HAPTIC_SDA_PIN, HAPTIC_SCL_PIN);
  if (!drv.begin()) {
    Serial.println("[haptics] DRV2605 not found, haptics disabled");
    return;
  }
  drv.selectLibrary(1);              // 1-5 = ERM libraries; for an LRA use 6 + drv.useLRA()
  drv.setMode(DRV2605_MODE_INTTRIG);
  haptics_ok = true;
  Serial.println("[haptics] ok");
}

// Called only from main's control task, so I2C is never used from two tasks at once.
void haptics_set_state(SlateState s) {
  switch (s) {
    case SLATE_LISTEN:  haptic_play(1);  break;  // strong click
    case SLATE_MUTE:    haptic_play(10); break;  // double click
    case SLATE_RESPOND: haptic_play(7);  break;  // soft bump
    case SLATE_ERROR:   haptic_play(14); break;  // strong buzz
    default: break;                               // IDLE, TRANSCRIBE: no cue
  }
}