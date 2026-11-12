import copy
import unittest

from slate.board import check, header, load


class BoardTests(unittest.TestCase):
    def setUp(self):
        board, self.parts = load()
        self.board = copy.deepcopy(board)
        self.oled = self.board["device"]["oled"]
        self.mic = self.board["device"]["mic"]

    def errors(self):
        problems = check(self.board, self.parts)
        return sorted(p.message for p in problems if p.level == "error")

    def test_current_breadboard_has_no_errors(self):
        self.assertEqual(self.errors(), [])

    def test_input_only_pin_cannot_drive_mic_clock(self):
        self.mic["pins"]["clk"] = 39
        self.assertIn("clk on GPIO39: input-only pin cannot drive clk", self.errors())

    def test_wrover_psram_takes_oled_control_pins(self):
        self.board["mcu"] = "esp32-wrover-e"
        self.assertEqual(
            self.errors(),
            [
                "dc on GPIO16: reserved for PSRAM",
                "reset on GPIO17: reserved for PSRAM",
            ],
        )

    def test_s3_rejects_pins_it_does_not_have(self):
        self.board["mcu"] = "esp32-s3-devkitc-1"
        self.assertEqual(
            self.errors(),
            [
                "chg_stat on GPIO34: ESP32-S3-DevKitC-1 has no GPIO34",
                "clk on GPIO26: ESP32-S3-DevKitC-1 has no GPIO26",
                "data on GPIO23: ESP32-S3-DevKitC-1 has no GPIO23",
                "din on GPIO27: ESP32-S3-DevKitC-1 has no GPIO27",
                "lrclk on GPIO25: ESP32-S3-DevKitC-1 has no GPIO25",
                "scl on GPIO22: ESP32-S3-DevKitC-1 has no GPIO22",
            ],
        )

    def test_shared_pin_is_rejected(self):
        self.mic["pins"]["clk"] = 18
        self.assertEqual(self.errors(), ["clk on GPIO18: already used by oled.clk"])

    def test_speaker_clock_cannot_share_mic_clock(self):
        self.board["device"]["speaker"]["pins"]["bclk"] = 26
        self.assertEqual(self.errors(), ["bclk on GPIO26: already used by mic.clk"])

    def test_pdm_clock_must_fit_the_mic(self):
        self.mic["sample_hz"] = 8000
        self.assertEqual(
            self.errors(),
            ["PDM clock 0.512 MHz is outside the mic's 1-3.25 MHz range"],
        )

    def test_long_wires_warn_about_ringing(self):
        self.oled["wire_cm"] = 20
        warnings = [p.message for p in check(self.board, self.parts)]
        self.assertTrue(any("may ring" in message for message in warnings))

    def test_power_conflict_is_rejected_when_enabled(self):
        self.board["device"]["power"]["enabled"] = True
        self.board["device"]["power"]["pins"]["sense"] = 14
        self.assertEqual(
            self.errors(), ["sense on GPIO14: already used by speaker.bclk"]
        )

    def test_power_hold_requires_output(self):
        self.board["device"]["power"]["pins"]["hold"] = 35
        self.assertIn("hold on GPIO35: input-only pin cannot drive hold", self.errors())

    def test_final_prototype_power_pins(self):
        self.assertEqual(
            self.board["device"]["power"]["pins"],
            {
                "hold": 13,
                "sense": 15,
                "fg_alrt": 35,
                "chg_stat": 34,
                "pgood": 39,
            },
        )
        self.assertEqual(self.board["device"]["power"]["i2c"], {"sda": 21, "scl": 22})
        self.board["device"]["power"]["enabled"] = True
        self.assertEqual(self.errors(), [])

    def test_i2c_cannot_use_flash_pins_on_prototype(self):
        self.board["device"]["power"]["i2c"]["sda"] = 8
        self.assertIn("sda on GPIO8: reserved for SPI flash", self.errors())

    def test_power_disabled_by_default(self):
        self.assertFalse(self.board["device"]["power"]["enabled"])
        self.assertEqual(header(self.board).count("#define POWER_ENABLED 0"), 1)
        self.board["device"]["power"]["enabled"] = True
        self.assertEqual(header(self.board).count("#define POWER_ENABLED 1"), 1)

    def test_power_conflict_is_rejected_when_disabled(self):
        self.board["device"]["power"]["pins"]["sense"] = 14
        self.assertEqual(
            self.errors(), ["sense on GPIO14: already used by speaker.bclk"]
        )

    def test_generated_header_matches_prototype_pins(self):
        generated = header(self.board)
        power = self.board["device"]["power"]
        for pin, gpio in (power["pins"] | power["i2c"]).items():
            self.assertIn(f"constexpr int POWER_{pin.upper()} = {gpio};", generated)
