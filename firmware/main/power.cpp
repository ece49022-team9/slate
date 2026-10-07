#include "power.h"

#include <Arduino.h>
#include <Wire.h>

static constexpr int PWR_HOLD = 1;
static constexpr int PWR_SENSE = 2;
static constexpr int FG_ALRT = 18;
static constexpr int CHG_STAT = 40;
static constexpr int PGOOD = 41;

static constexpr int I2C_SDA = 8;
static constexpr int I2C_SCL = 9;

static constexpr uint8_t MAX17048_ADDR = 0x36;

static constexpr unsigned long LONG_PRESS_MS = 2000;

static unsigned long button_pressed_at = 0;
static bool shutdown_requested = false;

static float battery_percent = 0;
static float battery_voltage = 0;

static uint16_t read_register(uint8_t reg) {
    Wire.beginTransmission(MAX17048_ADDR);
    Wire.write(reg);

    if (Wire.endTransmission(false) != 0) {
        return 0;
    }

    if (Wire.requestFrom(MAX17048_ADDR, (uint8_t)2) != 2) {
        return 0;
    }

    uint16_t value = uint16_t(Wire.read()) << 8;
    value |= Wire.read();

    return value;
}

static void read_battery() {
    // MAX17048 SOC register: 0x04
    uint16_t soc = read_register(0x04);

    // MAX17048 VCELL register: 0x02
    uint16_t vcell = read_register(0x02);

    battery_percent = float(soc >> 8) + float(soc & 0xFF) / 256.0f;

    // Each MAX17048 VCELL bit represents 78.125 uV.
    battery_voltage = float(vcell) * 0.000078125f;
}

void power_start() {
    // Keep the board powered immediately.
    pinMode(PWR_HOLD, OUTPUT);
    digitalWrite(PWR_HOLD, HIGH);

    pinMode(PWR_SENSE, INPUT);
    pinMode(FG_ALRT, INPUT_PULLUP);
    pinMode(CHG_STAT, INPUT_PULLUP);
    pinMode(PGOOD, INPUT_PULLUP);

    Wire.begin(I2C_SDA, I2C_SCL);

    read_battery();

    Serial.printf(
        "power.boot: battery=%.1f%% voltage=%.3fV charging=%s usb=%s\n",
        battery_percent,
        battery_voltage,
        power_is_charging() ? "yes" : "no",
        power_usb_connected() ? "yes" : "no"
    );
}

void power_update() {
    bool pressed = digitalRead(PWR_SENSE) == LOW;

    if (pressed) {
        if (button_pressed_at == 0) {
            button_pressed_at = millis();
        }

        if (!shutdown_requested &&
            millis() - button_pressed_at >= LONG_PRESS_MS) {

            shutdown_requested = true;

            Serial.println("power: long press, shutting down");

            digitalWrite(PWR_HOLD, LOW);
        }
    } else {
        button_pressed_at = 0;
    }

    static unsigned long last_battery_read = 0;

    if (millis() - last_battery_read >= 5000) {
        last_battery_read = millis();

        read_battery();

        Serial.printf(
            "power: battery=%.1f%% voltage=%.3fV charging=%s usb=%s\n",
            battery_percent,
            battery_voltage,
            power_is_charging() ? "yes" : "no",
            power_usb_connected() ? "yes" : "no"
        );
    }
}

float power_battery_percent() {
    return battery_percent;
}

float power_battery_voltage() {
    return battery_voltage;
}

bool power_is_charging() {
    return digitalRead(CHG_STAT) == LOW;
}

bool power_usb_connected() {
    return digitalRead(PGOOD) == LOW;
}