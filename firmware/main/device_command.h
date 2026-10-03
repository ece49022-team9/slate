#pragma once
#include <ArduinoJson.h>

void device_command(JsonDocument& receipt, const char* id, const char* operation,
                    JsonVariantConst arguments);
void device_serial_command(char* line);
