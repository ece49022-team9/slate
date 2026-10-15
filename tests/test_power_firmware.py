"""Run the actual power.cpp against deterministic GPIO/time/I2C fakes."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware/main"
FAKES = ROOT / "tests/power_fakes"


@unittest.skipUnless(shutil.which("g++"), "host C++ compiler unavailable")
class PowerFirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="slate-power-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.binaries = {}
        for mode, flags in {
            "disabled": [],
            "enabled": ["-DPOWER_ENABLED=1"],
        }.items():
            binary = Path(cls.directory.name) / mode
            subprocess.run(
                [
                    "g++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-Wno-unused-variable",
                    "-Wno-unused-function",
                    f"-I{FAKES}",
                    f"-I{FIRMWARE}",
                    *flags,
                    str(FAKES / "power_test.cpp"),
                    str(FIRMWARE / "power.cpp"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            cls.binaries[mode] = binary
        for mode, flags in {
            "app-disabled": [],
            "app-enabled": ["-DPOWER_ENABLED=1"],
            "app-speaker": ["-DPOWER_ENABLED=1", "-DSLATE_SPEAKER_TEST"],
        }.items():
            binary = Path(cls.directory.name) / mode
            subprocess.run(
                [
                    "g++",
                    "-std=c++17",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    f"-I{FAKES}",
                    f"-I{FIRMWARE}",
                    *flags,
                    str(FAKES / "application_test.cpp"),
                    str(FIRMWARE / "main.cpp"),
                    str(FIRMWARE / "power.cpp"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            cls.binaries[mode] = binary

    def run_scenario(self, mode, scenario):
        result = subprocess.run(
            [str(self.binaries[mode]), scenario],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_default_hardware_disabled(self):
        self.run_scenario("disabled", "disabled")

    def test_monitoring_and_button_scenarios(self):
        for scenario in (
            "good",
            "zero-reading",
            "soc-partial",
            "missing",
            "bus-failure",
            "partial-first",
            "partial",
            "pointer-fail",
            "write-fail",
            "negative-byte",
            "missing-later",
            "fractional-register",
            "stale",
            "charger",
            "short",
            "long",
            "bounce",
            "release-edge",
            "boot-held",
            "wrap",
            "button-wrap",
        ):
            with self.subTest(scenario=scenario):
                self.run_scenario("enabled", scenario)

    def test_latch_released_once_after_request(self):
        self.run_scenario("enabled", "release")

    def test_application_cleanup(self):
        for scenario in ("success", "timeout", "producer", "speaker-failure"):
            with self.subTest(scenario=scenario):
                self.run_scenario("app-enabled", scenario)

    def test_disabled_application(self):
        self.run_scenario("app-disabled", "disabled")

    def test_isolated_speaker_application_shutdown(self):
        self.run_scenario("app-speaker", "success")
