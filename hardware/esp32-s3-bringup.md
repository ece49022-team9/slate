# ESP32-S3-WROOM-1-N16R8 bring-up

Use `slate-s3-bringup` for initial hardware work. Wi-Fi and BLE are off at boot
and can be started independently through serial for local radio tests. It runs
without cloud credentials, haptics, microSD, I2C, or power-management initialization.
`slate-s3` retains the normal voice application. `esp32dev` and `speaker-test`
retain the prototype wiring. Arduino and the existing pinned pioarduino platform
are preserved (Arduino 3.3.12 / ESP-IDF 5.5.5). Bring-up uses precompiled
ESP-IDF libraries (including Wi-Fi and NimBLE) and only the OLED/GFX/BusIO external
dependencies. `nimble.py` selects the matching packaged NimBLE headers. It deliberately
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

From Windows PowerShell, use the checkout containing the latest changes and your
actual Windows COM port. For the current bench (CH343 adapter on COM5):

```powershell
Set-Location C:\Users\suhan\OneDrive\Documents\GitHub\slate
$env:PLATFORMIO_CORE_DIR = "C:\pio"
uv run python -m slate.board
uv tool run --from pioarduino==6.2.0 pio run -d firmware -e slate-s3-bringup
uv tool run --from pioarduino==6.2.0 pio device list
uv tool run --from pioarduino==6.2.0 pio run -d firmware -e slate-s3-bringup -t upload --upload-port COM5
uv tool run --from pioarduino==6.2.0 pio device monitor -d firmware -e slate-s3-bringup -p COM5 -b 921600
```

Use the `pioarduino` Core package above, matching the platform's own Python
environment, rather than mixing it with the upstream `platformio` package.
On Windows, `C:\pio` keeps dependency extraction paths short. If building outside
OneDrive (for example `C:\dev\slate`), first synchronize the updated source,
headers, scripts and configuration; a separate checkout can contain older firmware.
After a successful build, upload normally;
unchanged objects are reused. To upload an already successful, unchanged build
without compiling, add `-t nobuild` to the upload command. Do not use `nobuild`
after changing source or configuration, or after a failed build.

WSL requires USB passthrough (for example usbipd-win); use the resulting Linux
port, typically `/dev/ttyACM0` for native USB or `/dev/ttyUSB0` for a UART adapter.
A Windows COM name is not a Linux device path. The current `slate-s3-bringup`
and `slate-s3-voice-test` builds select UART0 (`ARDUINO_USB_CDC_ON_BOOT=0`):
connect a 3.3 V UART adapter RX to GPIO43 (TX), TX to GPIO44 (RX), and common
ground. `slate-s3` selects native USB CDC on GPIO19/20. To use native USB for
bring-up, change its existing `ARDUINO_USB_CDC_ON_BOOT` flag to `1` and rebuild
before uploading.
If enumeration/upload fails, hold BOOT while resetting into download mode, then
release BOOT. Do not let serial tools assert reset continuously.

Other environments:

```sh
uv tool run --from pioarduino==6.2.0 pio run -d firmware -e slate-s3
uv tool run --from pioarduino==6.2.0 pio run -d firmware -e esp32dev
```

## Hardware test commands

In the serial monitor, send one character (Enter is optional):

| Command | Test |
| --- | --- |
| `m` | Chip model/revision, cores, detected flash, initialized PSRAM, heap (also stops the current peripheral test) |
| `i` | Ten seconds of 16 kHz I2S0 PDM capture, left channel, RMS/peak every 500 ms |
| `s` | I2S1 TX initialization, 440 Hz sine for 500 ms at amplitude 2000/32767, shutdown and release |
| `o` | RGB diagnostic and label for one second, then existing orb for ten seconds |
| `c` | OLED, PDM RX and I2S TX together; tone at start, retains both I2S controllers for ten seconds |
| `d` | Continuous combined demo, with a 200 ms headphone tone every three seconds; `x` stops |
| `x` | Stop capture/display, mute amplifier, release I2S and SPI resources |

The MCU report is printed once at boot; the periodic MCU heartbeat defaults off.
Send `!mcu on` followed by Enter to enable reports every two seconds, `!mcu off`
to silence them, or `!mcu status` for one report without stopping peripherals
or either radio. Other diagnostic/error messages remain visible. The toggle
is RAM-only and resets to off on reboot.

Tests stop automatically; repeat commands to check resource release and restart.
Speak/tap near the microphone and compare RMS windows; variation is reported,
but a changing level is not proof of correct acoustic response. No samples is
an error. OLED reports measured frames per second; visually check diagnostic
colors, text and orb continuity during the combined test. I2S allocation and
playback errors are printed. Record the logs and audible/visual observations.
The normal application still provides `l/r/m` channel selection, `a/x` binary
serial audio streaming, and `t` speaker tone.

## Wi-Fi tests (no cloud required)

Wi-Fi commands start with `!` and require Enter (CR, LF or CRLF). Single-character
peripheral commands retain their original behavior outside a `!` command line.
Wi-Fi commands do not stop a peripheral test; `x` stops peripherals only. Use
`!wifi disconnect` to turn the radio off independently.

| Command | Result |
| --- | --- |
| `!wifi scan` | Start an asynchronous scan, including hidden networks; report up to 24 strongest results with SSID, RSSI in dBm, channel, and numeric ESP-IDF authentication mode |
| `!wifi connect "SSID" "PASSWORD"` | Attempt one station connection and DHCP acquisition, with a 30-second limit |
| `!wifi connect "SSID" ""` | Connect to an open network |
| `!wifi status` | Report radio/scan/connection state and last disconnect reason; when associated, show SSID, channel, RSSI and IP |
| `!wifi disconnect` | Cancel scan/connection, stop the radio, and clear RAM credentials; wait for status to leave `stopping` before another start |

Use a 2.4 GHz AP. Password-protected connections require WPA2 or stronger;
legacy WEP/WPA-only and enterprise authentication are not configured by this
interface. Scans finish asynchronously with a 15-second limit. Connections do
not automatically retry: a failed attempt or later link loss prints a numeric
ESP-IDF reason with a description. Authentication/handshake failures can reflect
either credentials or radio conditions; they do not prove a wrong password.
If no IP is assigned, the overall connect/DHCP attempt times out after 30 seconds.

Quote SSIDs/passwords containing spaces. Inside double quotes, `\"` means a
literal quote and `\\` means a literal backslash; single-quoted fields also work.
Limits are 32 SSID bytes, 64 password bytes and 255 bytes for the line after `!`.
Wi-Fi input is not echoed by firmware; disable local echo and avoid recording
typed passwords in your terminal. Backspace/Delete edits the current line.
Malformed or oversized commands are rejected. After 30 seconds without input,
an unfinished line is wiped and discarded until Enter; send Enter before
resuming single-character tests.

Driver initialization uses `nvs_enable=false` and `WIFI_STORAGE_RAM`. The
firmware neither reads nor writes Slate's saved provisioning namespace; it does
not start the cloud task or load a saved network. Input/configuration scratch
buffers are wiped after dispatch. Credentials remain only in driver RAM while
needed and are cleared by `!wifi disconnect`; reset also loses them. Radio stop
retains driver/netif/event allocations for subsequent tests, so free heap need
not return to its pre-Wi-Fi level. `status` and `disconnect` on a fresh boot do
not initialize the driver.

### Suggested board procedure

1. Build, upload and monitor `slate-s3-bringup` using the commands above, on the
   UART console at 921600 baud. Record the firmware revision and serial output.
2. Send `!wifi status`; expect `radio=off`, no IP. Check the existing `m`, `i`,
   `s`, `o`, `c`, `d` and `x` commands as your baseline.
3. Send `!wifi scan`; expect `scan complete`, your AP, RSSI and channel. Send
   peripheral keys while scanning to confirm the interface remains responsive.
   An empty result is valid but does not establish radio reception.
4. Send `!wifi connect "Your SSID" "Your password"`. Within 30 seconds expect
   `connected; assigned IP=...`, followed by SSID/channel/RSSI. Send
   `!wifi status` to inspect current values. If a scan is running, wait or cancel
   it before connecting.
5. Send `d` while connected; inspect microphone RMS, OLED continuity and headphone
   tones. Enable the MCU/heap heartbeat with `!mcu on` if wanted.
   Run `!wifi scan` and `!wifi status` during
   the demo. Send `x`; the peripheral demo stops and Wi-Fi stays connected.
6. Turn the AP off and wait for a disconnect reason. Restore it and explicitly
   reconnect; there is no automatic recovery in this test interface. If still
   associated or busy, disconnect first. Separately try a nonexistent SSID and
   a wrong password: expect a reason or bounded timeout, with serial/peripheral
   commands still usable. Correct the credentials and retry.
7. Send `!wifi disconnect`, then `!wifi status`; expect `radio=off`. Repeat
   scan/connect/disconnect several times and watch heap for progressive loss.
   Reset and verify Wi-Fi remains off until commanded and needs credentials
   again. Saved normal-application provisioning should be unchanged.

These checks establish scanning, association and DHCP on the physical board.
They do not test internet/cloud traffic or sustained throughput. Use the BLE
procedure below to check simultaneous radio operation.

## BLE tests with nRF Connect on iPhone

The same `slate-s3-bringup` environment now includes the existing Slate NimBLE
service, with authenticated LE Secure Connections, a six-digit passkey, and no
retained bond. Both radios start off. BLE commands require Enter:

| Serial command | Result |
| --- | --- |
| `!ble start` | Initialize BLE and advertise as `Slate-XXXX`; repeat while active is harmless |
| `!ble status` | Report active, connected, encrypted, authenticated, notification subscription, ping count and whether restart requires reset |
| `!ble stop` | Send `done`, stop the stack and release controller memory; reset the board before starting BLE again |

Starting BLE does not stop Wi-Fi or the peripheral tests. Stopping BLE does not
reset the board or intentionally stop Wi-Fi. The existing controller memory
release is irreversible until reset; another `!ble start` after release prints
instructions rather than unexpectedly rebooting and losing RAM Wi-Fi settings.
To repeat tests without resetting, disconnect/reconnect in nRF Connect while
leaving BLE running: the board resumes advertising when the phone disconnects.
Reset turns both radios off and loses the test Wi-Fi credentials.

### First discovery, pairing and ping

1. Upload the new image using the PowerShell commands above, then open the
   monitor at 921600 baud. Confirm the boot line lists `!ble start`. Type directly
   into the scrolling terminal; there is no prompt and input echo may be off.
2. Send `!ble start`. Expect `slate.ble: advertising Slate-XXXX`. Initially test
   with Wi-Fi off so any failure is easier to isolate.
3. Enable Bluetooth on the iPhone, grant nRF Connect Bluetooth access if asked,
   scan, and connect to `Slate-XXXX`. These are BLE operations inside nRF Connect,
   rather than manually pairing through the iPhone Settings device list.
4. When iOS requests pairing, enter the current six-digit value from
   `slate.ble: passkey NNNNNN` in serial. The code is also submitted to the OLED
   text renderer, but the OLED need not be running: serial is sufficient.
   If no prompt appears yet, read the protected status characteristic below.
   Expect `slate.ble: paired` after successful authentication.
5. Expand the custom service (nRF Connect may label it "Unknown Service"):

   | Interface | UUID | Operation |
   | --- | --- | --- |
   | Setup service | `5a7e0001-6a1b-4c3e-9f5d-8b2c1e0d4a10` | Expand to see characteristics |
   | Command | `5a7e0002-6a1b-4c3e-9f5d-8b2c1e0d4a10` | Write with response |
   | Status | `5a7e0003-6a1b-4c3e-9f5d-8b2c1e0d4a10` | Read and enable notifications |

6. Read Status; initially expect UTF-8 `ready`. Enable notifications on Status.
   Write UTF-8/text `ping` (lowercase, no Enter/newline) to Command using a write
   with response. If the app is set to hex input, use `70 69 6e 67` instead.
7. Expect Status to notify `pong` (`70 6f 6e 67` in hex), and serial to report
   the received ping count. If not subscribed, reading Status should still
   return `pong`. Send `!ble status`: expect encrypted/authenticated `yes`, and
   `notify=yes` when subscribed.
8. Repeat writes, disconnect/reconnect, and try an incorrect passkey on a new
   pairing attempt. Wrong pairing must fail; a new successful pairing should
   restore the exchange. Protected reads/writes require authentication; iOS
   may automatically pair before allowing them rather than exposing a raw ATT
   rejection. If iOS has a stale saved pairing, forget it if listed and retry.

nRF Connect supports service discovery, characteristic reads/writes and
notification subscriptions; see [Nordic's app documentation](https://github.com/nordicsemi/IOS-nRF-Connect).

### Wi-Fi and BLE together

With BLE still connected and Status notifications enabled, use the existing
serial Wi-Fi commands to connect to the tested hotspot. Verify `net up <IP>`
arrives over BLE and `!wifi status` still shows the IP. Write `ping` repeatedly
and run `d` for the continuous microphone/OLED/speaker demo. Watch for audio or
display interruptions, pairing/link errors and progressive heap loss. `x` stops
the peripheral demo while both radios remain active.

The phone can also write these text commands to Command:

| BLE write | Meaning |
| --- | --- |
| `ping` | Update/notify Status with `pong`; increment serial ping count |
| `status` | Update/notify Status with current Wi-Fi state/IP |
| `net wifi "Suharsha iPhone" "YOUR_PASSWORD"` | Attempt a Wi-Fi connection using RAM credentials only; same quoting, busy checks and timeout as serial |

There is no automatic BLE shutdown after the test connection succeeds, so both
radios can be exercised together. A GATT write acknowledgement confirms queue
acceptance, not Wi-Fi success; use Status/serial for the eventual result. Invalid
Wi-Fi syntax/busy states are explained in serial. All other BLE commands,
including `cloud ...`, receive an error in bring-up and do not save settings.
Normal application builds still use the existing cloud provisioning handler,
save settings, and finish BLE when both settings have been saved; this test
callback does not replace their flow. No cloud task is linked into bring-up.

Record advertising discovery, wrong/correct passkey outcomes, `pong` replies,
Wi-Fi IP and peripheral observations separately. Compilation and host tests
cannot establish physical RF performance or coexistence on the board.

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
