#pragma once
#include <stdint.h>
#include "slate_state.h"

constexpr unsigned DISPLAY_WIDTH = 128;
constexpr unsigned DISPLAY_HEIGHT = 128;
constexpr unsigned DISPLAY_PIXELS = DISPLAY_WIDTH * DISPLAY_HEIGHT;

struct DisplayConfig {
  bool custom;
  uint32_t color;
  float radius;
  char text[65];
  unsigned revision;
};

void display_set_state(SlateState state);
SlateState display_get_state();
unsigned display_revision();
void display_submit_audio(float rms);
bool display_set_orb(uint32_t color, float radius);
void display_set_text(const char* text);
void display_snapshot(DisplayConfig& config);
void display_render(SlateState state, uint32_t frame, uint16_t* pixels);
