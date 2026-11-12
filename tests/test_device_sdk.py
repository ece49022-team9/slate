import unittest

from pydantic import ValidationError
from slate.device import DeviceSDK, OrbRequest, TextRequest


class FirmwarePeer:
    def __init__(self):
        self.color = "#ffffff"
        self.radius = 27.0
        self.text = ""
        self.revision = 0
        self.custom = False

    async def execute(self, command):
        if command.operation == "set_orb":
            self.color = command.arguments["color"]
            self.radius = command.arguments["radius"]
            self.custom = True
            self.revision += 1
        elif command.operation == "show_text":
            self.text = command.arguments["text"]
            self.revision += 1
        return {
            "request_id": command.request_id,
            "operation": command.operation,
            "revision": self.revision,
            "state": 0,
            "color": self.color,
            "radius": self.radius,
            "text": self.text,
            "custom": self.custom,
        }


class DeviceSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_operations_compose_and_status_reads_acknowledged_state(self):
        peer = FirmwarePeer()
        sdk = DeviceSDK("scope-a", "turn-a", peer.execute, lambda: True)
        orb = await sdk.set_orb(OrbRequest(color="#0080ff", radius=35))
        text = await sdk.show_text(TextRequest(text="Ready"))
        status = await sdk.get_status()
        self.assertEqual(orb.color, "#0080ff")
        self.assertEqual(orb.radius, 35)
        self.assertEqual(text.text, "Ready")
        self.assertEqual(status.revision, 2)
        self.assertEqual(status.color, orb.color)
        self.assertEqual(status.text, text.text)

    async def test_cancelled_turn_loses_all_device_access(self):
        peer = FirmwarePeer()
        active = True
        sdk = DeviceSDK("scope-a", "turn-a", peer.execute, lambda: active)
        await sdk.show_text(TextRequest(text="Before"))
        active = False
        with self.assertRaisesRegex(ValueError, "ended"):
            await sdk.show_text(TextRequest(text="Late"))
        with self.assertRaisesRegex(ValueError, "ended"):
            await sdk.get_status()
        self.assertEqual(peer.text, "Before")

    async def test_mismatched_acknowledgment_is_not_reported_as_success(self):
        peer = FirmwarePeer()

        async def wrong(command):
            result = await peer.execute(command)
            result["request_id"] = "0" * 32
            return result

        sdk = DeviceSDK("scope-a", "turn-a", wrong, lambda: True)
        with self.assertRaisesRegex(RuntimeError, "acknowledgment"):
            await sdk.get_status()

    def test_rejects_invalid_commands_before_the_device(self):
        for arguments in (
            {"color": "blue", "radius": 30},
            {"color": "#ffffff", "radius": float("nan")},
            {"color": "#ffffff", "radius": 100},
            {"color": "#ffffff", "radius": 30, "gpio": 0},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                OrbRequest(**arguments)
        for value in ("x" * 65, "two\nlines", "é"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TextRequest(text=value)
