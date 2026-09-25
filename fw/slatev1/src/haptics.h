#pragma once
#include "slate_state.h"

void haptics_start();                  // init I2C + DRV2605 (disables itself if not found)
void haptics_set_state(SlateState s);  // plays a cue on state changes