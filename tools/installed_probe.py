# tools/installed_probe.py
"""The icon files the installed plugin cached for two URLs, and what is in them."""

import base64
import json
import os
import struct
import zlib
from pathlib import Path

root = Path(os.getenv("APPDATA", "")) / "FlowLauncher" / "Settings" / "Plugins"
folder = root / "Browser History V2" / "Cache" / "favicons"
index = json.loads((folder / "index.json").read_text(encoding="utf-8"))


def png_stats(data: bytes):
    width, height, depth, colour, _, _, _ = struct.unpack(">IIBBBBB", data[16:29])
    # Walk the chunks to find the pixel data.
    offset, idat = 8, b""
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        if kind == b"IDAT":
            idat += data[offset + 8:offset + 8 + length]
        offset += 12 + length
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour, 0)
    raw = zlib.decompress(idat)
    stride = width * channels + 1
    # Undo the per-scanline filters, enough to count opaque and dark pixels.
    previous = bytearray(width * channels)
    opaque = dark = total = 0
    for y in range(height):
        line = raw[y * stride:(y + 1) * stride]
        kind, payload = line[0], bytearray(line[1:])
        for i in range(len(payload)):
            left = payload[i - channels] if i >= channels else 0
            up = previous[i]
            upleft = previous[i - channels] if i >= channels else 0
            if kind == 1:
                payload[i] = (payload[i] + left) & 0xFF
            elif kind == 2:
                payload[i] = (payload[i] + up) & 0xFF
            elif kind == 3:
                payload[i] = (payload[i] + (left + up) // 2) & 0xFF
            elif kind == 4:
                p = left + up - upleft
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
                payload[i] = (payload[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else upleft)) & 0xFF
        previous = payload
        for x in range(width):
            base = x * channels
            if channels in (4, 2):
                alpha = payload[base + channels - 1]
                opaque += 1 if alpha > 32 else 0
            else:
                opaque += 1
            if channels >= 3:
                lum = (payload[base] * 299 + payload[base + 1] * 587 + payload[base + 2] * 114) // 1000
            else:
                lum = payload[base]
            dark += 1 if lum < 64 else 0
            total += 1
    return dict(size=f"{width}x{height}", colour_type=colour, bytes=len(data),
                visible=f"{100 * opaque // max(total, 1)}%",
                dark_pixels=f"{100 * dark // max(total, 1)}%")


for fragment in ("autocompressor.net/", "wide.video/app/1.7.11/"):
    for url, name in index.items():
        if not url.endswith(fragment) and fragment not in url:
            continue
        path = folder / name
        print(f"{url[:80]}")
        print(f"  file: {name} exists={path.exists()}")
        if not path.exists():
            continue
        data = path.read_bytes()
        print("  stats:", png_stats(data))
        print("  b64:", base64.b64encode(data).decode("ascii"))
