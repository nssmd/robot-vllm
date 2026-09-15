"""Camera payloads with explicit dimensions/encoding; missing data is never zero-filled."""

import base64
import struct
import zlib


def ros_image_png(message):
    width, height, step = message.width, message.height, message.step
    if (not 1 <= width <= 2048 or not 1 <= height <= 2048 or message.encoding not in ("rgb8", "bgr8")
            or step < width * 3 or len(message.data) != step * height):
        raise ValueError("invalid_or_unsupported_image")
    raw = bytes(message.data)
    rows = []
    for y in range(height):
        row = raw[y * step:y * step + width * 3]
        if message.encoding == "bgr8":
            converted = bytearray(len(row))
            converted[0::3], converted[1::3], converted[2::3] = row[2::3], row[1::3], row[0::3]
            row = bytes(converted)
        rows.append(b"\x00" + row)
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), level=1)) + chunk(b"IEND", b""))
    return {"mime_type": "image/png", "encoding": "base64", "width": width, "height": height,
            "data": base64.b64encode(png).decode("ascii"), "frame_id": message.header.frame_id,
            "stamp_sec": message.header.stamp.sec, "stamp_nanosec": message.header.stamp.nanosec}
