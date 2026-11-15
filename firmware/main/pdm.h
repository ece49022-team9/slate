#pragma once
void pdm_start();
bool pdm_ready();
// Poll until the capture task has released I2S0 before restarting.
bool pdm_stop();
