#include <Wire.h>
#include "Adafruit_DRV2605.h"

// in app_main, after Wire.begin():
Wire.begin(SDA_PIN, SCL_PIN);   // ESP32: pass your actual I2C pins
drv.begin();
drv.selectLibrary(1);           // 1–5 = ERM libraries, 6 = LRA
drv.setMode(DRV2605_MODE_INTTRIG);

// fire an effect
drv.setWaveform(0, 1);          // slot 0 = effect #1 (strong click)
drv.setWaveform(1, 0);          // slot 1 = end marker
drv.go();

