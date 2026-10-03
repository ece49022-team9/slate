#include <Arduino.h>
#include <ArduinoJson.h>
#include <ctype.h>
#include <string.h>
#include "device_command.h"
#include "display.h"
#include "slate_state.h"

void device_command(JsonDocument& receipt, const char* id, const char* operation,
                    JsonVariantConst arguments) {
  receipt["type"] = "receipt";
  receipt["request_id"] = id;
  bool valid = strlen(id) == 32;
  for (size_t i = 0; id[i]; ++i) valid &= isxdigit(id[i]) != 0;
  bool accepted = false;
  if (valid && !strcmp(operation, "get_status")) {
    accepted = true;
  } else if (valid && !strcmp(operation, "set_orb")) {
    const char* hex = arguments["color"] | "";
    valid = strlen(hex) == 7 && hex[0] == '#' && arguments["radius"].is<float>();
    for (size_t i = 1; valid && i < 7; ++i) valid &= isxdigit(hex[i]) != 0;
    unsigned color;
    if (valid && sscanf(hex + 1, "%x", &color) == 1) {
      accepted = display_set_orb(color, arguments["radius"].as<float>());
    }
  } else if (valid && !strcmp(operation, "show_text")) {
    const char* text = arguments["text"] | "";
    accepted = arguments["text"].is<const char*>() && strlen(text) <= 64 &&
               arguments["text"].as<JsonString>().size() == strlen(text);
    for (size_t i = 0; accepted && text[i]; ++i) accepted &= text[i] >= 32 && text[i] <= 126;
    if (accepted) display_set_text(text);
  }
  if (!accepted) {
    receipt["error"] = "invalid command";
    return;
  }
  DisplayConfig config;
  display_snapshot(config);
  char hex[8];
  snprintf(hex, sizeof(hex), "#%06lx", static_cast<unsigned long>(config.color));
  receipt["operation"] = operation;
  receipt["revision"] = config.revision;
  receipt["state"] = unsigned(slate_get_state());
  receipt["color"] = hex;
  receipt["radius"] = config.radius;
  receipt["text"] = config.text;
  receipt["custom"] = config.custom;
}

void device_serial_command(char* line) {
  char id[33] = {}, operation[8] = {}, argument[129] = {}, extra[2] = {};
  int consumed = 0;
  int fields = sscanf(line, "%32s %7s %128[^\n]%n", id, operation, argument, &consumed);
  if (strlen(id) != 32) {
    Serial.println("slate.device.error: invalid request id");
    return;
  }
  JsonDocument arguments, receipt;
  const char* name = "invalid";
  if (fields == 2 && !strcmp(operation, "status")) {
    name = "get_status";
  } else if (fields == 3 && !line[consumed] && !strcmp(operation, "orb")) {
    char hex[7] = {};
    float radius;
    if (sscanf(argument, "%6s %f %1s", hex, &radius, extra) == 2 && strlen(hex) == 6) {
      char color[8];
      snprintf(color, sizeof(color), "#%s", hex);
      arguments["color"] = color;
      arguments["radius"] = radius;
      name = "set_orb";
    }
  } else if (fields >= 2 && (fields == 2 || !line[consumed]) && !strcmp(operation, "text")) {
    size_t length = strlen(argument);
    char text[65] = {};
    bool valid = length <= 128 && length % 2 == 0;
    for (size_t i = 0; valid && i < length; i += 2) {
      unsigned value;
      char pair[] = {argument[i], argument[i + 1], 0};
      valid = isxdigit(pair[0]) && isxdigit(pair[1]) &&
              sscanf(pair, "%x", &value) == 1 && value >= 32 && value <= 126;
      if (valid) text[i / 2] = value;
    }
    if (valid) {
      arguments["text"] = text;
      name = "show_text";
    }
  }
  device_command(receipt, id, name, arguments.as<JsonVariantConst>());
  if (!receipt["error"].is<const char*>()) {
    const char* text = receipt["text"];
    char hex[129] = {};
    for (unsigned i = 0; text[i]; ++i) sprintf(hex + 2 * i, "%02x", unsigned(text[i]));
    receipt["text_hex"] = hex;
    receipt.remove("text");
  }
  receipt.remove("type");
  Serial.printf("slate.device:%s ", id);
  serializeJson(receipt, Serial);
  Serial.println();
}
