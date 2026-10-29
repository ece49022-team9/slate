from pathlib import Path

Import("env")

header = (
    Path(env.subst("$PROJECT_DIR"))
    / "managed_components/espressif__esp_rainmaker/src/ota/esp_rmaker_ota_internal.h"
)
if header.exists():
    source = header.read_text()
    updated = source.replace(
        "#if defined(CONFIG_BT_ENABLED) && !defined(CONFIG_IDF_TARGET_ESP32P4)",
        "#if defined(CONFIG_BT_CONTROLLER_ENABLED) && !defined(CONFIG_IDF_TARGET_ESP32P4)",
    )
    if updated != source:
        header.write_text(updated)
        print("slate.sdk: use the controller check in RainMaker OTA")
