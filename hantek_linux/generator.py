"""Native Hantek1025G waveform planning and explicit USB application.

Voltage scale +/-3.5 V is inferred from the vendor/demo behavior; amplitude_v
is peak amplitude, not Vpp. Closing USB does not stop the physical output.
"""
from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass
from typing import NamedTuple

from .transport import GENERATOR_USB, HantekError, UsbTransport

_MAX_VOLT = 3.5
_MAX_SAMPLES = 4096
# HTDDSDll handshake: every 7-byte A0 control write is answered by 10 bytes on EP 0x81,
# the A1 download start and every 64-byte waveform block by 1 byte; A1 download end is write-only.
_CONTROL_ACK_BYTES = 10
_DOWNLOAD_ACK_BYTES = 1
_TRIGGER_LATCH_SETTLE_S = 0.100   # after the A0 control write with bit 0x20


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    """Waveform request.

    Continuous mode uses burst_cycles=None. Tone-burst mode renders a
    Hann-windowed sine burst into arbitrary-waveform memory and repeats it at
    burst_interval_s.
    """

    frequency_hz: float = 1000.0
    amplitude_v: float = 0.1
    offset_v: float = 0.0
    shape: str = "sine"
    duty_cycle: float = 0.5
    external_trigger: bool = False
    falling: bool = False
    single: bool = False
    burst_cycles: int | None = None
    burst_interval_s: float = 0.001

    def __post_init__(self) -> None:
        for field in (
            "frequency_hz",
            "amplitude_v",
            "offset_v",
            "duty_cycle",
            "burst_interval_s",
        ):
            value = float(getattr(self, field))
            if not math.isfinite(value):
                raise ValueError(f"{field} must be finite")
            object.__setattr__(self, field, value)
        shape = self.shape.lower()
        object.__setattr__(self, "shape", shape)
        if not 1 <= self.frequency_hz <= 25_000_000:
            raise ValueError("frequency must be 1..25 MHz")
        if self.amplitude_v < 0 or self.amplitude_v + abs(self.offset_v) > _MAX_VOLT:
            raise ValueError("waveform exceeds inferred +/-3.5 V range")
        if not 0 < self.duty_cycle < 1:
            raise ValueError("duty cycle must be strictly between 0 and 1")
        if shape not in ("sine", "square", "triangle", "sawtooth"):
            raise ValueError("unsupported shape")
        if self.burst_cycles is not None:
            if type(self.burst_cycles) is not int or not 2 <= self.burst_cycles <= 5:
                raise ValueError("burst_cycles must be an integer in 2..5")
            if not 0.001 <= self.burst_interval_s <= 0.010:
                raise ValueError("burst_interval_s must be 0.001..0.010 s")
            if shape != "sine":
                raise ValueError("tone-burst mode currently supports sine carrier only")
            burst_duration_s = self.burst_cycles / self.frequency_hz
            if burst_duration_s >= self.burst_interval_s:
                raise ValueError("tone burst duration must be shorter than burst interval")


@dataclass(frozen=True, slots=True)
class FrequencyPlan:
    """DDS divider, samples, periods and actual programmed fundamental frequency."""

    divider: int
    samples: int
    periods: int
    actual_hz: float


def frequency_plan(frequency_hz: float) -> FrequencyPlan:
    """Mirror DDSSetFrequency geometry reconstructed from the vendor library."""
    f = float(frequency_hz)
    if not math.isfinite(f) or not 1 <= f <= 25_000_000:
        raise ValueError("frequency must be finite and in 1..25 MHz")
    divider = int(48_828.125 / f) if f <= 100_000 else 0
    if divider:
        periods = 1
        count = int(100_000_000 / (f * divider))
        clock = 100_000_000 / divider
    else:
        periods = 1 if f <= 100_000 else max(1, int(_MAX_SAMPLES / (200_000_000 / f)))
        count = min(_MAX_SAMPLES, int(200_000_000 / f * periods))
        clock = 200_000_000
    if not 1 <= count <= _MAX_SAMPLES:
        raise ValueError("frequency cannot be represented")
    return FrequencyPlan(divider, count, periods, clock * periods / count)


def voltage_code(voltage_v: float) -> int:
    if not math.isfinite(voltage_v) or not -_MAX_VOLT <= voltage_v <= _MAX_VOLT:
        raise ValueError("voltage outside inferred +/-3.5 V range")
    # The official Hantek demo decreases DAC code for positive voltage.
    return max(0, min(4095, int(2048 - 2047 * voltage_v / _MAX_VOLT)))


def _shape_value(shape: str, phase: float, duty_cycle: float) -> float:
    if shape == "sine":
        return math.sin(2 * math.pi * phase)
    if shape == "square":
        return 1.0 if phase < duty_cycle else -1.0
    if shape == "triangle":
        return 4.0 * abs(phase - 0.5) - 1.0
    if shape == "sawtooth":
        return 2.0 * phase - 1.0
    raise ValueError(f"unsupported shape: {shape}")


def render(config: GeneratorConfig, plan: FrequencyPlan) -> tuple[int, ...]:
    """Render a continuous waveform into the exact DDS memory geometry."""
    if config.burst_cycles is not None:
        raise ValueError("render() is for continuous waveforms; use render_tone_burst()")
    samples = []
    for index in range(plan.samples):
        phase = (index * plan.periods / plan.samples) % 1
        value = _shape_value(config.shape, phase, config.duty_cycle)
        samples.append(voltage_code(config.offset_v + config.amplitude_v * value))
    return tuple(samples)


def render_tone_burst(config: GeneratorConfig, plan: FrequencyPlan) -> tuple[int, ...]:
    """Render repeated Hann-windowed sine bursts into the DDS memory.

    plan must describe the repetition frequency, not the carrier frequency.
    The physical sample rate is derived from the exact DDS geometry so carrier
    synthesis remains consistent with the actually programmed repetition rate.
    """
    if config.burst_cycles is None:
        raise ValueError("burst_cycles is required for tone-burst synthesis")

    repetition_hz = plan.actual_hz
    repetition_interval_s = 1.0 / repetition_hz
    sample_rate_hz = plan.samples * repetition_hz / plan.periods
    samples_per_carrier_cycle = sample_rate_hz / config.frequency_hz
    if samples_per_carrier_cycle < 4.0:
        raise ValueError(
            "Hantek1025G arbitrary-waveform memory cannot represent this tone burst "
            f"without aliasing: {samples_per_carrier_cycle:.2f} samples/carrier-cycle"
        )

    burst_duration_s = config.burst_cycles / config.frequency_hz
    if burst_duration_s >= repetition_interval_s:
        raise ValueError(
            "actual DDS repetition interval is too short for the requested tone burst"
        )

    samples: list[int] = []
    for index in range(plan.samples):
        t_s = index / sample_rate_hz
        within_interval_s = t_s % repetition_interval_s
        if within_interval_s < burst_duration_s:
            envelope = 0.5 * (
                1.0 - math.cos(2.0 * math.pi * within_interval_s / burst_duration_s)
            )
            value_v = (
                config.offset_v
                + config.amplitude_v
                * envelope
                * math.sin(2.0 * math.pi * config.frequency_hz * within_interval_s)
            )
        else:
            value_v = config.offset_v
        samples.append(voltage_code(value_v))
    return tuple(samples)


def download_packets(samples: tuple[int, ...], periods: int = 1) -> tuple[bytes, ...]:
    """Pack DDS waveform RAM exactly like the vendor HTDDSDll.

    Bit 13 (0x2000) marks every real sample except the last one.
    Bit 12 (0x1000) is the synchronized-output square-wave marker.  The DLL
    derives it from wavePointNum/TNum and keeps generating that marker through
    the padded tail while repeating the final DAC sample.
    """
    count_samples = len(samples)
    if not 1 <= count_samples <= _MAX_SAMPLES:
        raise ValueError("need 1..4096 samples")
    if any(type(code) is not int or not 0 <= code <= 4095 for code in samples):
        raise ValueError("samples must be 12-bit integers")
    if type(periods) is not int or not 1 <= periods <= count_samples:
        raise ValueError("periods must be an integer in 1..len(samples)")

    period_samples = count_samples // periods
    if period_samples <= 0:
        raise ValueError("invalid DDS period geometry")
    sync_high_samples = int((period_samples + 1) * 0.5)

    padded_count = count_samples + (-count_samples % 64)
    words: list[int] = []
    for i in range(padded_count):
        sample = samples[i] if i < count_samples else samples[-1]
        word = sample & 0x0FFF
        if i % period_samples < sync_high_samples:
            word |= 0x1000
        if i < count_samples - 1:
            word |= 0x2000
        words.append(word)

    payload = struct.pack("<" + "H" * len(words), *words)
    count = count_samples - 1
    prefix = bytes([0xA1, count & 255, (count >> 8) | 0x80])
    suffix = bytes([0xA1, count & 255, count >> 8])
    return (prefix, *(payload[i : i + 64] for i in range(0, len(payload), 64)), suffix)


class TransferStep(NamedTuple):
    """One Bulk OUT packet, the acknowledgement bytes read back, and the pause after it."""

    packet: bytes
    ack_bytes: int = 0
    delay_after_s: float = 0.0


@dataclass(frozen=True, slots=True)
class WaveformPlan:
    """Complete ordered transfer steps and DDS frequency metadata.

    In tone-burst mode, frequency describes the repetition frequency while
    GeneratorConfig.frequency_hz remains the carrier frequency.
    """

    frequency: FrequencyPlan
    steps: tuple[TransferStep, ...]

    @property
    def packets(self) -> tuple[bytes, ...]:
        return tuple(step.packet for step in self.steps)


def plan_waveform(config: GeneratorConfig) -> WaveformPlan:
    dds_frequency_hz = (
        config.frequency_hz
        if config.burst_cycles is None
        else 1.0 / config.burst_interval_s
    )
    frequency = frequency_plan(dds_frequency_hz)

    def control(bits: int) -> TransferStep:
        packet = bytes([0xA0, bits, 0, 0, 0, frequency.divider & 255, frequency.divider >> 8])
        return TransferStep(packet, _CONTROL_ACK_BYTES, _TRIGGER_LATCH_SETTLE_S if bits & 0x20 else 0.0)

    samples = (
        render(config, frequency)
        if config.burst_cycles is None
        else render_tone_burst(config, frequency)
    )
    start, *blocks, end = download_packets(samples, frequency.periods)
    single = 0x04 if config.single else 0
    trigger = (0x10 | (0x08 if config.falling else 0)) if config.external_trigger else 0
    # Reproduce the observed trigger transition: set/clear 0x20, then apply edge bits.
    steps = (
        control(0),
        TransferStep(start, _DOWNLOAD_ACK_BYTES),
        *(TransferStep(block, _DOWNLOAD_ACK_BYTES) for block in blocks),
        TransferStep(end),
        control(0x20),
        control(0),
        control(trigger),
        control(trigger | single),
    )
    return WaveformPlan(frequency, steps)


class Generator:
    """Plan waveforms offline; apply only with dry_run=False.

    Application can cause transient output while memory is reprogrammed.
    No hardware mute, persistence write or rollback is implied.
    """

    def __init__(self, transport: UsbTransport | None = None):
        self.transport = transport or UsbTransport.for_device(GENERATOR_USB)
        self.last_applied: GeneratorConfig | None = None
        self.last_transfer_succeeded = False

    def apply(self, config: GeneratorConfig, *, dry_run: bool = True) -> WaveformPlan:
        plan = plan_waveform(config)
        if dry_run:
            return plan
        with self.transport.lock:
            if not self.transport.is_open:
                raise HantekError("open the transport first", operation="generator.apply")
            self.last_transfer_succeeded = False
            self.last_applied = None
            # Do not retry packets on failure: waveform RAM may be partly written.
            for step in plan.steps:
                self.transport.write(step.packet)
                if step.ack_bytes:
                    self.transport.read(step.ack_bytes)
                if step.delay_after_s:
                    time.sleep(step.delay_after_s)
            self.last_applied = config
            self.last_transfer_succeeded = True
        return plan

    def set_zero(self, *, dry_run: bool = True) -> WaveformPlan:
        """Program main OUTPUT to midscale DC; not high-Z. SYNC OUT may still toggle."""
        return self.apply(GeneratorConfig(amplitude_v=0), dry_run=dry_run)
