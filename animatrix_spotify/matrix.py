"""AniMe Matrix (GA402) geometry and raw HID access.

Protocol and LED layout follow asusctl's rog-anime crate. The matrix is a
USB HID device (0b05:193b); every message is a 640-byte report starting 0x5e.
"""
import glob
import os

ROWS = 61
PACKET = 640
PANE = 627  # LED bytes carried per packet
PREFIXES = (
    bytes([0x5E, 0xC0, 0x02, 0x01, 0x00, 0x73, 0x02]),
    bytes([0x5E, 0xC0, 0x02, 0x74, 0x02, 0x73, 0x02]),
    bytes([0x5E, 0xC0, 0x02, 0xE7, 0x04, 0x73, 0x02]),
)


def row_width(y):
    return 34 if y <= 11 else 39 - y // 2


def first_x(y):
    return 0 if y <= 11 else (y + 1) // 2 - 5


def led_grid():
    """(x, y) of every LED in wire order. x is in LED columns (odd rows sit
    half a column left), y is the row."""
    return [
        (first_x(y) + i - 0.5 * (y % 2), y)
        for y in range(ROWS)
        for i in range(row_width(y))
    ]


NUM_LEDS = len(led_grid())  # 1449


def _pkt(*head):
    return bytes(head) + bytes(PACKET - len(head))


def find_device():
    for uevent in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
        with open(uevent) as f:
            if "0B05:0000193B" in f.read().upper():
                return "/dev/" + uevent.split("/")[4]
    return None


class Matrix:
    def __init__(self, brightness=2):
        self.path = find_device()
        if not self.path:
            raise OSError("AniMe Matrix USB device (0b05:193b) not found")
        self.fd = os.open(self.path, os.O_WRONLY)  # PermissionError without the udev rule
        self.brightness = brightness
        self.init()

    def _write(self, pkt):
        os.write(self.fd, pkt)

    def init(self):
        self._write(_pkt(0x5E, *b"ASUS Tech.Inc."))
        self._write(_pkt(0x5E, 0xC2))
        self._write(_pkt(0x5E, 0xC4, 0x01, 0x80))  # built-in animations off
        self._write(_pkt(0x5E, 0xC3, 0x01, 0x00))  # display on
        self._write(_pkt(0x5E, 0xC0, 0x04, self.brightness))

    def show(self, leds):
        """leds: NUM_LEDS brightness bytes in wire order."""
        data = bytes(min(v, 254) for v in leds)
        for i, prefix in enumerate(PREFIXES):
            chunk = data[i * PANE:(i + 1) * PANE]
            self._write(prefix + chunk + bytes(PACKET - 7 - len(chunk)))
        self._write(_pkt(0x5E, 0xC0, 0x03))  # flush

    def clear(self):
        self.show(bytes(NUM_LEDS))

    def close(self):
        try:
            self.clear()
        finally:
            os.close(self.fd)
