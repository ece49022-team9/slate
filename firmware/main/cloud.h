#pragma once
#include <stddef.h>
#include <stdint.h>
#include "slate_state.h"

void cloud_start();
void cloud_provision(const char* line);
void cloud_state(SlateState state);
void cloud_audio(const int16_t* samples, size_t count);
