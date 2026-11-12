from pathlib import Path

Import("env")

sdkconfig = env.GetProjectOption("custom_sdkconfig", "")
if sdkconfig:
    idf = Path(env.PioPlatform().get_package_dir("framework-espidf"))
    nimble = idf / "components/bt/host/nimble"
else:
    # Use headers shipped with the exact precompiled libraries (including their
    # static-to-dynamic NimBLE configuration), not a separate ESP-IDF checkout.
    libs = Path(env.PioPlatform().get_package_dir("framework-arduinoespressif32-libs"))
    nimble = libs / env.BoardConfig().get("build.mcu") / "include/bt/host/nimble"
env.Append(
    CPPPATH=[
        str(nimble / path)
        for path in (
            "esp-hci/include",
            "port/include",
            "nimble/nimble/include",
            "nimble/nimble/host/include",
            "nimble/nimble/host/services/gap/include",
            "nimble/nimble/host/services/gatt/include",
            "nimble/porting/nimble/include",
            "nimble/porting/npl/freertos/include",
            "../../porting/include",
            "nimble/nimble/transport/include",
        )
    ]
)

if "CONFIG_BT_CONTROLLER_DISABLED=y" in sdkconfig:
    env.AddBuildMiddleware(lambda node: None, "*esp32-hal-bt.c")
