#pragma once

void ble_setup_boot(bool requested);
void ble_setup_start();
void ble_setup_status(const char* line);
void ble_setup_finish();
bool ble_setup_active();
