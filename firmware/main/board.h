#pragma once
#ifdef SLATE_BOARD_S3
#pragma once
// Generated from hardware/board.toml by make board
constexpr char BOARD_MCU[] = "esp32-s3-wroom-1-n16r8";
constexpr int OLED_CLK = 12;
constexpr int OLED_DATA = 11;
constexpr int OLED_CS = 10;
constexpr int OLED_DC = 14;
constexpr int OLED_RESET = 21;
constexpr unsigned OLED_SPI_HZ = 8000000;
constexpr unsigned OLED_FRAME_HZ = 25;
constexpr int MIC_CLK = 4;
constexpr int MIC_DATA = 5;
constexpr unsigned MIC_SAMPLE_HZ = 16000;
constexpr int SPEAKER_BCLK = 6;
constexpr int SPEAKER_LRCLK = 17;
constexpr int SPEAKER_DIN = 15;
constexpr int SPEAKER_SD = 16;
constexpr int POWER_HOLD = 1;
constexpr int POWER_SENSE = 2;
constexpr int POWER_FG_ALRT = 18;
constexpr int POWER_CHG_STAT = 40;
constexpr int POWER_PGOOD = 41;
constexpr int POWER_SDA = 8;
constexpr int POWER_SCL = 9;
constexpr int FUTURE_HAPTICS_EN = 7;
constexpr int FUTURE_MISO_SD = 13;
constexpr int FUTURE_SDCS = 47;
constexpr int FUTURE_SDCD = 42;
constexpr int CONSOLE_USB_DM = 19;
constexpr int CONSOLE_USB_DP = 20;
constexpr int CONSOLE_UART_TX = 43;
constexpr int CONSOLE_UART_RX = 44;
constexpr int CONSOLE_BOOT = 0;
#ifndef POWER_ENABLED
#define POWER_ENABLED 0
#endif
#else
#pragma once
// Generated from hardware/board.toml by make board
constexpr char BOARD_MCU[] = "esp32-wroom-32";
constexpr int OLED_CLK = 18;
constexpr int OLED_DATA = 23;
constexpr int OLED_CS = 5;
constexpr int OLED_DC = 16;
constexpr int OLED_RESET = 17;
constexpr unsigned OLED_SPI_HZ = 8000000;
constexpr unsigned OLED_FRAME_HZ = 25;
constexpr int MIC_CLK = 26;
constexpr int MIC_DATA = 36;
constexpr unsigned MIC_SAMPLE_HZ = 16000;
constexpr int SPEAKER_BCLK = 14;
constexpr int SPEAKER_LRCLK = 25;
constexpr int SPEAKER_DIN = 27;
constexpr int POWER_HOLD = 13;
constexpr int POWER_SENSE = 15;
constexpr int POWER_FG_ALRT = 35;
constexpr int POWER_CHG_STAT = 34;
constexpr int POWER_PGOOD = 39;
constexpr int POWER_SDA = 21;
constexpr int POWER_SCL = 22;
#ifndef POWER_ENABLED
#define POWER_ENABLED 0
#endif
#endif
