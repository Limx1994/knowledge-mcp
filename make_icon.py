"""Build a small original book glyph without an imaging dependency."""
from pathlib import Path
import struct


def build_icon():
    size = 32
    pixels = bytearray()
    for y in reversed(range(size)):
        for x in range(size):
            inside = 2 <= x < 30 and 2 <= y < 30
            corner = min(x - 2, 29 - x) < 3 and min(y - 2, 29 - y) < 3
            color = (0, 0, 0, 0)
            if inside and not corner:
                color = (91, 112, 58, 255)
            book = (7 <= x <= 24 and y in (9, 23)) or (x in (7, 15, 24) and 9 <= y <= 23)
            page = (10 <= x <= 12 or 18 <= x <= 21) and y in (13, 16, 19)
            if book or page:
                color = (244, 248, 244, 255)
            pixels.extend(color)
    dib = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0,
                      len(pixels), 0, 0, 0, 0) + pixels + bytes(128)
    icon = struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(dib), 22) + dib
    output = Path(__file__).resolve().parent / 'web' / 'book.ico'
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(icon)


if __name__ == '__main__':
    build_icon()
