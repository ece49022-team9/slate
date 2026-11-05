import asyncio
import hashlib
import logging
import struct
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

log = logging.getLogger(__name__)
ADDRESS = bytes.fromhex("1032547698ba")
PHONE_ADDRESS = bytes.fromhex("665544332211")
SERVICE = UUID("5a7e0001-6a1b-4c3e-9f5d-8b2c1e0d4a10")
COMMAND = UUID("5a7e0002-6a1b-4c3e-9f5d-8b2c1e0d4a10")
STATUS = UUID("5a7e0003-6a1b-4c3e-9f5d-8b2c1e0d4a10")
SUPPORTED_COMMANDS = bytes(
    {
        0: 0x20,
        2: 0x80,
        5: 0xC0,
        10: 0xE0,
        14: 0xB8,
        15: 0x02,
        22: 0x04,
        24: 0x40,
        25: 0xF7,
        26: 0xC3,
        27: 0xE4,
        28: 0x0E,
        33: 0x80,
        34: 0x01,
        35: 0x08,
    }.get(octet, 0)
    for octet in range(64)
)


def cmac(key: bytes, data: bytes) -> bytes:
    digest = CMAC(algorithms.AES(key))
    digest.update(data)
    return digest.finalize()


def f4(u: bytes, v: bytes, x: bytes, z: int) -> bytes:
    return cmac(x, u + v + bytes([z]))


def f5(w: bytes, n1: bytes, n2: bytes, a1: bytes, a2: bytes) -> tuple[bytes, bytes]:
    salt = bytes.fromhex("6c888391aaf5a53860370bdb5a6083be")
    key = cmac(salt, w)
    data = b"btle" + n1 + n2 + a1 + a2 + b"\x01\x00"
    return cmac(key, b"\x00" + data), cmac(key, b"\x01" + data)


def f6(w: bytes, n1: bytes, n2: bytes, r: bytes, io: bytes, a1: bytes, a2: bytes):
    return cmac(w, n1 + n2 + r + io + a1 + a2)


class AttError(Exception):
    def __init__(self, response: bytes):
        self.opcode, self.handle, self.code = struct.unpack("<BHB", response[1:])
        super().__init__(f"slate.bluetooth: ATT error {self.code:02x} at {self.handle}")


class PairingError(Exception):
    pass


class Bluetooth:
    def __init__(self, timeout: Callable = asyncio.timeout):
        self.timeout = timeout
        self.input = bytearray()
        self.output = bytearray()
        self.acl = bytearray()
        self.commands: Counter[int] = Counter()
        self.advertising = False
        self.advertising_data = b""
        self.scan_response = b""
        self.address = ADDRESS
        self.address_type = 0
        self.random_address = bytes(6)
        self.own_address_type = 0
        self.connected = False
        self.encrypted = asyncio.Event()
        self.att: asyncio.Queue[bytes] = asyncio.Queue()
        self.smp: asyncio.Queue[bytes] = asyncio.Queue()
        self.notifications: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self.ltk: bytes | None = None
        self.random_count = 0
        self.mtu = 23
        self.host_flow = False
        self.host_credits = 0
        self.held: deque[bytes] = deque()

    def random(self, size: int) -> bytes:
        self.random_count += 1
        return hashlib.sha256(struct.pack("<I", self.random_count)).digest()[:size]

    def event(self, code: int, data: bytes) -> None:
        self.output += bytes([4, code, len(data)]) + data

    def complete(self, opcode: int, data: bytes = b"\x00") -> None:
        self.event(0x0E, struct.pack("<BH", 1, opcode) + data)

    def status(self, opcode: int, status: int = 0) -> None:
        self.event(0x0F, struct.pack("<BBH", status, 1, opcode))

    def le_event(self, code: int, data: bytes) -> None:
        self.event(0x3E, bytes([code]) + data)

    def feed(self, data: bytes) -> None:
        self.input += data
        while self.input:
            kind = self.input[0]
            if kind == 1:
                if len(self.input) < 4:
                    return
                size = self.input[3] + 4
            elif kind == 2:
                if len(self.input) < 5:
                    return
                size = struct.unpack_from("<H", self.input, 3)[0] + 5
            else:
                raise ValueError(f"slate.bluetooth: unknown H4 packet type {kind:02x}")
            if len(self.input) < size:
                return
            packet = bytes(self.input[:size])
            del self.input[:size]
            if kind == 1:
                self.command(struct.unpack_from("<H", packet, 1)[0], packet[4:])
            else:
                self.receive_acl(struct.unpack_from("<H", packet, 1)[0], packet[5:])

    def command(self, opcode: int, data: bytes) -> None:
        self.commands[opcode] += 1
        log.debug("slate.bluetooth: HCI command %04x", opcode)
        fixed = {
            0x1001: bytes.fromhex("0009000009ffff0000"),
            0x1002: b"\x00" + SUPPORTED_COMMANDS,
            0x1003: b"\x00" + bytes.fromhex("0000000060000000"),
            0x1005: struct.pack("<BHBHH", 0, 251, 0, 16, 0),
            0x1009: b"\x00" + ADDRESS,
            0x2002: struct.pack("<BHB", 0, 251, 16),
            0x2003: b"\x00\x01" + bytes(7),
            0x2007: b"\x00\x00",
            0x200F: b"\x00\x00",
            0x201C: b"\x00\xff\x01" + bytes(6),
            0x2023: b"\x00" + struct.pack("<HH", 251, 2120),
            0x202F: b"\x00" + struct.pack("<HHHH", 251, 2120, 251, 2120),
        }
        if opcode in fixed:
            self.complete(opcode, fixed[opcode])
        elif opcode in (0x0C03, 0x0C01, 0x0C63, 0x0C6D, 0x2001, 0x2010, 0x2024):
            if opcode == 0x0C03:
                self.advertising = False
                self.connected = False
                self.encrypted.clear()
                self.host_flow = False
                self.held.clear()
            self.complete(opcode)
        elif opcode == 0x0C31:
            self.host_flow = bool(data[0] & 1)
            self.complete(opcode)
        elif opcode == 0x0C33:
            self.host_credits = struct.unpack_from("<HBHH", data)[2]
            self.complete(opcode)
        elif opcode == 0x0C35:
            for index in range(data[0]):
                self.host_credits += struct.unpack_from("<HH", data, 1 + 4 * index)[1]
            self.release_held()
        elif opcode == 0x2005:
            self.random_address = data
            self.complete(opcode)
        elif opcode == 0x2006:
            self.own_address_type = data[5]
            self.address_type = self.own_address_type
            self.address = self.random_address if self.address_type else ADDRESS
            self.complete(opcode)
        elif opcode in (0x2008, 0x2009):
            value = data[1 : 1 + data[0]]
            if opcode == 0x2008:
                self.advertising_data = value
            else:
                self.scan_response = value
            self.complete(opcode)
        elif opcode == 0x200A:
            self.advertising = bool(data[0])
            self.complete(opcode)
        elif opcode == 0x2018:
            self.complete(opcode, b"\x00" + self.random(8))
        elif opcode == 0x2017:
            encryptor = Cipher(algorithms.AES(data[:16][::-1]), modes.ECB()).encryptor()
            result = encryptor.update(data[16:][::-1]) + encryptor.finalize()
            self.complete(opcode, b"\x00" + result[::-1])
        elif opcode == 0x0406:
            self.status(opcode)
            self.disconnect(data[2])
        elif opcode == 0x041D:
            self.status(opcode)
            self.event(0x0C, b"\x00\x01\x00\x09\xff\xff\x00\x00")
        elif opcode == 0x2016:
            self.status(opcode)
            self.le_event(4, b"\x00\x01\x00\x01" + bytes(7))
        elif opcode in (0x201A, 0x201B):
            self.complete(opcode, b"\x00" + data[:2])
            success = opcode == 0x201A and data[2:] == self.ltk
            self.event(8, struct.pack("<BHB", 0 if success else 6, 1, int(success)))
            if success:
                self.encrypted.set()
        elif opcode == 0x2022:
            self.complete(opcode, b"\x00" + data[:2])
        elif opcode == 0x2013:
            self.status(opcode)
            self.le_event(3, b"\x00" + data[:2] + data[2:4] + data[6:10])
        else:
            log.warning("slate.bluetooth: unsupported HCI command %04x", opcode)
            self.complete(opcode, b"\x01")

    def connect(self) -> None:
        if not self.advertising or self.connected:
            raise RuntimeError("slate.bluetooth: peripheral is not advertising")
        self.connected = True
        self.advertising = False
        self.encrypted.clear()
        self.ltk = None
        self.mtu = 23
        self.acl.clear()
        for queue in (self.att, self.smp, self.notifications):
            while not queue.empty():
                queue.get_nowait()
        self.le_event(
            1, struct.pack("<BHBB6sHHHB", 0, 1, 1, 0, PHONE_ADDRESS, 24, 0, 200, 0)
        )

    def disconnect(self, reason: int = 0x13) -> None:
        self.connected = False
        self.encrypted.clear()
        self.event(5, struct.pack("<BHB", 0, 1, reason))

    def send(self, cid: int, data: bytes) -> None:
        packet = struct.pack("<HH", len(data), cid) + data
        for offset in range(0, len(packet), 64):
            fragment = packet[offset : offset + 64]
            handle = 1 | (0x2000 if offset == 0 else 0x1000)
            self.held.append(
                b"\x02" + struct.pack("<HH", handle, len(fragment)) + fragment
            )
        self.release_held()

    def release_held(self) -> None:
        while self.held and (not self.host_flow or self.host_credits > 0):
            self.output += self.held.popleft()
            if self.host_flow:
                self.host_credits -= 1

    def receive_acl(self, handle: int, data: bytes) -> None:
        self.event(0x13, struct.pack("<BHH", 1, handle & 0x0FFF, 1))
        if handle & 0x3000 != 0x1000:
            self.acl.clear()
        self.acl += data
        if len(self.acl) < 4:
            return
        size, cid = struct.unpack_from("<HH", self.acl)
        if len(self.acl) < size + 4:
            return
        if len(self.acl) != size + 4:
            raise ValueError("slate.bluetooth: invalid L2CAP length")
        packet = bytes(self.acl[4:])
        self.acl.clear()
        if cid == 4:
            if packet[0] in (0x1B, 0x1D):
                self.notifications.put_nowait(
                    (struct.unpack_from("<H", packet, 1)[0], packet[3:])
                )
                if packet[0] == 0x1D:
                    self.send(4, b"\x1e")
            elif packet[0] == 2:
                self.send(4, b"\x03\x00\x01")
            else:
                self.att.put_nowait(packet)
        elif cid == 6:
            self.smp.put_nowait(packet)
        elif cid == 5 and packet[0] == 0x12:
            self.send(5, bytes([0x13, packet[1]]) + b"\x02\x00\x00\x00")
        else:
            log.warning("slate.bluetooth: unhandled L2CAP channel %04x", cid)

    async def request(self, data: bytes) -> bytes:
        self.send(4, data)
        async with self.timeout(10):
            response = await self.att.get()
        if response[0] == 1:
            raise AttError(response)
        if response[0] != data[0] + 1:
            raise ValueError(
                f"slate.bluetooth: unexpected ATT response {response.hex()}"
            )
        return response

    async def exchange_mtu(self) -> int:
        response = await self.request(b"\x02\x00\x01")
        self.mtu = min(256, struct.unpack_from("<H", response, 1)[0])
        return self.mtu

    async def discover(self) -> dict[UUID, int]:
        start = 1
        service = None
        while start <= 0xFFFF:
            try:
                response = await self.request(
                    struct.pack("<BHHH", 0x10, start, 0xFFFF, 0x2800)
                )
            except AttError as error:
                if error.code == 0x0A:
                    break
                raise
            width = response[1]
            for offset in range(2, len(response), width):
                row = response[offset : offset + width]
                first, last = struct.unpack_from("<HH", row)
                if row[4:] == SERVICE.bytes[::-1]:
                    service = first, last
                start = last + 1
        if service is None:
            raise ValueError("slate.bluetooth: Slate service is missing")
        start, end = service
        values = {}
        while start <= end:
            try:
                response = await self.request(
                    struct.pack("<BHHH", 8, start, end, 0x2803)
                )
            except AttError as error:
                if error.code == 0x0A:
                    break
                raise
            width = response[1]
            for offset in range(2, len(response), width):
                row = response[offset : offset + width]
                declaration, _, value = struct.unpack_from("<HBH", row)
                if len(row[5:]) == 16:
                    values[UUID(bytes=row[5:][::-1])] = value
                start = declaration + 1
        return values

    async def write(self, handle: int, value: bytes) -> None:
        if len(value) > self.mtu - 3:
            raise ValueError(
                f"slate.bluetooth: {len(value)} bytes do not fit ATT MTU {self.mtu}"
            )
        await self.request(struct.pack("<BH", 0x12, handle) + value)

    async def read(self, handle: int) -> bytes:
        return (await self.request(struct.pack("<BH", 0x0A, handle)))[1:]

    async def subscribe(self, handle: int) -> int:
        response = await self.request(struct.pack("<BHH", 4, handle + 1, 0xFFFF))
        if response[1] != 1:
            raise ValueError("slate.bluetooth: expected 16-bit descriptors")
        for offset in range(2, len(response), 4):
            descriptor, uuid = struct.unpack_from("<HH", response, offset)
            if uuid == 0x2902:
                await self.write(descriptor, b"\x01\x00")
                return descriptor
        raise ValueError("slate.bluetooth: status CCCD is missing")

    async def smp_receive(self, opcode: int) -> bytes:
        async with self.timeout(10):
            while True:
                packet = await self.smp.get()
                if packet[0] == 0x0B:
                    continue
                if packet[0] == 5:
                    raise PairingError(
                        f"slate.bluetooth: pairing failed {packet[1]:02x}"
                    )
                if packet[0] != opcode:
                    raise PairingError(
                        f"slate.bluetooth: unexpected SMP {packet.hex()}"
                    )
                return packet[1:]

    async def pair(self, passkey: Callable[[], Awaitable[int]]) -> None:
        request = bytes([2, 0, 0x0C, 16, 0, 0])
        self.send(6, b"\x01" + request)
        response = await self.smp_receive(2)
        if response[0] != 0 or response[2] & 0x0C != 0x0C or response[3] != 16:
            raise PairingError(
                "slate.bluetooth: expected authenticated Secure Connections"
            )
        private = ec.derive_private_key(0x123456789ABCDEF, ec.SECP256R1())
        public = private.public_key().public_numbers()
        x = public.x.to_bytes(32, "big")
        y = public.y.to_bytes(32, "big")
        self.send(6, b"\x0c" + x[::-1] + y[::-1])
        peer = await self.smp_receive(0x0C)
        px, py = peer[:32][::-1], peer[32:][::-1]
        peer_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(px, "big"), int.from_bytes(py, "big"), ec.SECP256R1()
        ).public_key()
        shared = private.exchange(ec.ECDH(), peer_key)
        key = await passkey()
        try:
            for bit in range(20):
                nonce = self.random(16)
                z = 0x80 | ((key >> bit) & 1)
                self.send(6, b"\x03" + f4(x, px, nonce, z)[::-1])
                confirm = await self.smp_receive(3)
                self.send(6, b"\x04" + nonce[::-1])
                peer_nonce = (await self.smp_receive(4))[::-1]
                if confirm[::-1] != f4(px, x, peer_nonce, z):
                    raise PairingError("slate.bluetooth: passkey confirmation failed")
            a1 = b"\x00" + PHONE_ADDRESS[::-1]
            a2 = bytes([self.address_type]) + self.address[::-1]
            mac, ltk = f5(shared, nonce, peer_nonce, a1, a2)
            r = key.to_bytes(16, "big")
            check = f6(mac, nonce, peer_nonce, r, request[:3][::-1], a1, a2)
            self.send(6, b"\x0d" + check[::-1])
            peer_check = await self.smp_receive(0x0D)
            expected = f6(mac, peer_nonce, nonce, r, response[:3][::-1], a2, a1)
            if peer_check[::-1] != expected:
                raise PairingError("slate.bluetooth: DHKey check failed")
            self.ltk = ltk[::-1]
            self.le_event(5, struct.pack("<HQH", 1, 0, 0))
            async with self.timeout(10):
                await self.encrypted.wait()
        except PairingError:
            self.send(6, b"\x05\x04")
            raise
