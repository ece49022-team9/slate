import subprocess
from pathlib import Path

Import("env")

PIO_CORE = "6.2.0"

python = env.subst("$PYTHONEXE")
installed = subprocess.run(
    [python, "-c", "import platformio; print(platformio.__version__)"],
    capture_output=True,
    text=True,
).stdout.strip()
if installed and installed != PIO_CORE:
    pinned = subprocess.run(
        [python, "-m", "uv", "pip", "install", "--quiet", f"--python={python}",
         f"pioarduino=={PIO_CORE}"]
    )
    if pinned.returncode:
        print(f"slate.sdk: could not pin pioarduino {PIO_CORE} over {installed}")
    else:
        print(f"slate.sdk: pinned the build environment's pioarduino to {PIO_CORE}")

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
