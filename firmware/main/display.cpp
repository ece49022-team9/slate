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
  if (static_cast<unsigned>(state) > SLATE_ERROR || state == SLATE_MUTE) return;
  constexpr float tau = 6.283185307179586f;
  float radius = 27.0f + 5.0f * std::sin(tau * float(frame % 120) / 120.0f);
  float core_scale = 1.0f / (radius * radius);
  float halo_scale = 1.0f / ((radius + 12.0f) * (radius + 12.0f));
  uint16_t color = colors[state];
  for (unsigned y = 0; y < DISPLAY_HEIGHT / 2; ++y) {
    float dy = float(y) - 63.5f;
    for (unsigned x = 0; x < DISPLAY_WIDTH / 2; ++x) {
      float dx = float(x) - 63.5f;
      float distance_squared = dx * dx + dy * dy;
      float core = std::max(0.0f, 1.0f - distance_squared * core_scale);
      float edge = std::min(1.0f, core * 4.0f);
      float halo = std::max(0.0f, 1.0f - distance_squared * halo_scale);
      float intensity = std::min(1.0f, edge * edge * (3.0f - 2.0f * edge) *
                                 (0.55f + 0.45f * core) + 0.18f * halo * halo);
      unsigned r = unsigned(float(color >> 11) * intensity + 0.5f);
      unsigned g = unsigned(float((color >> 5) & 63) * intensity + 0.5f);
      unsigned b = unsigned(float(color & 31) * intensity + 0.5f);
      uint16_t pixel = (r << 11) | (g << 5) | b;
      pixels[y * DISPLAY_WIDTH + x] = pixel;
      pixels[y * DISPLAY_WIDTH + DISPLAY_WIDTH - 1 - x] = pixel;
      pixels[(DISPLAY_HEIGHT - 1 - y) * DISPLAY_WIDTH + x] = pixel;
      pixels[(DISPLAY_HEIGHT - 1 - y) * DISPLAY_WIDTH + DISPLAY_WIDTH - 1 - x] = pixel;
    }
  }
}
