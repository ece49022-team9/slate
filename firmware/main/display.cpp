#include <algorithm>
#include <atomic>
#include <cmath>
#include "display.h"

static std::atomic<SlateState> current_state{SLATE_ERROR};
static std::atomic<unsigned> revision{0};

void display_set_state(SlateState state) {
  current_state.store(state);
  revision.fetch_add(1);
}

SlateState display_get_state() { return current_state.load(); }
unsigned display_revision() { return revision.load(); }

void display_render(SlateState state, uint32_t frame, uint16_t* pixels) {
  constexpr uint16_t colors[] = {0xffff, 0x07e0, 0x0000, 0x001f, 0xffe0, 0xf81f};
  std::fill_n(pixels, DISPLAY_PIXELS, uint16_t{0});
  if (state > SLATE_ERROR || state == SLATE_MUTE) return;
  constexpr double tau = 6.283185307179586;
  double phase = std::fmod(double(frame) * 0.1, tau);
  for (unsigned x = 0; x < DISPLAY_WIDTH; ++x) {
    unsigned y = std::lround(63.5 + 32 * std::sin(tau * x / 64 - phase));
    pixels[y * DISPLAY_WIDTH + x] = colors[state];
  }
}
