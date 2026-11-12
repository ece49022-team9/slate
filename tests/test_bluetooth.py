import asyncio
import struct
import unittest

from cryptography.hazmat.primitives.asymmetric import ec
from slate.bluetooth import (
    ADDRESS,
    PHONE_ADDRESS,
    AttError,
    Bluetooth,
    PairingError,
    cmac,
    f4,
    f5,
    f6,
)

h = bytes.fromhex


class CryptoTests(unittest.TestCase):
    def test_core_appendix_d_cmac(self):
        key = h("2b7e151628aed2a6abf7158809cf4f3c")
        message = h(
            "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
            "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710"
        )
        for size, expected in (
            (0, "bb1d6929e95937287fa37d129b756746"),
            (16, "070a16b46b4d4144f79bdd9dd04a287c"),
            (40, "dfa66747de9ae63030ca32611497c827"),
            (64, "51f0bebf7e3b9d92fc49741779363cfe"),
        ):
            with self.subTest(size=size):
                self.assertEqual(cmac(key, message[:size]), h(expected))

    def test_core_appendix_d_f4(self):
        u = h("20b003d2f297be2c5e2c83a7e9f9a5b9eff49111acf4fddbcc0301480e359de6")
        v = h("55188b3d32f6bb9a900afcfbeed4e72a59cb9ac2f19d7cfb6b4fdd49f47fc5fd")
        x = h("d5cb8454d177733effffb2ec712baeab")
        self.assertEqual(f4(u, v, x, 0), h("f2c916f107a9bd1cf1eda1bea974872d"))

    def test_core_appendix_d_f5(self):
        w = h("ec0234a357c8ad05341010a60a397d9b99796b13b4f866f1868d34f373bfa698")
        n1 = h("d5cb8454d177733effffb2ec712baeab")
        n2 = h("a6e8e7cc25a75f6e216583f7ff3dc4cf")
        mac, ltk = f5(w, n1, n2, h("0056123737bfce"), h("00a713702dcfc1"))
        self.assertEqual(mac, h("2965f176a1084a02fd3f6a20ce636e20"))
        self.assertEqual(ltk, h("6986791169d7cd23980522b594750a38"))

    def test_core_appendix_d_f6(self):
        result = f6(
            h("2965f176a1084a02fd3f6a20ce636e20"),
            h("d5cb8454d177733effffb2ec712baeab"),
            h("a6e8e7cc25a75f6e216583f7ff3dc4cf"),
            h("12a3343bb453bb5408da42d20c2d0fc8"),
            h("010102"),
            h("0056123737bfce"),
            h("00a713702dcfc1"),
        )
        self.assertEqual(result, h("e3c473989cd0e8c5d26c0b09da958f61"))


class ControllerTests(unittest.TestCase):
    def command(self, controller, opcode, data=b""):
        packet = b"\x01" + struct.pack("<HB", opcode, len(data)) + data
        for byte in packet:
            controller.feed(bytes([byte]))
        result = bytes(controller.output)
        controller.output.clear()
        return result

    def test_hci_initialization_and_unknown_command(self):
        controller = Bluetooth()
        self.assertEqual(self.command(controller, 0x0C03), h("040e0401030c00"))
        self.assertEqual(
            self.command(controller, 0x1009), h("040e0a01091000") + ADDRESS
        )
        self.assertEqual(self.command(controller, 0x2002), h("040e0701022000fb0010"))
        with self.assertLogs("slate.bluetooth", level="WARNING") as logs:
            result = self.command(controller, 0xFFFF)
        self.assertEqual(result, h("040e0401ffff01"))
        self.assertIn("slate.bluetooth: unsupported HCI command ffff", logs.output[0])

    def test_remote_version_uses_its_own_completion_event(self):
        controller = Bluetooth()
        result = self.command(controller, 0x041D, b"\x01\x00")
        self.assertEqual(result, h("040f0400011d04040c0800010009ffff0000"))

    def test_advertising_tracks_contents_and_connection(self):
        controller = Bluetooth()
        self.command(controller, 0x2008, b"\x03abc" + bytes(28))
        self.command(controller, 0x2009, b"\x03def" + bytes(28))
        self.command(controller, 0x200A, b"\x01")
        self.assertEqual(controller.advertising_data, b"abc")
        self.assertEqual(controller.scan_response, b"def")
        controller.connect()
        self.assertTrue(controller.connected)
        self.assertFalse(controller.advertising)
        self.assertEqual(controller.output[:5], h("043e130100"))

    def test_acl_fragments_reassemble_and_return_packet_credit(self):
        controller = Bluetooth()
        payload = b"\x0b" + bytes(range(200))
        packet = struct.pack("<HH", len(payload), 4) + payload
        for offset in range(0, len(packet), 27):
            fragment = packet[offset : offset + 27]
            handle = 0x2001 if not offset else 0x1001
            h4 = b"\x02" + struct.pack("<HH", handle, len(fragment)) + fragment
            for byte in h4:
                controller.feed(bytes([byte]))
        self.assertEqual(controller.att.get_nowait(), payload)
        completed = h("0413050101000100")
        self.assertEqual(controller.output, completed * 8)

    def test_ltk_must_match_before_encryption(self):
        controller = Bluetooth()
        controller.ltk = bytes(range(16))
        self.command(controller, 0x201A, b"\x01\x00" + bytes(16))
        self.assertFalse(controller.encrypted.is_set())
        result = self.command(controller, 0x201A, b"\x01\x00" + controller.ltk)
        self.assertTrue(controller.encrypted.is_set())
        self.assertTrue(result.endswith(h("04080400010001")))


class AttTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_reports_att_security_error(self):
        controller = Bluetooth()
        controller.att.put_nowait(h("0112050005"))
        with self.assertRaises(AttError) as raised:
            await controller.write(5, b"net eth")
        self.assertEqual(raised.exception.code, 5)
        self.assertEqual(raised.exception.handle, 5)

    async def test_notifications_do_not_consume_request_responses(self):
        controller = Bluetooth()
        task = asyncio.create_task(controller.read(7))
        await asyncio.sleep(0)
        for pdu in (h("1b0700") + b"ready", h("0b") + b"saved net"):
            controller.receive_acl(0x2001, struct.pack("<HH", len(pdu), 4) + pdu)
        self.assertEqual(await task, b"saved net")
        self.assertEqual(controller.notifications.get_nowait(), (7, b"ready"))


class PairingTests(unittest.IsolatedAsyncioTestCase):
    async def pair(self, entered: int, displayed: int):
        controller = Bluetooth()
        outgoing = asyncio.Queue()
        controller.send = lambda cid, packet: outgoing.put_nowait((cid, packet))
        rounds = []

        async def take(opcode):
            cid, packet = await outgoing.get()
            self.assertEqual(cid, 6)
            self.assertEqual(packet[0], opcode)
            return packet[1:]

        async def peripheral():
            request = await take(1)
            response = h("00000c100000")
            controller.smp.put_nowait(b"\x02" + response)
            initiator = await take(0x0C)
            private = ec.derive_private_key(0xFEDCBA987654321, ec.SECP256R1())
            public = private.public_key().public_numbers()
            px = public.x.to_bytes(32, "big")
            py = public.y.to_bytes(32, "big")
            controller.smp.put_nowait(b"\x0c" + px[::-1] + py[::-1])
            x, y = initiator[:32][::-1], initiator[32:][::-1]
            for bit in range(20):
                confirm = await take(3)
                peer_nonce = bytes([bit + 1]) * 16
                z = 0x80 | ((displayed >> bit) & 1)
                controller.smp.put_nowait(b"\x03" + f4(px, x, peer_nonce, z)[::-1])
                nonce = (await take(4))[::-1]
                if confirm[::-1] != f4(x, px, nonce, z):
                    controller.smp.put_nowait(b"\x05\x04")
                    return
                rounds.append(bit)
                controller.smp.put_nowait(b"\x04" + peer_nonce[::-1])
            peer = ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()
            ).public_key()
            shared = private.exchange(ec.ECDH(), peer)
            a1 = b"\x00" + PHONE_ADDRESS[::-1]
            a2 = b"\x00" + ADDRESS[::-1]
            mac, ltk = f5(shared, nonce, peer_nonce, a1, a2)
            check = await take(0x0D)
            r = displayed.to_bytes(16, "big")
            self.assertEqual(
                check[::-1], f6(mac, nonce, peer_nonce, r, request[:3][::-1], a1, a2)
            )
            check = f6(mac, peer_nonce, nonce, r, response[:3][::-1], a2, a1)
            controller.smp.put_nowait(b"\x0d" + check[::-1])
            while controller.ltk is None:
                await asyncio.sleep(0)
            controller.command(0x201A, b"\x01\x00" + ltk[::-1])

        task = asyncio.create_task(peripheral())

        async def passkey() -> int:
            return entered

        try:
            async with asyncio.timeout(2):
                await controller.pair(passkey)
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return controller, rounds

    async def test_secure_connections_completes_twenty_rounds_and_checks_ltk(self):
        controller, rounds = await self.pair(123456, 123456)
        self.assertEqual(rounds, list(range(20)))
        self.assertTrue(controller.encrypted.is_set())

    async def test_wrong_passkey_is_rejected(self):
        with self.assertRaises(PairingError):
            await self.pair(123457, 123456)
