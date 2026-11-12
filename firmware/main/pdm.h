#pragma once
void pdm_start();

// Request once or poll until the capture task has released I2S. No restart.
bool pdm_stop();
