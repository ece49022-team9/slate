#pragma once

#include <stddef.h>
#include <stdint.h>

void speaker_start();

size_t speaker_write(const uint8_t* data, size_t bytes);

void speaker_stop();