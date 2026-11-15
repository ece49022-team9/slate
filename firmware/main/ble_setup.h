#pragma once

void ble_setup_boot(bool requested);
// Optional local command sink. Return false to reject a write (e.g. queue full).
// Normal application calls without a sink retain cloud provisioning.
using BleSetupCommandHandler = bool (*)(const char* line);
void ble_setup_start(BleSetupCommandHandler handler = nullptr);
void ble_setup_report();
void ble_setup_status(const char* line);
void ble_setup_finish();
bool ble_setup_active();
