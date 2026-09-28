idf_component_register(
    SRCS "controller.cpp" "simulation.cpp" "mic.cpp" "display.cpp"
    INCLUDE_DIRS "."
    REQUIRES log freertos esp_driver_uart
)
