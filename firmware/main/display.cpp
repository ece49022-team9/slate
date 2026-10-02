#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstring>
#include <glcdfont.c>
#include "freertos/FreeRTOS.h"
#include "display.h"

static std::atomic<SlateState> current_state{SLATE_ERROR};
static std::atomic<unsigned> revision{0};
static std::atomic<unsigned> audio_level{0};
static portMUX_TYPE config_lock = portMUX_INITIALIZER_UNLOCKED;
static DisplayConfig config{false, 0xffffff, 27.0f, "", 0};

bool display_set_orb(uint32_t color, float radius) {
  if (color > 0xffffff || !std::isfinite(radius) || radius < 10 || radius > 45) return false;
  portENTER_CRITICAL(&config_lock);
  config.custom = true;
  config.color = color;
  config.radius = radius;
  revision.fetch_add(1);
  portEXIT_CRITICAL(&config_lock);
  return true;
}

void display_set_text(const char* text) {
  portENTER_CRITICAL(&config_lock);
  std::strncpy(config.text, text, sizeof(config.text) - 1);
  config.text[sizeof(config.text) - 1] = 0;
  revision.fetch_add(1);
  portEXIT_CRITICAL(&config_lock);
}

void display_snapshot(DisplayConfig& value) {
  portENTER_CRITICAL(&config_lock);
  value = config;
  value.revision = revision.load();
  portEXIT_CRITICAL(&config_lock);
}

void display_set_state(SlateState state) {
  current_state.store(state);
  audio_level.store(0);
  revision.fetch_add(1);
}

SlateState display_get_state() { return current_state.load(); }
unsigned display_revision() { return revision.load(); }

void display_submit_audio(float rms) {
  unsigned target = rms > 60.0f ? std::min(1000u, unsigned((rms - 60.0f) * 2.0f)) : 0;
  unsigned previous = audio_level.load();
  audio_level.store(target > previous ? (3 * target + previous) / 4
                                      : (target + 7 * previous) / 8);
}

void display_render(SlateState state, uint32_t frame, uint16_t* pixels) {
  constexpr uint16_t colors[] = {0xffff, 0x07e0, 0x0000, 0x001f, 0xffe0, 0xf81f};
  std::fill_n(pixels, DISPLAY_PIXELS, uint16_t{0});
  if (static_cast<unsigned>(state) > SLATE_ERROR || state == SLATE_MUTE) return;
  DisplayConfig settings;
  display_snapshot(settings);
  constexpr float tau = 6.283185307179586f;
  float level = state == SLATE_LISTEN ? audio_level.load() / 1000.0f : 0.0f;
  float radius = settings.radius + 5.0f * std::sin(tau * float(frame % 120) / 120.0f)
                 + 11.0f * level;
  float core_scale = 1.0f / (radius * radius);
  float halo_scale = 1.0f / ((radius + 12.0f) * (radius + 12.0f));
  uint16_t color = state == SLATE_LISTEN
                       ? (uint16_t(31.0f * level + 0.5f) << 11) | 0x07e0 |
                             uint16_t(31.0f * level + 0.5f)
                       : colors[state];
  if (settings.custom) {
    color = ((settings.color >> 19) << 11) |
            (((settings.color >> 10) & 63) << 5) | ((settings.color >> 3) & 31);
  }
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
  if (settings.text[0]) {
    std::fill_n(pixels + 96 * DISPLAY_WIDTH, 32 * DISPLAY_WIDTH, uint16_t{0});
    for (unsigned i = 0; settings.text[i]; ++i) {
      unsigned x = 1 + (i % 21) * 6;
      unsigned y = 96 + (i / 21) * 8;
      for (unsigned column = 0; column < 5; ++column) {
        uint8_t bits = font[unsigned(settings.text[i]) * 5 + column];
        for (unsigned row = 0; row < 8; ++row) {
          if (bits & (1 << row)) pixels[(y + row) * DISPLAY_WIDTH + x + column] = 0xffff;
        }
      }
    }
  }
}
