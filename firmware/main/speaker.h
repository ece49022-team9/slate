#pragma once

#include <stddef.h>
#include <stdint.h>

constexpr int SPEAKER_RATE = 16000;

void speaker_start();

size_t speaker_write(const uint8_t* data, size_t bytes);
void speaker_play(const uint8_t* pcm, size_t bytes);
void speaker_flush();

bool speaker_stop();
void speaker_test();
