#pragma once
#include <stdint.h>

enum class PerfTimer : uint8_t { RENDER, SPI, AUDIO, COUNT };
enum class PerfTask : uint8_t { LOOP, CONTROL, OLED, PDM, CLOUD, COUNT };

void perf_time(PerfTimer timer, uint32_t microseconds);
void perf_stack(PerfTask task);
void perf_report();
