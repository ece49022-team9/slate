# Hardware

Keep the team's native schematic and board sources here, including any project-specific libraries needed to open them. The current bring-up board uses an ESP32-WROOM-32 module; its microphone, OLED, and haptic GPIO wiring is not yet confirmed. Record the pin map alongside these sources when it is known. Firmware configuration must follow that hardware revision.

Open `ESP32_PRELIM_DESIGN.kicad_pro` with KiCad 10. The preliminary schematic was imported unchanged from the team's `hw` and `fw` branches. The PCB file is an empty board, not a placed or routed design. Circuit correctness and fabrication readiness have not been established.

KiCad 10.0.5 can export the schematic netlist, but reports annotation errors. Resolve those in the schematic editor before treating the netlist as ready for board work.
