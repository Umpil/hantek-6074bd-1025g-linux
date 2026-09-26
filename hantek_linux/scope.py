"""Native Hantek6000B/6074BD acquisition for the verified RoboHand mode.

The supported acquisition path stays evidence-backed: 4096 samples, 4-channel
ADC mode, and direct-capture time-div indices 8..24. Index 12 remains the
default verified 20 us/div / 12.5 MS/s mode; index 8 provides 250 MS/s for
high-frequency loopback validation. Faster indices require vendor software
interpolation and slower indices require roll mode, so both remain rejected.
Channel ranges/coupling, trigger source/level/slope/sweep, and logical
signal/sync roles are configurable.

The low-level command path reproduces the vendor B3/B2 synchronization around
Bulk OUT commands and the extra B2 preparation used before state/metadata Bulk
IN reads. No flash/calibration writes are implemented.
"""
from __future__ import annotations

import logging
import math
import os
import re
import struct
import sys
import time
from dataclasses import dataclass

from .transport import SCOPE_USB, HantekError, UsbTransport

_LOG = logging.getLogger(__name__)

_VOLTS_PER_DIV = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
_DEFAULT_LEVERS = (192, 160, 96, 64)
# Fixed frame geometry of the verified backend: all four channels, 4096 samples
# per channel, trigger at 50 % of the frame.
_CHANNELS = (1, 2, 3, 4)
_ALL_CHANNELS_MASK = 0x0F
_SAMPLE_COUNT = 4096
_TIME_DIV_INDEX = 12
_DIRECT_TIME_DIV_MIN = 8
_DIRECT_TIME_DIV_MAX = 24
_HORIZONTAL_TRIGGER_PERCENT = 50
_TIMEBASE_SCALE = {
    8: 4,
    9: 8,
    10: 20,
    11: 40,
    12: 80,
    13: 200,
    14: 400,
    15: 800,
    16: 2000,
    17: 4000,
    18: 8000,
    19: 20000,
    20: 40000,
    21: 80000,
    22: 200000,
    23: 400000,
    24: 800000,
}
_TIME_DIV_COUNTER = {
    8: 1,
    9: 1,
    10: 2,
    11: 5,
    12: 10,
    13: 25,
    14: 50,
    15: 100,
    16: 250,
    17: 500,
    18: 1000,
    19: 2500,
    20: 5000,
    21: 10000,
    22: 25000,
    23: 50000,
    24: 100000,
}
_SCOPE_SYNC_SEED = bytes((0x0F, 0x03, 0x03, 0x03, 0, 0, 0, 0, 0, 0))
_SCOPE_PACKET_SIZE = SCOPE_USB.packet_size
_STATE_DATA_READY_BIT = 0x02
_SCOPE_INIT_WRITES = (
    bytes.fromhex("08 00 00 77 47 12 04 00"),
    bytes.fromhex("08 00 00 03 00 33 04 00"),
    bytes.fromhex("08 00 00 65 00 30 02 00"),
    bytes.fromhex("08 00 00 28 F1 0F 02 00"),
    bytes.fromhex("08 00 00 12 38 01 02 00"),
)
_SCOPE_INIT_DELAYS_S = (0.002, 0.002, 0.002, 0.015, 0.0)
_SCOPE_ADC_4CH_MODE_GAIN = bytes.fromhex("08 00 00 31 00 55 04 00")
_RELAY_FIRST_SETTLE_S = 0.004
_RELAY_SECOND_SETTLE_S = 0.050
# Every configure() re-latches the relays and rewrites the PWM zero positions.
# Measured on the 6074BD: a capture started right after configure() shows a
# baseline ramp of up to ~40 ADC codes; it is settled after 75 ms.
_ANALOG_SETTLE_S = 0.100
_COUPLING_CODES = {"dc": 0, "ac": 1, "gnd": 2}
_SLOPE_CODES = {"rising": 0, "falling": 1}
_SWEEP_CODES = {"normal": 0, "auto": 1}


class _StderrHandler(logging.StreamHandler):
    """Writes to the current sys.stderr at emit time, like the former print() trace."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def _enable_trace_from_env() -> None:
    """HANTEK_TRACE_SYNC=1 prints the init and B3/B2 sync trace to stderr.

    The trace is ordinary DEBUG logging on the "hantek_linux.scope" logger, so an
    application can also enable it through its own logging configuration.
    """
    if os.environ.get("HANTEK_TRACE_SYNC", "") in ("", "0", "false", "False"):
        return
    logger = logging.getLogger("hantek_linux")
    if not any(isinstance(handler, _StderrHandler) for handler in logger.handlers):
        logger.addHandler(_StderrHandler())
    logger.setLevel(logging.DEBUG)
    logger.propagate = False   # exactly one copy of each line, as before


@dataclass(frozen=True, slots=True)
class Calibration:
    """Raw device-specific calibration snapshots; no flash writes."""

    zero: bytes
    amplitude: bytes
    dds: bytes

    @property
    def zero_words(self) -> tuple[int, ...]:
        return struct.unpack("<577H", self.zero)

    @property
    def amplitude_words(self) -> tuple[int, ...]:
        return struct.unpack("<289H", self.amplitude)


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """One physical scope channel.

    lever is the normalized 0..255 vertical zero position. None uses the
    vendor-demo defaults (CH1..CH4 = 192, 160, 96, 64). All four channels are
    always captured; the verified backend has no partial-channel modes.
    """

    volts_per_div: float = 1.0
    coupling: str = "dc"
    bandwidth_limit: bool = False
    lever: int | None = None

    def __post_init__(self) -> None:
        value = float(self.volts_per_div)
        object.__setattr__(self, "volts_per_div", value)
        if value not in _VOLTS_PER_DIV:
            raise ValueError(f"unsupported volts_per_div={value}; choose one of {_VOLTS_PER_DIV}")
        coupling = self.coupling.lower()
        object.__setattr__(self, "coupling", coupling)
        if coupling not in _COUPLING_CODES:
            raise ValueError("coupling must be dc, ac or gnd")
        if self.lever is not None and not 0 <= self.lever <= 255:
            raise ValueError("lever must be 0..255")


@dataclass(frozen=True, slots=True)
class ScopeConfig:
    """Evidence-backed acquisition configuration.

    signal_channel and sync_channel are logical roles only; the actual
    hardware trigger is selected independently by trigger_source. Every
    capture has all four channels, 4096 samples each, trigger at 50 %.
    """

    channels: tuple[ChannelConfig, ...] = (
        ChannelConfig(),
        ChannelConfig(volts_per_div=2.0),
        ChannelConfig(),
        ChannelConfig(),
    )
    trigger_source: int = 2
    trigger_level_v: float = 1.0
    trigger_slope: str = "rising"
    trigger_sweep: str = "normal"
    signal_channel: int = 1
    sync_channel: int = 2
    time_div_index: int = _TIME_DIV_INDEX

    def __post_init__(self) -> None:
        if len(self.channels) != 4:
            raise ValueError("exactly four physical channel configurations are required")
        for name in ("trigger_source", "signal_channel", "sync_channel"):
            value = int(getattr(self, name))
            if not 1 <= value <= 4:
                raise ValueError(f"{name} must be 1..4")
            object.__setattr__(self, name, value)
        slope = self.trigger_slope.lower()
        sweep = self.trigger_sweep.lower()
        object.__setattr__(self, "trigger_slope", slope)
        object.__setattr__(self, "trigger_sweep", sweep)
        if slope not in _SLOPE_CODES:
            raise ValueError("trigger_slope must be rising or falling")
        if sweep not in _SWEEP_CODES:
            raise ValueError("trigger_sweep must be normal or auto")
        if not math.isfinite(float(self.trigger_level_v)):
            raise ValueError("trigger_level_v must be finite")
        object.__setattr__(self, "trigger_level_v", float(self.trigger_level_v))
        if not _DIRECT_TIME_DIV_MIN <= self.time_div_index <= _DIRECT_TIME_DIV_MAX:
            raise ValueError(
                "current native backend supports direct-capture time-div indices "
                f"{_DIRECT_TIME_DIV_MIN}..{_DIRECT_TIME_DIV_MAX}"
            )

    def lever_for(self, channel: int) -> int:
        cfg = self.channels[channel - 1]
        return _DEFAULT_LEVERS[channel - 1] if cfg.lever is None else cfg.lever


@dataclass(frozen=True, slots=True)
class Waveform:
    channel: int
    sample_rate_hz: float
    codes: tuple[int, ...]
    volts: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Capture:
    config: ScopeConfig
    state: int
    metadata: bytes
    ring_address: int
    raw: bytes
    waveforms: tuple[Waveform, ...]

    def waveform(self, channel: int) -> Waveform:
        for waveform in self.waveforms:
            if waveform.channel == channel:
                return waveform
        raise KeyError(f"channel {channel} was not transferred")

    @property
    def signal(self) -> Waveform:
        return self.waveform(self.config.signal_channel)

    @property
    def sync(self) -> Waveform:
        return self.waveform(self.config.sync_channel)


def normalize_samples(raw: bytes, *, channels: int = 4) -> tuple[tuple[int, ...], ...]:
    """Decode the vendor ADC normalization and ordinary interleaving."""
    if channels not in (1, 2, 4) or len(raw) % channels:
        raise ValueError("need complete 1-, 2- or 4-channel frames")
    normalized = [max(0, min(255, int(256 * (b - 28) / 200 + 0.5))) for b in raw]
    return tuple(tuple(normalized[channel::channels]) for channel in range(channels))


def channel_position_packet(
    calibration: Calibration,
    channel: int,
    volts_per_div: float,
    lever: int,
) -> bytes:
    """Build a calibrated vertical-position packet for four-channel ADC mode."""
    if volts_per_div not in _VOLTS_PER_DIV or not 1 <= channel <= 4 or not 0 <= lever <= 255:
        raise ValueError("unsupported channel, V/div or lever position")
    index = _VOLTS_PER_DIV.index(volts_per_div)
    group = 5 if index < 6 else 8 if index < 9 else 11
    divisor = (50, 20, 10, 5, 2, 1, 5, 2, 1, 5, 2, 1)[index]
    base = (channel - 1) * 144 + group * 12 + 4
    a, b = calibration.zero_words[base : base + 2]
    if divisor != 1:
        center = int((a + b) / 2 + 0.5)
        half = int((b - center) / divisor + 0.5)
        a, b = center - half, center + half
    position = int((b - a) * lever / 255 + a)
    if not 0 <= position <= 65535 or a == b:
        raise ValueError("invalid calibration pair")
    mask = 0 if channel == 1 else 1 << (channel - 2)
    return bytes([mask, 0]) + struct.pack("<H", position)


def _relay_byte(channel: ChannelConfig) -> int:
    vd = _VOLTS_PER_DIV.index(channel.volts_per_div)
    coupling = _COUPLING_CODES[channel.coupling]
    bw = 1 if channel.bandwidth_limit else 0
    high_range = 1 if vd > 8 else 0
    not_high_range = 1 - high_range
    medium_range = 1 if vd > 5 else 0
    not_medium_range = 1 - medium_range
    dc_path = 1 if coupling <= 0 else 0
    return (
        (bw << 7)
        | (high_range << 6)
        | (not_high_range << 5)
        | (medium_range << 4)
        | (not_medium_range << 3)
        | (dc_path << 2)
        | (1 << 1)
    )


def relay_packets(config: ScopeConfig) -> tuple[bytes, bytes]:
    first_values = [_relay_byte(channel) for channel in config.channels]
    first = bytes([0x08, 0x00, *first_values, 0x01, 0x00])
    second = bytes([0x08, 0x00, *(value & 0x86 for value in first_values), 0x01, 0x01])
    return first, second


def _fpga_delay_correction(fpga_version: int | None) -> int:
    """Return the timing correction used by the direct-USB vendor implementation."""
    return 246 if fpga_version in (0xA00A, 0xA00B) else 230


def _timebase_scale(time_div_index: int) -> int:
    try:
        return _TIMEBASE_SCALE[time_div_index]
    except KeyError as exc:
        raise ValueError(
            f"time-div index must be {_DIRECT_TIME_DIV_MIN}..{_DIRECT_TIME_DIV_MAX}"
        ) from exc


def scope_sample_rate_hz(config: ScopeConfig) -> float:
    """Exact 4-channel sample-rate formula from the Android direct-USB SDK."""
    index = config.time_div_index
    if index <= 7:
        return 250_000_000.0
    n = index + 1
    coefficient = 1 if n % 3 == 0 else 2 if n % 3 == 1 else 5
    divisor = coefficient * (10 ** (n // 3))
    return 250_000_000_000.0 / divisor


def time_div_packet(config: ScopeConfig) -> bytes:
    """Build the exact 0F time-div packet for direct-capture timebases."""
    count = _TIME_DIV_COUNTER[config.time_div_index] - 1
    return b"\x0f\x00" + struct.pack("<I", count)


def horizontal_trigger_packet(
    config: ScopeConfig,
    *,
    fpga_version: int | None = None,
) -> bytes:
    """Build the vendor 0x10 horizontal timing packet for a 4096-sample frame."""
    scale = _timebase_scale(config.time_div_index)
    delay = _fpga_delay_correction(fpga_version)
    pre = _HORIZONTAL_TRIGGER_PERCENT * _SAMPLE_COUNT // 100
    post = _SAMPLE_COUNT - pre + 8
    pre += 100
    first = (pre * scale + delay) // 8
    second = post * scale // 8
    return b"\x10\x00" + first.to_bytes(6, "little") + second.to_bytes(6, "little")


def sample_rate_packets(config: ScopeConfig) -> tuple[bytes, ...]:
    """Register writes shared by the verified four-channel direct-capture modes."""
    range_code = (13, 10, 7, 5, 2, 0, 5, 2, 0, 5, 2, 0)
    packed_ranges = 0
    for channel_index, channel in enumerate(config.channels):
        vd = _VOLTS_PER_DIV.index(channel.volts_per_div)
        packed_ranges |= (range_code[vd] & 0x0F) << (4 * channel_index)

    def reg(value: int, register: int) -> bytes:
        return bytes([0x08, 0x00, 0x00, value & 0xFF, (value >> 8) & 0xFF, register, 0x04, 0x00])

    return (
        reg(2064, 0x3A),
        reg(516, 0x3B),
        reg(0, 0x0F),
        reg(516, 0x31),
        reg(packed_ranges, 0x2A),
    )


def ram_trigger_packet(config: ScopeConfig) -> bytes:
    """Build dsoHTSetRamAndTrigerControl for the verified 4-channel path."""
    source = config.trigger_source - 1
    # The direct-USB SDK switches the RAM/ADC timing fields at index 8/9.
    # Four-channel mode uses (mode=0, divider=1) through index 8 and
    # (mode=1, divider=0) from index 9 onward.
    mode = 0 if config.time_div_index <= 8 else 1
    divider = 1 - mode
    control = ((_ALL_CHANNELS_MASK << 2) | mode) & 0x3F
    # Byte 5 bit 2 would flag a disabled trigger-source channel; all four are always on.
    return bytes([0x12, 0x00, control, 0x00, divider, source])


def trigger_code(config: ScopeConfig) -> int:
    channel = config.channels[config.trigger_source - 1]
    lever = config.lever_for(config.trigger_source)
    code = lever + round(config.trigger_level_v * 255 / (8 * channel.volts_per_div))
    if not 0 <= code <= 255:
        min_v = (0 - lever) * 8 * channel.volts_per_div / 255
        max_v = (255 - lever) * 8 * channel.volts_per_div / 255
        raise ValueError(
            f"trigger level {config.trigger_level_v} V is outside "
            f"[{min_v:.6g}, {max_v:.6g}] V for CH{config.trigger_source}"
        )
    return code


def trigger_voltage_packet(code: int) -> bytes:
    if not 0 <= code <= 255:
        raise ValueError("trigger code must be 0..255")
    center = int(code * 200 / 256 + 28.5)
    upper = max(0, min(228, center + 4))
    lower = max(0, min(228, center - 4))
    return bytes([0x07, 0x00] + [upper, upper, lower, lower] * 4 + [center] * 8)


def trigger_mode_packet(config: ScopeConfig) -> bytes:
    return bytes([0x11, 0x00, 0x00, _SLOPE_CODES[config.trigger_slope], 0x00, 0x00])


def ring_buffer_address(
    metadata: bytes,
    *,
    transfer_channels: int,
    timebase_correction: int | None = None,
    time_div_index: int = _TIME_DIV_INDEX,
    fpga_version: int | None = None,
) -> int:
    """Reproduce the metadata-derived 0E ring-buffer address.

    The backend always transfers 4 channels; the 1/2-channel branches are kept
    because they are pinned by Windows reference vectors in the tests.
    """
    if len(metadata) < 5:
        raise ValueError("metadata block is too short")
    if transfer_channels not in (1, 2, 4):
        raise ValueError("transfer_channels must be 1, 2 or 4")
    base = metadata[2] | (metadata[3] << 8)
    phase = metadata[4]
    # The vendor computes ((100 - p) * n + p * n) // 100 for p = trigger percent,
    # which is always the full sample count n.
    address = base - transfer_channels * _SAMPLE_COUNT

    phase_correction = (7 - phase) & 7
    if transfer_channels == 4:
        phase_correction = (phase_correction & 1) - 6
    elif transfer_channels == 2:
        phase_correction = (phase_correction & 3) - 4
    else:
        phase_correction &= 7

    address += phase_correction * transfer_channels
    if timebase_correction is None:
        scale = _timebase_scale(time_div_index)
        timebase_correction = _fpga_delay_correction(fpga_version) // scale
    address -= timebase_correction * transfer_channels
    return address & 0xFFFF


def estimate_frequency(codes: tuple[int, ...], sample_rate_hz: float) -> float | None:
    """Estimate frequency with Schmitt-style rising crossings for diagnostics."""
    if len(codes) < 4 or sample_rate_hz <= 0:
        return None
    lo = min(codes)
    hi = max(codes)
    span = hi - lo
    # Reject a few ADC counts of front-end/quantization noise before applying
    # Schmitt thresholds.  Physical idle captures can span 2-3 codes and would
    # otherwise look like a high-frequency square wave to this diagnostic.
    if span < 4:
        return None

    low_threshold = lo + 0.35 * span
    high_threshold = lo + 0.65 * span
    armed = codes[0] <= low_threshold
    crossings: list[int] = []
    for index, code in enumerate(codes[1:], start=1):
        if not armed:
            if code <= low_threshold:
                armed = True
        elif code >= high_threshold:
            crossings.append(index)
            armed = False

    if len(crossings) < 2:
        return None
    periods = [b - a for a, b in zip(crossings, crossings[1:]) if b > a]
    if not periods:
        return None
    return sample_rate_hz / (sum(periods) / len(periods))


class Scope:
    """Native USB scope driver for verified 4-channel direct-capture modes."""

    def __init__(self, transport: UsbTransport | None = None):
        self.transport = transport or UsbTransport.for_device(SCOPE_USB)
        self._calibration: Calibration | None = None
        self._config: ScopeConfig | None = None
        self._last_capture: Capture | None = None
        self._initialized = False
        self._initialization_failed = False
        self._fpga_version: int | None = None
        # Vendor sync helper reuses one 10-byte buffer across B3 OUT -> B2 IN
        # calls. It rewrites only bytes 0..3 before the next B3, so preserve
        # bytes 4..9 returned by the previous paired B2 response.
        self._sync_buffer = bytearray(_SCOPE_SYNC_SEED)
        self._sync_seq = 0
        self._session = None
        _enable_trace_from_env()

    def _check_session(self) -> bool:
        """Forget per-session hardware state after the transport was reopened.

        The device may have been power-cycled in between, so initialization,
        calibration and the applied configuration can no longer be trusted.
        Returns True when state was reset.
        """
        session = getattr(self.transport, "session", None)
        if session == self._session:
            return False
        self._session = session
        self._initialized = False
        self._initialization_failed = False
        self._fpga_version = None
        self._calibration = None
        self._sync_buffer = bytearray(_SCOPE_SYNC_SEED)
        self._last_capture = None
        return True

    def identity(self) -> dict:
        with self.transport.lock:
            raw = self.transport.control_in(0xA2, 0x1580, 71)
            return {
                "raw_hex": raw.hex(),
                "ascii_fields": [m.decode("ascii") for m in re.findall(rb"[ -~]{3,}", raw)],
            }

    def calibration(self, *, refresh: bool = False) -> Calibration:
        with self.transport.lock:
            self._check_session()
            if self._calibration is not None and not refresh:
                return self._calibration
            zero = self.transport.control_in(0xA2, 0x1600, 1024) + self.transport.control_in(
                0xA2, 0x1A00, 130
            )
            amp = self.transport.control_in(0xA2, 0x1B00, 578)
            dds = self.transport.control_in(0xA2, 0x15E0, 8)
            self._calibration = Calibration(zero=zero, amplitude=amp, dds=dds)
            return self._calibration

    def _prepare_out(self, *, strict: bool = True) -> None:
        # HTHardDll!sync (0x18000a070) keeps a 10-byte stack buffer across
        # the B3/B2 pair. On each invocation only the mode prefix is written;
        # the remaining bytes retain the previous B2 response. Reproduce that
        # state instead of sending a freshly zeroed tail for every B3.
        self._sync_buffer[:4] = _SCOPE_SYNC_SEED[:4]
        self._sync_seq += 1
        seq = self._sync_seq
        _LOG.debug("SYNC[%d] B3 OUT %s", seq, self._sync_buffer.hex(" "))
        try:
            self.transport.control_out(0xB3, 0, bytes(self._sync_buffer))
        except HantekError as exc:
            if strict:
                raise
            _LOG.debug("SYNC[%d] B3 ERROR ignored: %s", seq, exc)

        # The vendor sync helper calls B2 unconditionally even when B3 failed.
        try:
            response = self.transport.control_in(0xB2, 0, 10, allow_short=True)
        except HantekError as exc:
            if strict:
                raise
            _LOG.debug("SYNC[%d] B2 ERROR ignored: %s", seq, exc)
            return

        _LOG.debug("SYNC[%d] B2 IN  len=%d %s", seq, len(response), response.hex(" "))
        self._sync_buffer[: len(response)] = response

    def _prepare_in(self) -> None:
        self.transport.control_in(0xB2, 0, 10, allow_short=True)

    def _send(self, payload: bytes) -> None:
        # HTHardDll!SendOutImpl ignores the sync-helper return value and still
        # performs the bulk OUT. Keep that best-effort behavior only here;
        # calibration/control-read preparation remains strict.
        self._prepare_out(strict=False)
        self.transport.write(payload)

    def _command_read(self, payload: bytes, length: int = _SCOPE_PACKET_SIZE) -> bytes:
        self._send(payload)
        self._prepare_in()
        return self.transport.read(length)

    def _synced_control_in(self, request: int, value: int, length: int) -> bytes:
        """Mirror the DLL's B3/B2 preparation before persistent A2 reads."""
        self._prepare_out()
        return self.transport.control_in(request, value, length)

    def initialize(self) -> int:
        """Run the verified dsoInitHard-equivalent sequence once.

        The vendor DLL attempts dsoSetUSBBus (EA) first but ignores its result.
        This exact scope revision STALLs that request under libusb, so the native
        backend attempts EA best-effort and ignores failure. No flash or
        calibration writes occur.
        """
        with self.transport.lock:
            if not self.transport.is_open:
                raise HantekError("open the transport first", operation="scope.initialize")
            self._check_session()
            if self._initialized:
                assert self._fpga_version is not None
                return self._fpga_version
            if self._initialization_failed:
                raise HantekError(
                    "previous initialization failed; reopen/power-cycle before retrying",
                    operation="scope.initialize",
                )

            try:
                # Android q.open() calls f.a() before hardware init: vendor OUT
                # EA, value 0, ten zero bytes. Its boolean result is discarded,
                # so mirror this as a best-effort direct-USB precondition.
                try:
                    _LOG.debug("INIT EA OUT %s", bytes(10).hex(" "))
                    self.transport.control_out(0xEA, 0, bytes(10))
                except HantekError as exc:
                    _LOG.debug("INIT EA ERROR ignored: %s", exc)

                # The direct-USB Android implementation for Hantek 6074BD
                # primes the FPGA-version path with three identical send_out
                # transactions before the single B2 + bulk-IN response read.
                # Windows HTHardDll sends one, but direct USB needs the triple
                # preamble to reproduce the device state reliably.
                for _ in range(3):
                    self._send(b"\x0c\x00")
                self._prepare_in()
                fpga = self.transport.read(_SCOPE_PACKET_SIZE)
                self._fpga_version = fpga[0] | (fpga[1] << 8)

                # dsoInitHard reads/caches product information at this point.
                self.transport.control_in(0xA2, 0x1580, 71)

                # The independent Android direct-USB 6074BD implementation
                # goes straight from identity read to the five hardware-init
                # writes. Do not perform calibration EEPROM reads here; load
                # calibration lazily after hardware init succeeds.

                for index, (payload, delay_s) in enumerate(
                    zip(_SCOPE_INIT_WRITES, _SCOPE_INIT_DELAYS_S), start=1
                ):
                    _LOG.debug("INIT WRITE #%d %s", index, payload.hex(" "))
                    try:
                        self._send(payload)
                    except HantekError as exc:
                        if index != 5:
                            raise
                        # HTHardDll InitPart2 keeps the return value from init
                        # write #2, calls the helper that sends #3..#5, and
                        # ignores that helper's return value entirely. On this
                        # unit #5 is the first write that fails after the relay
                        # / hardware-state transition caused by #4, so mirror
                        # the vendor's non-fatal behavior for #5 only.
                        _LOG.debug("INIT WRITE #5 ERROR ignored: %s", exc)
                    if delay_s:
                        time.sleep(delay_s)

                # Official HTHardDll/VCDSO flow performs dsoHTADCCHModGain(4)
                # immediately after dsoInitHard(), before calibration reads or
                # the rest of the acquisition configuration.
                _LOG.debug("INIT ADC 4CH MODE/GAIN %s", _SCOPE_ADC_4CH_MODE_GAIN.hex(" "))
                self._send(_SCOPE_ADC_4CH_MODE_GAIN)

                self._initialized = True
                return self._fpga_version
            except Exception:
                # Init contains hardware writes; never silently replay a partial
                # sequence in the same session after an unknown failure.
                self._initialization_failed = True
                raise

    def configuration_plan(
        self,
        config: ScopeConfig,
        calibration: Calibration | None = None,
    ) -> tuple[bytes, ...]:
        """The ordered configuration packets (see configuration_steps for timing)."""
        return tuple(payload for payload, _ in self.configuration_steps(config, calibration))

    def configuration_steps(
        self,
        config: ScopeConfig,
        calibration: Calibration | None = None,
    ) -> tuple[tuple[bytes, float], ...]:
        """Ordered (payload, delay after sending in seconds) pairs applied by configure()."""
        cal = calibration or self._calibration
        if cal is None:
            raise HantekError(
                "calibration is required to build channel-position packets",
                operation="scope.configuration_plan",
            )
        sample = tuple((packet, 0.0) for packet in sample_rate_packets(config))
        relay_first, relay_second = relay_packets(config)
        horizontal = (horizontal_trigger_packet(config, fpga_version=self._fpga_version), 0.0)
        positions = tuple(
            (
                channel_position_packet(
                    cal,
                    channel,
                    config.channels[channel - 1].volts_per_div,
                    config.lever_for(channel),
                ),
                0.0,
            )
            for channel in _CHANNELS
        )
        return (
            *sample,
            (time_div_packet(config), 0.0),
            horizontal,
            # HTHardDll relay helper (0x18000b140) deliberately gives the analog
            # front-end time to settle: 4 ms after the first relay write, then
            # 50 ms after the masked/latch write.
            (relay_first, _RELAY_FIRST_SETTLE_S),
            (relay_second, _RELAY_SECOND_SETTLE_S),
            *sample,
            horizontal,
            (ram_trigger_packet(config), 0.0),
            *positions,
            (trigger_voltage_packet(trigger_code(config)), 0.0),
            (trigger_mode_packet(config), 0.0),
        )

    def configure(self, config: ScopeConfig) -> tuple[bytes, ...]:
        """Apply an acquisition configuration to hardware."""
        with self.transport.lock:
            if not self.transport.is_open:
                raise HantekError("open the transport first", operation="scope.configure")
            self.initialize()
            steps = self.configuration_steps(config, self.calibration())
            for payload, delay_s in steps:
                self._send(payload)
                if delay_s:
                    time.sleep(delay_s)
            self._config = config
            self._last_capture = None
            time.sleep(_ANALOG_SETTLE_S)
            return tuple(payload for payload, _ in steps)

    def arm(self, *, timeout_s: float = 2.0, poll_interval_s: float = 0.01) -> int:
        """Arm one acquisition and wait until the hardware data-ready bit is set."""
        if timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("timeout and poll interval must be positive")
        with self.transport.lock:
            if not self.transport.is_open:
                raise HantekError("open the transport first", operation="scope.arm")
            if self._config is None:
                raise HantekError("configure the scope first", operation="scope.arm")
            if self._check_session():
                self.configure(self._config)
            self._last_capture = None
            start = bytes([0x03, 0x00, _SWEEP_CODES[self._config.trigger_sweep], 0x00])
            self._send(start)

            deadline = time.monotonic() + timeout_s
            last_state = 0
            while time.monotonic() < deadline:
                response = self._command_read(b"\x06\x00")
                last_state = response[0] | (response[1] << 8)
                if last_state & _STATE_DATA_READY_BIT:
                    return last_state
                time.sleep(poll_interval_s)
            raise HantekError(
                "scope acquisition timed out",
                operation="scope.arm",
                context={"last_state": f"0x{last_state:04x}", "timeout_s": timeout_s},
            )

    def fetch(self, *, state: int = 0) -> Capture:
        """Fetch the most recently completed capture exactly once."""
        with self.transport.lock:
            if not self.transport.is_open:
                raise HantekError("open the transport first", operation="scope.fetch")
            if self._config is None:
                raise HantekError("configure the scope first", operation="scope.fetch")
            if self._check_session():
                raise HantekError(
                    "transport was reopened; arm() a new acquisition first",
                    operation="scope.fetch",
                )
            if self._last_capture is not None:
                return self._last_capture

            config = self._config
            metadata = self._command_read(b"\x0d\x00")
            count = len(_CHANNELS)
            address = ring_buffer_address(
                metadata,
                transfer_channels=count,
                time_div_index=config.time_div_index,
                fpga_version=self._fpga_version,
            )
            self._send(b"\x0e\x00" + struct.pack("<H", address))

            total_bytes = _SAMPLE_COUNT * count
            if total_bytes % 2:
                raise HantekError("odd waveform byte count", operation="scope.fetch")
            self._send(b"\x05\x00" + struct.pack("<H", total_bytes // 2))

            chunks: list[bytes] = []
            remaining = total_bytes
            while remaining:
                size = min(_SCOPE_PACKET_SIZE, remaining)
                chunks.append(self.transport.read(size))
                remaining -= size
            raw = b"".join(chunks)

            normalized = normalize_samples(raw, channels=count)
            sample_rate_hz = scope_sample_rate_hz(config)
            waveforms: list[Waveform] = []
            for packed_index, physical_channel in enumerate(_CHANNELS):
                codes = normalized[packed_index]
                channel_cfg = config.channels[physical_channel - 1]
                lever = config.lever_for(physical_channel)
                if channel_cfg.coupling == "gnd":
                    # Like the vendor SDK: GND keeps the AC relay path and the
                    # samples are replaced by the channel's zero level.
                    codes = (lever,) * len(codes)
                volts = tuple(
                    (sample - lever) * 8 * channel_cfg.volts_per_div / 255
                    for sample in codes
                )
                waveforms.append(
                    Waveform(
                        channel=physical_channel,
                        sample_rate_hz=sample_rate_hz,
                        codes=codes,
                        volts=volts,
                    )
                )
            capture = Capture(
                config=config,
                state=state,
                metadata=metadata,
                ring_address=address,
                raw=raw,
                waveforms=tuple(waveforms),
            )
            self._last_capture = capture
            return capture

    def capture(
        self,
        config: ScopeConfig | None = None,
        *,
        timeout_s: float = 2.0,
        poll_interval_s: float = 0.01,
    ) -> Capture:
        """Configure if requested, arm, wait and fetch one capture."""
        if config is not None:
            self.configure(config)
        elif self._config is None:
            self.configure(ScopeConfig())
        state = self.arm(timeout_s=timeout_s, poll_interval_s=poll_interval_s)
        return self.fetch(state=state)

    @property
    def last_capture(self) -> Capture | None:
        return self._last_capture
