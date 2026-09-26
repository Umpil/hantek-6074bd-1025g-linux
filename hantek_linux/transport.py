"""Strict, synchronous PyUSB transport. Timeouts are in milliseconds."""
from __future__ import annotations

from threading import RLock
from typing import NamedTuple

import usb.core
import usb.util


class UsbDeviceId(NamedTuple):
    """USB identity of one instrument: interface 0, Bulk OUT 0x02 and this Bulk IN endpoint."""

    vid: int
    pid: int
    ep_in: int
    packet_size: int


GENERATOR_USB = UsbDeviceId(0x0483, 0x5726, 0x81, 64)    # Hantek1025G, full speed
SCOPE_USB = UsbDeviceId(0x04B5, 0x6CDE, 0x86, 512)       # Hantek6074BD / 6000B, high speed
_KNOWN_PACKET_SIZES = {(d.vid, d.pid): d.packet_size for d in (GENERATOR_USB, SCOPE_USB)}


class HantekError(RuntimeError):
    """USB operation failure with context; mutations are never retried."""

    def __init__(self, message: str, *, operation: str, context: dict | None = None):
        self.operation = operation
        self.context = context or {}
        super().__init__(f"{message} | operation={operation} | context={self.context}")


class UsbTransport:
    """One claimed USB interface, explicitly opened and closed.

    packet_size is the expected wMaxPacketSize of both bulk endpoints. It defaults to the
    value of a known Hantek id; for other devices None skips that check.
    """

    def __init__(
        self,
        vid: int,
        pid: int,
        ep_in: int,
        *,
        timeout_ms: int = 1500,
        packet_size: int | None = None,
    ):
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        self.vid, self.pid, self.ep_in = vid, pid, ep_in
        self.packet_size = packet_size if packet_size is not None else _KNOWN_PACKET_SIZES.get((vid, pid))
        self.ep_out = 0x02
        self.timeout_ms = timeout_ms
        self.lock = RLock()
        # Incremented by every successful open(); lets drivers detect a reopened
        # (possibly power-cycled) device and discard per-session hardware state.
        self.session = 0
        self._dev = None
        self._claimed = False

    @classmethod
    def for_device(cls, device: UsbDeviceId, *, timeout_ms: int = 1500) -> "UsbTransport":
        return cls(device.vid, device.pid, device.ep_in, timeout_ms=timeout_ms, packet_size=device.packet_size)

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    def open(self, *, bus: int | None = None, address: int | None = None) -> None:
        with self.lock:
            if self.is_open:
                raise HantekError("already open", operation="open")
            devices = list(usb.core.find(find_all=True, idVendor=self.vid, idProduct=self.pid))
            devices = [
                d
                for d in devices
                if (bus is None or d.bus == bus) and (address is None or d.address == address)
            ]
            if len(devices) != 1:
                raise HantekError(
                    "select exactly one device",
                    operation="open",
                    context={"count": len(devices), "vid": self.vid, "pid": self.pid},
                )
            dev = devices[0]
            try:
                cfg = dev.get_active_configuration()
                interface = cfg[(0, 0)]
                endpoints = {ep.bEndpointAddress: ep for ep in interface}
                for endpoint in (self.ep_out, self.ep_in):
                    ep = endpoints.get(endpoint)
                    if ep is None or usb.util.endpoint_type(ep.bmAttributes) != 2:
                        raise HantekError("unexpected USB endpoints", operation="open")
                    if self.packet_size is not None and ep.wMaxPacketSize != self.packet_size:
                        raise HantekError("unexpected packet size", operation="open")
                if dev.is_kernel_driver_active(0):
                    raise HantekError("interface bound to a kernel driver", operation="open")
                usb.util.claim_interface(dev, 0)
                self._claimed = True
                self._dev = dev
                self.session += 1
            except Exception:
                usb.util.dispose_resources(dev)
                raise

    def _require(self):
        if self._dev is None:
            raise HantekError("device is closed", operation="usb")
        return self._dev

    def describe(self) -> dict:
        with self.lock:
            dev = self._require()
            return {
                "vid": f"{self.vid:04x}",
                "pid": f"{self.pid:04x}",
                "bus": dev.bus,
                "address": dev.address,
                "product": usb.util.get_string(dev, dev.iProduct) if dev.iProduct else None,
                "interface": 0,
                "ep_out": self.ep_out,
                "ep_in": self.ep_in,
            }

    def write(self, data: bytes) -> None:
        with self.lock:
            dev = self._require()
            try:
                count = dev.write(self.ep_out, data, timeout=self.timeout_ms)
            except usb.core.USBError as exc:
                raise HantekError(
                    str(exc), operation="bulk_out", context={"length": len(data)}
                ) from exc
            if count != len(data):
                raise HantekError(
                    "short USB write; device state is unknown",
                    operation="bulk_out",
                    context={"expected": len(data), "got": count},
                )

    def read(self, length: int) -> bytes:
        if length <= 0:
            raise ValueError("length must be positive")
        with self.lock:
            dev = self._require()
            try:
                data = bytes(dev.read(self.ep_in, length, timeout=self.timeout_ms))
            except usb.core.USBError as exc:
                raise HantekError(
                    str(exc), operation="bulk_in", context={"length": length}
                ) from exc
            if len(data) != length:
                raise HantekError(
                    "short USB read",
                    operation="bulk_in",
                    context={"expected": length, "got": len(data)},
                )
            return data

    def control_in(
        self,
        request: int,
        value: int,
        length: int,
        *,
        index: int = 0,
        request_type: int = 0xC0,
        allow_short: bool = False,
    ) -> bytes:
        if length <= 0:
            raise ValueError("length must be positive")
        with self.lock:
            dev = self._require()
            try:
                data = bytes(
                    dev.ctrl_transfer(
                        request_type,
                        request,
                        value,
                        index,
                        length,
                        timeout=self.timeout_ms,
                    )
                )
            except usb.core.USBError as exc:
                raise HantekError(
                    str(exc),
                    operation="control_in",
                    context={"request": request, "value": value, "length": length},
                ) from exc
            if not data:
                raise HantekError(
                    "empty control response",
                    operation="control_in",
                    context={"expected_max": length},
                )
            if len(data) != length and not allow_short:
                raise HantekError(
                    "short control response",
                    operation="control_in",
                    context={"expected": length, "got": len(data)},
                )
            return data

    def control_out(
        self,
        request: int,
        value: int,
        data: bytes = b"",
        *,
        index: int = 0,
        request_type: int = 0x40,
    ) -> None:
        with self.lock:
            dev = self._require()
            try:
                count = int(
                    dev.ctrl_transfer(
                        request_type,
                        request,
                        value,
                        index,
                        data,
                        timeout=self.timeout_ms,
                    )
                )
            except usb.core.USBError as exc:
                raise HantekError(
                    str(exc),
                    operation="control_out",
                    context={"request": request, "value": value, "length": len(data)},
                ) from exc
            if count != len(data):
                raise HantekError(
                    "short control write; device state is unknown",
                    operation="control_out",
                    context={"expected": len(data), "got": count},
                )

    def close(self) -> None:
        with self.lock:
            dev, self._dev = self._dev, None
            if dev is not None:
                try:
                    if self._claimed:
                        usb.util.release_interface(dev, 0)
                finally:
                    self._claimed = False
                    usb.util.dispose_resources(dev)

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
