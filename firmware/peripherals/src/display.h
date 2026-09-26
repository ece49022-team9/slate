#pragma once
#include "slate_state.h"

void display_start();                  // init OLED + start the display task
void display_set_state(SlateState s);  // called by main when state changes