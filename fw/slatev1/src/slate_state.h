#pragma once
#include <stdint.h>

// Values match the serial test keys 0-5.
enum SlateState : uint8_t {
  IDLE = 0,
  SLATE_LISTEN,
  SLATE_MUTE,
  SLATE_TRANSCRIBE,
  SLATE_RESPOND,
  SLATE_ERROR
};

// Any task (serial, API/network, buttons) calls this to ask for a state change.
// main.cpp's control task applies it and pushes it to every component.
void       slate_request_state(SlateState s);
SlateState slate_get_state();