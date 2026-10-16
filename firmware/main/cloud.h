#pragma once
#include <stddef.h>
#include <stdint.h>
#include "slate_state.h"

void cloud_start();
void cloud_provision(const char* line);
void cloud_state(SlateState state);
void cloud_audio(const int16_t* samples, size_t count);

void cloud_toggle_live();
bool cloud_live();
void cloud_audio_tap(bool enabled);
void cloud_tap(uint8_t marker, const uint8_t* payload, size_t length);
