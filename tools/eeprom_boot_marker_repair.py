#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import time
from pathlib import Path

from hantek_linux.transport import HantekError, UsbTransport

VID = 0x04B5
PID = 0x6CDE
EP_IN = 0x86
IMAGE = Path("reference/recovery/DSO6106BD20160601.iic")
EVIDENCE_DIR = Path("evidence")
EXPECTED_BAD = 0x20
EXPECTED_GOOD = 0xC2
CHUNK = 0x400


def read_eeprom(transport: UsbTransport, length: int) -> bytes:
    data = bytearray()
    for offset in range(0, length, CHUNK):
        size = min(CHUNK, length - offset)
        data.extend(transport.control_in(0xA2, offset, size))
    return bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed repair of the single invalid FX2 EEPROM boot marker."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write exactly one byte (0xC2) at EEPROM address 0, then read it back",
    )
    args = parser.parse_args()

    expected = IMAGE.read_bytes()
    if not expected or expected[0] != EXPECTED_GOOD:
        raise SystemExit("refusing: recovery image does not start with expected 0xC2")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    transport = UsbTransport(VID, PID, EP_IN)
    transport.open()
    try:
        before = read_eeprom(transport, len(expected))
        (EVIDENCE_DIR / "eeprom-before-repair.bin").write_bytes(before)

        mismatches = [i for i, (a, b) in enumerate(zip(expected, before)) if a != b]
        print(f"before_sha256={hashlib.sha256(before).hexdigest()}")
        print(f"reference_sha256={hashlib.sha256(expected).hexdigest()}")
        print(f"mismatches={mismatches}")
        print(f"byte0_before=0x{before[0]:02x} byte0_expected=0x{expected[0]:02x}")

        if len(before) != len(expected):
            raise SystemExit("refusing: EEPROM read length mismatch")
        if mismatches != [0]:
            raise SystemExit("refusing: EEPROM differs from reference anywhere except byte 0")
        if before[0] != EXPECTED_BAD:
            raise SystemExit(
                f"refusing: byte 0 is 0x{before[0]:02x}, expected known-bad 0x{EXPECTED_BAD:02x}"
            )

        if not args.apply:
            print("DRY_RUN_OK=1")
            print("planned_write=request A2 OUT, value=0x0000, length=1, data=c2")
            print("NO_EEPROM_WRITE_PERFORMED=1")
            return 0

        print("APPLYING_SINGLE_BYTE_REPAIR=1")
        transport.control_out(0xA2, 0x0000, bytes([EXPECTED_GOOD]))
        time.sleep(0.020)

        verify = transport.control_in(0xA2, 0x0000, 1)
        print(f"byte0_readback=0x{verify[0]:02x}")
        if verify != bytes([EXPECTED_GOOD]):
            raise HantekError(
                "EEPROM byte-0 readback mismatch",
                operation="eeprom_repair_verify",
                context={"expected": EXPECTED_GOOD, "actual": verify.hex()},
            )

        after = read_eeprom(transport, len(expected))
        (EVIDENCE_DIR / "eeprom-after-repair.bin").write_bytes(after)
        after_mismatches = [i for i, (a, b) in enumerate(zip(expected, after)) if a != b]
        print(f"after_sha256={hashlib.sha256(after).hexdigest()}")
        print(f"after_mismatches={after_mismatches}")
        if after != expected:
            raise HantekError(
                "EEPROM full readback does not match reference",
                operation="eeprom_repair_verify",
                context={"mismatch_count": len(after_mismatches)},
            )

        print("EEPROM_REPAIR_VERIFIED=1")
        print("Power-cycle the scope next; do not RAM-boot it before checking lsusb.")
        return 0
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
