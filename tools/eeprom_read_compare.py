#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from pathlib import Path

from hantek_linux.transport import UsbTransport

VID = 0x04B5
PID = 0x6CDE
IMAGE = Path("reference/recovery/DSO6106BD20160601.iic")
OUT = Path("evidence/eeprom-low-read.bin")
CHUNK = 0x400


def main() -> int:
    expected = IMAGE.read_bytes()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    transport = UsbTransport(VID, PID, 0x86)
    transport.open()
    try:
        actual = bytearray()
        for offset in range(0, len(expected), CHUNK):
            size = min(CHUNK, len(expected) - offset)
            block = transport.control_in(0xA2, offset, size)
            actual.extend(block)
            print(f"read 0x{offset:04x}-0x{offset+size-1:04x} ({size} bytes)")
    finally:
        transport.close()

    actual_b = bytes(actual)
    OUT.write_bytes(actual_b)

    print(f"expected_size={len(expected)} actual_size={len(actual_b)}")
    print(f"expected_sha256={hashlib.sha256(expected).hexdigest()}")
    print(f"actual_sha256={hashlib.sha256(actual_b).hexdigest()}")

    mismatches = [i for i, (a, b) in enumerate(zip(expected, actual_b)) if a != b]
    if len(expected) != len(actual_b):
        print("length_mismatch=1")
    print(f"byte_mismatches={len(mismatches)}")
    for i in mismatches[:32]:
        print(f"mismatch @ 0x{i:04x}: expected={expected[i]:02x} actual={actual_b[i]:02x}")

    if not mismatches and len(expected) == len(actual_b):
        print("EEPROM_PREFIX_MATCHES_IIC=1")
        return 0
    print("EEPROM_PREFIX_MATCHES_IIC=0")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
