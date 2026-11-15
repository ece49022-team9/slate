import sys
import time

import esptool
import serial

CHUNK = 64
write = serial.Serial.write


def paced(port: serial.Serial, data: bytes) -> int:
    data = bytes(data)
    for start in range(0, len(data), CHUNK):
        piece = data[start : start + CHUNK]
        write(port, piece)
        time.sleep(len(piece) * 12 / port.baudrate)
    return len(data)


serial.Serial.write = paced
esptool.main(sys.argv[1:])
