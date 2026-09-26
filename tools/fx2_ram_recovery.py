#!/usr/bin/env python3
"""Temporary FX2 RAM loader for Hantek recovery.

Loads a Cypress .iic image into internal FX2 RAM using only the hardware
0xA0 vendor request. It does NOT write EEPROM and is fully lost on unplug.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import time

import usb.core
import usb.util

VID = 0x04B4
PID = 0x8613
CPUCS = 0xE600
REQTYPE_OUT_VENDOR_DEVICE = 0x40
RW_INTERNAL = 0xA0
MAX_BLOCK = 4096


def parse_iic(path: Path) -> tuple[bytes, list[tuple[int, bytes]], bytes]:
    raw = path.read_bytes()
    if len(raw) < 13:
        raise ValueError("IIC image is too short")
    header = raw[:8]
    if header[0] != 0xC2:
        raise ValueError(f"expected executable FX2 IIC header 0xC2, got 0x{header[0]:02x}")

    pos = 8
    blocks: list[tuple[int, bytes]] = []
    while pos < len(raw) - 5:
        if pos + 4 > len(raw):
            raise ValueError("truncated IIC block header")
        length = (raw[pos] << 8) | raw[pos + 1]
        address = (raw[pos + 2] << 8) | raw[pos + 3]
        pos += 4
        if length > MAX_BLOCK or pos + length > len(raw) - 5:
            raise ValueError(f"invalid IIC block length {length} at 0x{address:04x}")
        data = raw[pos:pos + length]
        pos += length
        blocks.append((address, data))

    tail = raw[pos:]
    if tail != bytes.fromhex("80 01 e6 00 00"):
        raise ValueError(f"unexpected IIC tail: {tail.hex(' ')}")

    for address, data in blocks:
        end = address + len(data)
        fx2_internal = address <= 0x1FFF and end <= 0x2000
        fx2_data = 0xE000 <= address <= 0xE1FF and end <= 0xE200
        if not (fx2_internal or fx2_data):
            raise ValueError(
                f"block 0x{address:04x}-0x{end - 1:04x} is outside first-stage FX2 internal RAM"
            )
    return header, blocks, tail


def write_internal(dev, address: int, data: bytes) -> None:
    written = dev.ctrl_transfer(
        REQTYPE_OUT_VENDOR_DEVICE,
        RW_INTERNAL,
        address & 0xFFFF,
        0,
        data,
        timeout=2000,
    )
    if written != len(data):
        raise RuntimeError(f"short FX2 write at 0x{address:04x}: {written}/{len(data)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--run", action="store_true", help="actually upload to FX2 RAM")
    args = ap.parse_args()

    header, blocks, tail = parse_iic(args.image)
    total = sum(len(data) for _, data in blocks)
    print(f"image={args.image}")
    print(f"header={header.hex(' ')}")
    print(f"blocks={len(blocks)} total={total} bytes tail={tail.hex(' ')}")
    for i, (address, data) in enumerate(blocks, 1):
        print(f"  {i}: 0x{address:04x}-0x{address + len(data) - 1:04x} ({len(data)} bytes)")

    if not args.run:
        print("dry-run only; EEPROM and RAM unchanged")
        return

    devs = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))
    if len(devs) != 1:
        raise RuntimeError(f"expected exactly one {VID:04x}:{PID:04x}, found {len(devs)}")
    dev = devs[0]
    print(f"device bus={dev.bus} address={dev.address}")

    # Cypress FX2 hardware loader: halt CPU, write on-chip RAM, resume CPU.
    # No EEPROM/I2C request is issued anywhere in this script.
    write_internal(dev, CPUCS, b"\x01")
    print("CPU halted")
    try:
        for i, (address, data) in enumerate(blocks, 1):
            write_internal(dev, address, data)
            print(f"wrote block {i}/{len(blocks)} @ 0x{address:04x}, {len(data)} bytes")
    except Exception:
        # Leave CPU halted on partial load. A physical unplug returns to ROM bootloader.
        print("upload failed with CPU halted; unplug/replug to recover")
        raise

    write_internal(dev, CPUCS, b"\x00")
    print("CPU resumed; waiting for USB re-enumeration")
    usb.util.dispose_resources(dev)
    time.sleep(3.0)

    hantek = list(usb.core.find(find_all=True, idVendor=0x04B5, idProduct=0x6CDE))
    print(f"04b5:6cde devices after RAM boot: {len(hantek)}")
    if len(hantek) != 1:
        raise RuntimeError("RAM firmware did not re-enumerate as 04b5:6cde")


if __name__ == "__main__":
    main()
