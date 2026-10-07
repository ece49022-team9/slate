#pragma once

void power_start();
void power_update();

float power_battery_percent();
float power_battery_voltage();

bool power_is_charging();
bool power_usb_connected();