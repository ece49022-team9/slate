# ESP32-S3-WROOM-1-N16R8 bring-up

Use `slate-s3-bringup` for initial hardware work. It runs without Wi-Fi,
credentials, cloud, haptics, microSD, I2C, or power-management initialization.
`slate-s3` retains the normal voice application. `esp32dev` and `speaker-test`
retain the prototype wiring. Arduino and the existing pinned pioarduino platform
are preserved (Arduino 3.3.12 / ESP-IDF 5.5.5). Bring-up uses precompiled
ESP-IDF libraries and only the OLED/GFX/BusIO dependencies. It deliberately
does not inherit `custom_sdkconfig`, which triggers a full ESP-IDF source build.
The normal application environments retain their custom SDK settings.

## Configuration and GPIOs

`hardware/board.toml` describes the final N16R8 module;
`hardware/board-prototype.toml` preserves the prototype. `make board` validates
both and generates the conditional `firmware/main/board.h`. The S3 environments
select the final map with `SLATE_BOARD_S3`; other environments use the prototype.
Simulator board readers explicitly select the prototype map.

| Signal | Final GPIO |
| --- | --- |
| MP34DT01 PDM CLK / DATA | 4 / 5 |
| MAX98357A BCLK / DOUT / SD_MODE / LRCLK | 6 / 15 / 16 / 17 |
| SSD1351 CS / MOSI / CLK / DC / RESET | 10 / 11 / 12 / 14 / 21 |
| Reserved HAPTICS_EN | 7 |
| Reserved I2C SDA / SCL | 8 / 9 |
| Reserved SD MISO / CS / CD | 13 / 47 / 42 |
| Reserved PWR_HOLD / PWR_SENSE | 1 / 2 |
| Reserved FG_ALERT / CHG_STAT / PGOOD | 18 / 40 / 41 |
| Native USB D- / D+ | 19 / 20 |
| UART0 TX / RX | 43 / 44 |
| BOOT | 0 |

The validator rejects inaccessible module pins, including GPIO26–32 and
GPIO35–37 (Octal PSRAM), input-only outputs, reserved console pins used as
peripherals, and duplicate assignments. Explicit matching `bus` names permit
shared SPI CLK/MOSI and I2C SDA/SCL, while CS and control signals remain unique.
Strapping pins generate warnings, including the intentionally assigned BOOT pin.
Assignments for future devices only reserve pins; they do not initialize them.

Flash is 16 MB QIO at 80 MHz, with Octal PSRAM (`qio_opi`, `BOARD_HAS_PSRAM`).
The S3 partition table has two 6 MB OTA slots and fits exactly within 16 MB.
The bring-up MCU report checks detected flash and initialized PSRAM against
16 MB / 8 MB and reports an error if either differs. Build configuration alone
cannot confirm memory initialization on a physical module.

## Build, flash, monitor

From the repository root, with uv and the repository Python dependencies installed:

```sh
make board
uv run python -m slate.board --check
make firmware FIRMWARE_ENV=slate-s3-bringup
make ports
make flash FIRMWARE_ENV=slate-s3-bringup PORT=COM5
make monitor FIRMWARE_ENV=slate-s3-bringup PORT=COM5
```

Use your actual COM port on Windows. Native Windows commands without Make:

```powershell
uv run python -m slate.board
uv tool run --from pioarduino==6.2.0 pio run -d firmware -e slate-s3-bringup
uv tool run --from pioarduino==6.2.0 pio device list
uv tool run --from pioarduino==6.2.0 pio run -d firmware -e slate-s3-bringup -t upload --upload-port COM5
uv tool run --from pioarduino==6.2.0 pio device monitor -d firmware -e slate-s3-bringup -p COM5 -b 921600
```

Use the `pioarduino` Core package above, matching the platform's own Python
environment, rather than mixing it with the upstream `platformio` package.
On Windows, build outside OneDrive (for example `C:\dev\slate`) and set
`$env:PLATFORMIO_CORE_DIR = "C:\pio"` before running these commands to keep
dependency extraction paths short. After a successful build, upload normally;
unchanged objects are reused. To upload an already successful, unchanged build
without compiling, add `-t nobuild` to the upload command. Do not use `nobuild`
after changing source or configuration, or after a failed build.

WSL requires USB passthrough (for example usbipd-win); use the resulting Linux
port, typically `/dev/ttyACM0` for native USB or `/dev/ttyUSB0` for a UART adapter.
A Windows COM name is not a Linux device path. The S3 builds use native USB CDC
on GPIO19/20. If using a UART0 adapter instead, override
`ARDUINO_USB_CDC_ON_BOOT=0` in the environment's build flags and connect GPIO43/44.
If enumeration/upload fails, hold BOOT while resetting into download mode, then
release BOOT. Do not let serial tools assert reset continuously.

Other environments:

```sh
make firmware FIRMWARE_ENV=slate-s3
make firmware FIRMWARE_ENV=esp32dev
make flash FIRMWARE_ENV=esp32dev PORT=COM5
make monitor FIRMWARE_ENV=esp32dev PORT=COM5
```

## Hardware test commands

In the serial monitor, send one character (Enter is optional):

| Command | Test |
| --- | --- |
| `m` | Chip model/revision, cores, detected flash, initialized PSRAM, heap; heartbeat every 2 seconds |
| `i` | Ten seconds of 16 kHz I2S0 PDM capture, left channel, RMS/peak every 500 ms |
| `s` | I2S1 TX initialization, 440 Hz sine for 500 ms at amplitude 2000/32767, shutdown and release |
| `o` | RGB diagnostic and label for one second, then existing orb for ten seconds |
| `c` | OLED, PDM RX and I2S TX together; tone at start, retains both I2S controllers for ten seconds |
| `x` | Stop capture/display, mute amplifier, release I2S and SPI resources |

Tests stop automatically; repeat commands to check resource release and restart.
Speak/tap near the microphone and compare RMS windows; variation is reported,
but a changing level is not proof of correct acoustic response. No samples is
an error. OLED reports measured frames per second; visually check diagnostic
colors, text and orb continuity during the combined test. I2S allocation and
playback errors are printed. Record the logs and audible/visual observations.
The normal application still provides `l/r/m` channel selection, `a/x` binary
serial audio streaming, and `t` speaker tone.

## Driver changes and physical dependencies

PDM uses the existing ESP-IDF channel API, I2S0, six 320-frame DMA descriptors,
16-bit stereo capture and existing mono selection/filter/stream buffer. Explicit
8S decimation gives a nominal 1.024 MHz PDM clock at 16 kHz. Left selection is
preserved; verify the MP34DT01 L/R strap on the actual assembly. Capture stop
waits for the worker to delete I2S0 before allowing a restart.

Speaker uses I2S1, 16 kHz, 16-bit stereo Philips I2S, one-bit delay and no MCLK;
mono playback remains duplicated to both slots. DMA uses 128-frame buffers and
auto-clears underruns. The MAX98357A accepts this format; the MAX98357B's
left-justified format is different. See the
[manufacturer datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/max98357a-max98357b.pdf)
and [Espressif I2S guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.5/esp32s3/api-reference/peripherals/i2s.html).

GPIO16 drives SD_MODE low before I2S setup and on stop; after initialization it
becomes a high-impedance input to preserve the external channel-select bias.
Verify that GPIO16 is actually connected directly/compatibly to SD_MODE, that
low shuts down the amplifier, and that the external bias enables the intended
channel when released. With no external bias, release does not guarantee enable.
Do not infer shutdown behavior from the temporary PCM5102 headphone wiring.
Firmware cannot guarantee mute before boot without an appropriate hardware bias.

OLED SPI uses the generated S3 pins. Existing drawing and orb logic and 30 ms
frame schedule remain unchanged. The existing 16 MHz SPI setting is preserved;
the validator warns that it exceeds the part inventory's 4.5 MHz specification.
Validate signal integrity and reliable full-frame transfers on the actual board;
reducing SPI speed would affect achievable frame rate and requires a deliberate
follow-up decision.

PWR_HOLD is never driven in bring-up. If the physical latch requires assertion
to stay powered, independently power the board or verify the schematic and latch
polarity before changing firmware. No power-latch polarity is assumed here.
