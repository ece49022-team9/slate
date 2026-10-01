import copy
import unittest

from slate.board import check, load


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
        self.assertEqual(
            self.errors(), ["clk on GPIO39: input-only pin cannot drive clk"]
        )

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
                "clk on GPIO26: ESP32-S3-DevKitC-1 has no GPIO26",
                "data on GPIO23: ESP32-S3-DevKitC-1 has no GPIO23",
            ],
        )

    def test_shared_pin_is_rejected(self):
        self.mic["pins"]["clk"] = 18
        self.assertEqual(self.errors(), ["clk on GPIO18: already used by oled.clk"])

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
