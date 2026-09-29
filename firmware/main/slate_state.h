#pragma once
#include <stdint.h>

enum SlateState : uint8_t {
  IDLE = 0,
  SLATE_LISTEN,
  SLATE_MUTE,
  SLATE_TRANSCRIBE,
  SLATE_RESPOND,
  SLATE_ERROR
};

void slate_start();
bool slate_request_state(SlateState state);
SlateState slate_get_state();
