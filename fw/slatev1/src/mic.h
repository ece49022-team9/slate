#pragma once
#include "slate_state.h"
#include "freertos/FreeRTOS.h"
#include "freertos/stream_buffer.h"

void                 mic_start();                  // init PDM + start the mic task
void                 mic_set_state(SlateState s);  // streams only in SLATE_LISTEN
StreamBufferHandle_t mic_get_stream();             // for the future network task