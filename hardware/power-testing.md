# Prototype power support

The ESP32-WROOM-32E power circuit is opt-in. Keep
`[device.power].enabled = false` in `hardware/board.toml` until the wiring is
verified. `make board` generates `board.h`; a `-DPOWER_ENABLED=1` override is
available for bench builds. Disabled power code accesses no power GPIO or I2C.

GPIO assignments: HOLD13, SENSE15, FG_ALRT35, CHG_STAT34, PGOOD39, SDA21, SCL22.
Other peripheral GPIO assignments are unchanged. GPIO15 is a reset strapping
pin; GPIO13 cannot also serve a physical JTAG connection. Status inputs require
external bias, including GPIO34/35/39, which have no internal pull-ups.

`power_start()` runs first in setup and asserts HOLD before other startup work.
It initializes Wire once on GPIO21/22 with a 20 ms transaction timeout. MAX17048
uses address 0x36. The unchanged haptics driver uses the same bus at 0x5A;
Arduino's already-initialized Wire.begin() preserves these pins. The current
application does not start haptics. Bus initialization failure leaves battery
status invalid until reset; missing-gauge read failures are retried every five
seconds. Bus callers must stay in the application task.

`power_update()` reads the battery and inputs and latches a shutdown request
following a debounced two-second active-low press. A startup-held button must
first be released. Battery reads commit voltage and SOC together; failure keeps
last-good values but marks them invalid. No sample is represented by NAN.
`power_status()` also marks data older than ten seconds invalid. Charger
Inactive does not mean full; inconsistent inputs report Invalid. Fuel-gauge
alerts are observed only. There is no automatic low-battery shutdown.

The application stops commands and streaming, requests PDM/OLED stops, waits for
both acknowledgments and any active speaker test, then stops speaker I2S before
calling `power_finish_shutdown()`. PDM stops after its current read (up to the
existing one-second timeout); OLED finishes its frame and blanks the display.
These stops do not support restart. No storage module requires flushing.

A cleanup timeout of three seconds or a peripheral stop failure retains HOLD.
The application stays stopped, possibly with some peripherals still active;
reset/power cycle is required after diagnosis. The timeout cannot interrupt a
blocked driver call. With retained USB/button power after successful cutoff,
the application stays quiescent and does not repeat cleanup or latch release.

## Verification

Run `make board`, board tests, and `tests/test_power_firmware.py`. The latter
checks power.cpp with GPIO/time/I2C fakes and main.cpp with substituted peripheral
stops. Application tests cover acknowledgment ordering, an active speaker
producer, timeout, speaker failure, and retained-power behavior; they do not
validate the actual peripheral driver stops. Build default esp32dev,
POWER_ENABLED=1, and the existing speaker-test environment.

Before enabling hardware power:

1. Verify latch polarity/startup timing, button bias, status pull-ups, I2C rails,
   GPIO15 reset behavior, and GPIO13 availability with a current-limited supply.
2. Confirm existing microphone, OLED and speaker operation with power disabled.
3. Enable power and verify HOLD assertion precedes peripheral startup. Compare
   gauge voltage with a meter; disconnect/reconnect the gauge and check invalid
   data and recovery. Check USB/charging inputs and gauge alerts.
4. Exercise button bounce, short/long presses and a startup-held button. During
   capture/playback, confirm cleanup precedes HOLD LOW. Induce missing cleanup
   acknowledgment and confirm HOLD stays HIGH. Retain USB power after successful
   shutdown and confirm audio/display activity stays stopped.

No physical power behavior is validated by the host tests or QEMU.
