#pragma once
#include <stdint.h>
#include "slate_state.h"

constexpr unsigned DISPLAY_WIDTH = 128;
constexpr unsigned DISPLAY_HEIGHT = 128;
constexpr unsigned DISPLAY_PIXELS = DISPLAY_WIDTH * DISPLAY_HEIGHT;

void display_set_state(SlateState state);
SlateState display_get_state();
unsigned display_revision();
void display_render(SlateState state, uint32_t frame, uint16_t* pixels);
