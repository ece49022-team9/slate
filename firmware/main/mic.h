#pragma once
#include <stddef.h>
#include <stdint.h>
#include "slate_state.h"

constexpr size_t MIC_FRAME_SAMPLES = 320;
constexpr size_t MIC_BUFFER_SAMPLES = 16000;
constexpr unsigned MIC_SAMPLE_RATE = 16000;

enum class MicChannel : uint8_t { LEFT, RIGHT, MIX };

void mic_start();
bool mic_configure(MicChannel channel);
void mic_set_state(SlateState state);
bool mic_submit(const int16_t* stereo, size_t frames);
size_t mic_read(int16_t* mono, size_t capacity);
size_t mic_buffered();
