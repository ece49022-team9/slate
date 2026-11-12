#pragma once
void oled_start();

// Poll until the draw task has blanked the screen and stopped using SPI. No restart.
bool oled_stop();
