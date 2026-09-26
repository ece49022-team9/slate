#pragma once
#include "slate_state.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"

void                 mic_start();
void                 mic_set_state(SlateState s);
StreamBufferHandle_t mic_get_stream();
