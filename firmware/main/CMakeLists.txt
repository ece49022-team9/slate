idf_component_register(
    SRCS "controller.cpp" "simulation.cpp"
    INCLUDE_DIRS "."
    REQUIRES log freertos
)
