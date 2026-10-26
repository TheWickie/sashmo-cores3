"""Elgato Stream Deck 2019 (0FD9:006D), using its published HID reports."""

from __future__ import annotations

import struct

VID = 0x0FD9
PID = 0x006D
REPORT_SIZE = 1024
IMAGE_CHUNK = REPORT_SIZE - 8


def image_reports(index: int, jpeg: bytes) -> list[bytes]:
    if not 0 <= index < 15 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
        raise ValueError("Expected key 0-14 and JPEG data")
    reports = []
    for part, offset in enumerate(range(0, len(jpeg), IMAGE_CHUNK)):
        chunk = jpeg[offset:offset + IMAGE_CHUNK]
        last = offset + len(chunk) >= len(jpeg)
        header = struct.pack("<BBBBHH", 2, 7, index, int(last), len(chunk), part)
        reports.append((header + chunk).ljust(REPORT_SIZE, b"\0"))
    return reports


def key_states(report: bytes | list[int]) -> list[bool] | None:
    data = bytes(report)
    if len(data) < 19 or data[:2] != b"\x01\x00" or int.from_bytes(data[2:4], "little") != 15:
        return None
    return [bool(value) for value in data[4:19]]


class Hardware:
    def __init__(self) -> None:
        self.device = None

    def open(self) -> None:
        import hid  # Optional, isolated from the backend and firmware.
        matches = [row for row in hid.enumerate(VID, PID) if row.get("usage_page") == 12]
        if not matches:
            raise OSError("15-key Stream Deck 2019 is not connected")
        device = hid.device()
        device.open_path(matches[0]["path"])
        info = bytes(device.get_feature_report(8, 32))
        if len(info) < 11 or info[0] != 8 or info[1:3] != b"\x03\x05" \
                or info[3:7] != b"\x48\x00\x48\x00":
            device.close()
            raise OSError("Unexpected Stream Deck geometry")
        self.device = device

    def set_brightness(self, percent: int) -> None:
        if self.device is None:
            raise OSError("Stream Deck disconnected")
        if not 0 <= percent <= 100:
            raise ValueError("Brightness must be 0-100")
        self.device.send_feature_report(bytes([3, 8, percent]).ljust(32, b"\0"))

    def set_key(self, index: int, jpeg: bytes) -> None:
        if self.device is None:
            raise OSError("Stream Deck disconnected")
        for report in image_reports(index, jpeg):
            if self.device.write(report) != len(report):
                raise OSError("Stream Deck image write failed")

    def read_keys(self, timeout_ms: int = 50) -> list[bool] | None:
        if self.device is None:
            raise OSError("Stream Deck disconnected")
        return key_states(self.device.read(512, timeout_ms))

    def close(self) -> None:
        if self.device is not None:
            self.device.close()
            self.device = None
