# Hardware

Keep the team's native schematic and board sources here, including any project-specific libraries needed to open them. Record the actual MCU module, pin map, display, microphones, and haptic parts alongside those sources when adding them. Firmware configuration must follow that hardware revision.

Open `ESP32_PRELIM_DESIGN.kicad_pro` with KiCad 10. The preliminary schematic was imported unchanged from the team's `hw` and `fw` branches. The PCB file is an empty board, not a placed or routed design. Circuit correctness and fabrication readiness have not been established.

KiCad 10.0.5 can export the schematic netlist, but reports annotation errors. Resolve those in the schematic editor before treating the netlist as ready for board work.
