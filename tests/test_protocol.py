"""Offline golden-vector and failure-path checks."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import struct
import unittest
from pathlib import Path
from threading import RLock
from unittest import mock

from hantek_linux import scope as scope_module
from hantek_linux.cli import _add_scope_args
from hantek_linux.generator import (
    Generator, GeneratorConfig, download_packets, frequency_plan, plan_waveform,
    render_tone_burst, voltage_code,
)
from hantek_linux.scope import (
    Calibration, ChannelConfig, Scope, ScopeConfig,
    estimate_frequency, horizontal_trigger_packet, normalize_samples,
    ram_trigger_packet, ring_buffer_address, scope_sample_rate_hz,
    time_div_packet,
)
from hantek_linux.transport import HantekError, UsbTransport


class FakeTransport:
    def __init__(self):
        self.lock = RLock()
        self.is_open = True
        self.writes = []
        self.fail_at = None
        self.reads = []

    def write(self, data):
        if len(self.writes) == self.fail_at:
            raise HantekError("injected disconnect", operation="write")
        self.writes.append(data)

    def read(self, length):
        self.reads.append(("bulk", length))
        return bytes(length)

    def control_in(self, request, value, length):
        self.reads.append((request, value, length))
        return bytes(length)


class ScopeFakeTransport:
    """Answers every scope transfer; records writes and sleeps in one event list."""

    def __init__(self):
        self.lock = RLock()
        self.is_open = True
        self.session = 1
        self.events = []
        self.fail_writes = False

    def control_out(self, request, value, data=b"", **kwargs):
        self.events.append(("control_out", request))

    def control_in(self, request, value, length, *, allow_short=False, **kwargs):
        if request == 0xA2 and value == 0x1600:
            return _FAKE_ZERO_CAL[:1024]
        if request == 0xA2 and value == 0x1A00:
            return _FAKE_ZERO_CAL[1024:]
        return bytes(length)

    def write(self, data):
        if self.fail_writes:
            raise HantekError("injected failure", operation="bulk_out")
        self.events.append(("write", bytes(data)))

    def read(self, length):
        # First two bytes: FPGA version 0xD003 and a state word with data-ready bit 0x02.
        return bytes.fromhex("03 d0") + bytes(i % 200 + 28 for i in range(length - 2))


def _fake_zero_calibration() -> bytes:
    words = [4] * 577
    for channel in range(4):
        for group in (5, 8, 11):
            base = channel * 144 + group * 12
            words[base + 4], words[base + 5] = 13_000, 43_000
    return struct.pack("<577H", *words)


_FAKE_ZERO_CAL = _fake_zero_calibration()


class ProtocolTests(unittest.TestCase):
    def test_frequency_vectors_from_windows_reference(self) -> None:
        for f, count, periods, divider in [
            (1, 2048, 1, 48828), (1000, 2083, 1, 48),
            (10000, 2500, 1, 4), (100000, 2000, 1, 0),
            (500000, 4000, 10, 0), (1000000, 4000, 20, 0),
            (2250000, 4088, 46, 0), (10000000, 4080, 204, 0),
            (25000000, 4096, 512, 0),
        ]:
            with self.subTest(f=f):
                p = frequency_plan(f)
                self.assertEqual((p.samples, p.periods, p.divider), (count, periods, divider))
                self.assertLess(abs(p.actual_hz / f - 1), 0.001)

    def test_download_sync_marker_and_padding(self) -> None:
        packets = download_packets((0x000, 0x123, 0xABC))
        self.assertEqual(packets[0], bytes.fromhex("a1 02 80"))
        self.assertEqual(packets[-1], bytes.fromhex("a1 02 00"))
        words = struct.unpack("<64H", b"".join(packets[1:-1]))
        # period=3, sync-high=floor((3+1)/2)=2:
        # bit12 is high for indices mod 3 in {0,1}; bit13 only marks real
        # samples before the last one. Padding repeats only the DAC code.
        self.assertEqual(words[:6], (0x3000, 0x3123, 0x0ABC, 0x1ABC, 0x1ABC, 0x0ABC))
        self.assertTrue(all(len(packet) == 64 for packet in packets[1:-1]))

    def test_10khz_sync_marker_is_half_period_square(self) -> None:
        packets = download_packets((0x800,) * 2500, periods=1)
        words = struct.unpack("<2560H", b"".join(packets[1:-1]))
        self.assertTrue(all(word & 0x1000 for word in words[:1250]))
        self.assertTrue(all(not (word & 0x1000) for word in words[1250:2500]))
        self.assertTrue(all(word & 0x2000 for word in words[:2499]))
        self.assertFalse(words[2499] & 0x2000)
        # Vendor padding continues the sync marker while repeating the last DAC code.
        self.assertTrue(words[2500] & 0x1000)

    def test_full_memory_count(self) -> None:
        packets = download_packets((2048,) * 4096)
        self.assertEqual(packets[0], bytes.fromhex("a1 ff 8f"))
        self.assertEqual(packets[-1], bytes.fromhex("a1 ff 0f"))
        self.assertEqual(len(packets), 130)

    def test_dac_polarity(self) -> None:
        self.assertEqual(voltage_code(0), 2048)
        self.assertEqual(voltage_code(3.5), 1)
        self.assertEqual(voltage_code(-3.5), 4095)

    def test_trigger_bit_sequence(self) -> None:
        plan = plan_waveform(GeneratorConfig(external_trigger=True, falling=True, single=True))
        self.assertEqual([p[1] for p in plan.packets if p[0] == 0xA0 and len(p) == 7],
                         [0, 0x20, 0, 0x18, 0x1C])

    def test_invalid_values_rejected(self) -> None:
        for kwargs in [dict(frequency_hz=float("nan")), dict(frequency_hz=0),
                       dict(amplitude_v=-1), dict(amplitude_v=3, offset_v=1),
                       dict(duty_cycle=1), dict(offset_v=float("inf"))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                GeneratorConfig(**kwargs)

    def test_dry_run_never_writes(self) -> None:
        transport = FakeTransport()
        generator = Generator(transport)
        generator.apply(GeneratorConfig())
        self.assertEqual(transport.writes, [])
        self.assertFalse(generator.last_transfer_succeeded)

    def test_partial_failure_invalidates_state_without_retry(self) -> None:
        transport = FakeTransport()
        generator = Generator(transport)
        generator.apply(GeneratorConfig(), dry_run=False)
        self.assertTrue(generator.last_transfer_succeeded)
        transport.writes.clear()
        transport.fail_at = 3
        with self.assertRaises(HantekError):
            generator.apply(GeneratorConfig(), dry_run=False)
        self.assertEqual(len(transport.writes), 3)
        self.assertFalse(generator.last_transfer_succeeded)
        self.assertIsNone(generator.last_applied)

    def test_scope_calibration_layout(self) -> None:
        transport = FakeTransport()
        result = Scope(transport).calibration()
        self.assertEqual(transport.reads, [
            (0xA2, 0x1600, 1024), (0xA2, 0x1A00, 130),
            (0xA2, 0x1B00, 578), (0xA2, 0x15E0, 8),
        ])
        self.assertEqual(len(result.zero_words), 577)
        self.assertEqual(len(result.amplitude_words), 289)

    def test_scope_initialize_matches_vendor_init_sequence(self) -> None:
        class InitTransport:
            def __init__(self):
                self.lock = RLock()
                self.is_open = True
                self.events = []

            def control_out(self, request, value, data=b"", **kwargs):
                self.events.append(("control_out", request, value, bytes(data)))

            def control_in(
                self, request, value, length, *, allow_short=False, request_type=0xC0, **kwargs
            ):
                self.events.append(("control_in", request, value, length, allow_short))
                if request == 0xB2:
                    return bytes.fromhex("aa bb cc dd 10 20 30 40 50 60")
                return bytes(length)

            def write(self, data):
                self.events.append(("write", bytes(data)))

            def reopen(self):
                self.events.append(("reopen",))

            def read(self, length):
                self.events.append(("read", length))
                return bytes.fromhex("01 d0") + bytes(length - 2)

        transport = InitTransport()
        scope = Scope(transport)
        self.assertEqual(scope.initialize(), 0xD001)

        writes = [event[1] for event in transport.events if event[0] == "write"]
        self.assertEqual(
            writes,
            [
                bytes.fromhex("0c 00"),
                bytes.fromhex("0c 00"),
                bytes.fromhex("0c 00"),
                bytes.fromhex("08 00 00 77 47 12 04 00"),
                bytes.fromhex("08 00 00 03 00 33 04 00"),
                bytes.fromhex("08 00 00 65 00 30 02 00"),
                bytes.fromhex("08 00 00 28 f1 0f 02 00"),
                bytes.fromhex("08 00 00 12 38 01 02 00"),
                bytes.fromhex("08 00 00 31 00 55 04 00"),
            ],
        )

        control_outs = [event for event in transport.events if event[0] == "control_out"]
        ea_outs = [event for event in control_outs if event[1] == 0xEA]
        b3_outs = [event for event in control_outs if event[1] == 0xB3]
        self.assertEqual(ea_outs, [("control_out", 0xEA, 0, bytes(10))])
        self.assertEqual(len(b3_outs), 9)
        self.assertEqual(b3_outs[0][3], bytes.fromhex("0f 03 03 03 00 00 00 00 00 00"))
        self.assertTrue(
            all(
                event[3] == bytes.fromhex("0f 03 03 03 10 20 30 40 50 60")
                for event in b3_outs[1:]
            )
        )

        a2_reads = [
            (event[2], event[3])
            for event in transport.events
            if event[0] == "control_in" and event[1] == 0xA2
        ]
        self.assertEqual(a2_reads, [(0x1580, 71)])
        self.assertIsNone(scope._calibration)

        event_count = len(transport.events)
        self.assertEqual(scope.initialize(), 0xD001)
        self.assertEqual(len(transport.events), event_count)


    def test_scope_sendout_ignores_sync_failure_like_vendor_dll(self) -> None:
        class SyncFailureTransport:
            def __init__(self):
                self.lock = RLock()
                self.is_open = True
                self.events = []
                self.control_out_count = 0

            def control_out(self, request, value, data=b"", **kwargs):
                self.events.append(("control_out", request, value, bytes(data)))
                if request == 0xB3:
                    self.control_out_count += 1
                    if self.control_out_count == 8:
                        raise HantekError("injected B3 failure", operation="control_out")

            def control_in(
                self, request, value, length, *, allow_short=False, request_type=0xC0, **kwargs
            ):
                self.events.append(("control_in", request, value, length, allow_short))
                if request == 0xB2:
                    return b"\x01"
                return bytes(length)

            def write(self, data):
                self.events.append(("write", bytes(data)))

            def reopen(self):
                self.events.append(("reopen",))

            def read(self, length):
                self.events.append(("read", length))
                return bytes.fromhex("01 d0") + bytes(length - 2)

        transport = SyncFailureTransport()
        scope = Scope(transport)
        self.assertEqual(scope.initialize(), 0xD001)

        writes = [event[1] for event in transport.events if event[0] == "write"]
        self.assertEqual(
            writes[-2],
            bytes.fromhex("08 00 00 12 38 01 02 00"),
        )
        self.assertEqual(
            writes[-1],
            bytes.fromhex("08 00 00 31 00 55 04 00"),
        )
        self.assertEqual(transport.control_out_count, 9)


    def test_scope_init_write_5_failure_is_nonfatal_like_vendor_dll(self) -> None:
        class InitWrite5FailureTransport:
            def __init__(self):
                self.lock = RLock()
                self.is_open = True
                self.events = []

            def control_out(self, request, value, data=b"", **kwargs):
                self.events.append(("control_out", request, value, bytes(data)))

            def control_in(
                self, request, value, length, *, allow_short=False, request_type=0xC0, **kwargs
            ):
                self.events.append(("control_in", request, value, length, allow_short))
                if request == 0xB2:
                    return b"\x01"
                return bytes(length)

            def write(self, data):
                payload = bytes(data)
                self.events.append(("write", payload))
                if payload == bytes.fromhex("08 00 00 12 38 01 02 00"):
                    raise HantekError("injected init write #5 failure", operation="bulk_out")

            def read(self, length):
                self.events.append(("read", length))
                return bytes.fromhex("01 d0") + bytes(length - 2)

        transport = InitWrite5FailureTransport()
        scope = Scope(transport)
        self.assertEqual(scope.initialize(), 0xD001)
        self.assertTrue(scope._initialized)

    def test_scope_adc_normalization_reference(self) -> None:
        self.assertEqual(normalize_samples(bytes.fromhex("b3 9b 70 57")),
                         ((193,), (163,), (108,), (76,)))

    def test_frequency_estimator_rejects_midpoint_chatter(self) -> None:
        cycle = (
            0, 0, 20, 40, 49, 51, 49, 52, 60, 80,
            100, 100, 80, 60, 40, 20, 0, 0, 0, 0,
        )
        codes = cycle * 8
        estimate = estimate_frequency(codes, 20_000.0)
        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate, 1_000.0, delta=1.0)

    def test_frequency_estimator_returns_none_for_flat_signal(self) -> None:
        self.assertIsNone(estimate_frequency((100,) * 100, 1_000_000.0))

    def test_frequency_estimator_rejects_low_span_adc_noise(self) -> None:
        noise = (100, 101, 103, 102, 100, 103, 101, 100) * 16
        self.assertIsNone(estimate_frequency(noise, 1_000_000.0))

    def test_scope_ring_address_reference_vectors(self) -> None:
        def meta(base: int, phase: int) -> bytes:
            data = bytearray(512)
            data[2:4] = struct.pack("<H", base)
            data[4] = phase
            return bytes(data)

        self.assertEqual(ring_buffer_address(meta(0x2288, 0x06), transfer_channels=1), 0x1287)
        self.assertEqual(ring_buffer_address(meta(0x4510, 0x06), transfer_channels=2), 0x2506)
        self.assertEqual(ring_buffer_address(meta(0x4510, 0x05), transfer_channels=2), 0x2508)
        self.assertEqual(ring_buffer_address(meta(0x8A20, 0x06), transfer_channels=4), 0x4A04)

    def test_scope_direct_timebase_reference_vectors(self) -> None:
        vectors = (
            (8, 250_000_000.0, "0f 00 00 00 00 00",
             "10 00 4e 04 00 00 00 00 04 04 00 00 00 00",
             "12 00 3c 00 01 01", 0x4928),
            (12, 12_500_000.0, "0f 00 09 00 00 00",
             "10 00 04 54 00 00 00 00 50 50 00 00 00 00",
             "12 00 3d 00 00 01", 0x4A04),
            (17, 250_000.0, "0f 00 f3 01 00 00",
             "10 00 6c 63 10 00 00 00 a0 af 0f 00 00 00",
             "12 00 3d 00 00 01", 0x4A0C),
        )

        metadata = bytearray(512)
        metadata[2:4] = struct.pack("<H", 0x8A20)
        metadata[4] = 0x06

        for index, rate, timediv, horizontal, ram, address in vectors:
            with self.subTest(index=index):
                config = ScopeConfig(time_div_index=index, trigger_source=2)
                self.assertEqual(scope_sample_rate_hz(config), rate)
                self.assertEqual(time_div_packet(config), bytes.fromhex(timediv))
                self.assertEqual(
                    horizontal_trigger_packet(config, fpga_version=0xD001),
                    bytes.fromhex(horizontal),
                )
                self.assertEqual(ram_trigger_packet(config), bytes.fromhex(ram))
                self.assertEqual(
                    ring_buffer_address(
                        bytes(metadata),
                        transfer_channels=4,
                        time_div_index=index,
                        fpga_version=0xD001,
                    ),
                    address,
                )

    def test_scope_rejects_interpolated_and_roll_timebases(self) -> None:
        for index in (7, 25):
            with self.subTest(index=index), self.assertRaises(ValueError):
                ScopeConfig(time_div_index=index)

    def test_scope_trigger_source_is_hardware_configurable(self) -> None:
        ch = (ChannelConfig(),) * 4
        ch1 = ScopeConfig(channels=ch, trigger_source=1, trigger_level_v=0)
        ch2 = ScopeConfig(channels=ch, trigger_source=2, trigger_level_v=0)
        self.assertEqual(ram_trigger_packet(ch1), bytes.fromhex("12 00 3d 00 00 00"))
        self.assertEqual(ram_trigger_packet(ch2), bytes.fromhex("12 00 3d 00 00 01"))

    def test_scope_signal_sync_roles_are_independent(self) -> None:
        cfg = ScopeConfig(signal_channel=3, sync_channel=4, trigger_source=2)
        self.assertEqual((cfg.signal_channel, cfg.sync_channel, cfg.trigger_source), (3, 4, 2))

    def test_scope_rejects_unverified_partial_channel_mode(self) -> None:
        # Only the verified 4-channel / 4096-sample / 50 % geometry is expressible.
        with self.assertRaises(ValueError):
            ScopeConfig(channels=(ChannelConfig(), ChannelConfig()))
        for kwargs in (dict(sample_count=4096), dict(horizontal_trigger_percent=50)):
            with self.subTest(kwargs=kwargs), self.assertRaises(TypeError):
                ScopeConfig(**kwargs)
        with self.assertRaises(TypeError):
            ChannelConfig(enabled=False)

    def test_tone_burst_uses_repetition_frequency(self) -> None:
        config = GeneratorConfig(
            frequency_hz=100_000,
            amplitude_v=1.0,
            burst_cycles=3,
            burst_interval_s=0.001,
        )
        plan = plan_waveform(config)
        self.assertLess(abs(plan.frequency.actual_hz / 1000.0 - 1.0), 0.001)
        samples = render_tone_burst(config, plan.frequency)
        self.assertEqual(len(samples), plan.frequency.samples)
        self.assertTrue(any(sample != voltage_code(0.0) for sample in samples))
        self.assertTrue(any(sample == voltage_code(0.0) for sample in samples))

    def test_tone_burst_rejects_overlong_burst(self) -> None:
        with self.assertRaises(ValueError):
            GeneratorConfig(
                frequency_hz=1000,
                burst_cycles=2,
                burst_interval_s=0.001,
            )

    def test_short_usb_write_is_error(self) -> None:
        class ShortDevice:
            def write(self, *args, **kwargs):
                return 1
        transport = UsbTransport(0x0483, 0x5726, 0x81)
        transport._dev = ShortDevice()
        with self.assertRaises(HantekError):
            transport.write(b"123")

    def test_short_control_response_is_error(self) -> None:
        class ShortDevice:
            def ctrl_transfer(self, *args, **kwargs):
                return bytes(70)
        transport = UsbTransport(0x04B5, 0x6CDE, 0x86)
        transport._dev = ShortDevice()
        with self.assertRaises(HantekError):
            Scope(transport).identity()

    def test_configure_waits_for_analog_settling(self) -> None:
        # Relay re-latch and the position PWM need ~75 ms on the real 6074BD;
        # a capture started earlier returns a drifting baseline.
        transport = ScopeFakeTransport()
        scope = Scope(transport)
        with mock.patch.object(scope_module.time, "sleep",
                               side_effect=lambda s: transport.events.append(("sleep", s))):
            scope.configure(ScopeConfig())
        self.assertEqual(transport.events[-1][0], "sleep")
        self.assertGreaterEqual(transport.events[-1][1], 0.075)

    def test_gnd_coupling_returns_zero_level_like_vendor(self) -> None:
        channels = (ChannelConfig(), ChannelConfig(volts_per_div=2.0),
                    ChannelConfig(coupling="gnd"), ChannelConfig())
        scope = Scope(ScopeFakeTransport())
        with mock.patch.object(scope_module.time, "sleep"):
            scope.configure(ScopeConfig(channels=channels))
        capture = scope.fetch()
        ch3 = capture.waveform(3)
        self.assertEqual(set(ch3.codes), {96})
        self.assertEqual(set(ch3.volts), {0.0})
        self.assertGreater(len(set(capture.waveform(1).codes)), 1)

    def test_new_transport_session_reinitializes_scope(self) -> None:
        transport = ScopeFakeTransport()
        scope = Scope(transport)
        with mock.patch.object(scope_module.time, "sleep"):
            scope.configure(ScopeConfig())
            transport.events.clear()
            scope.arm()
            self.assertNotIn(("write", b"\x0c\x00"), transport.events)

            transport.session += 1  # closed and reopened, e.g. after a power cycle
            transport.events.clear()
            scope.arm()
        writes = [event[1] for event in transport.events if event[0] == "write"]
        self.assertEqual(writes[:3], [b"\x0c\x00"] * 3)  # initialize() ran again
        self.assertIn(bytes.fromhex("08 00 00 31 00 55 04 00"), writes)
        self.assertEqual(writes[-2][:2], b"\x03\x00")  # start acquisition after re-configure

    def test_failed_initialization_is_retryable_in_new_session_only(self) -> None:
        transport = ScopeFakeTransport()
        scope = Scope(transport)
        transport.fail_writes = True
        with self.assertRaises(HantekError):
            scope.initialize()
        transport.fail_writes = False
        with self.assertRaisesRegex(HantekError, "previous initialization failed"):
            scope.initialize()
        transport.session += 1
        with mock.patch.object(scope_module.time, "sleep"):
            self.assertEqual(scope.initialize(), 0xD003)

    def test_cli_capture_defaults_to_auto_sweep(self) -> None:
        # NORMAL with the fixed 1.0 V default never fires on the SYNC trace.
        parser = argparse.ArgumentParser()
        _add_scope_args(parser)
        self.assertEqual(parser.parse_args([]).trigger_sweep, "auto")

    def test_usb_transport_counts_sessions(self) -> None:
        transport = UsbTransport(0x04B5, 0x6CDE, 0x86)
        self.assertEqual(transport.session, 0)

    # ------------------------------------------------------------------ tracing
    def test_scope_sync_trace_goes_through_logging(self) -> None:
        with self.assertLogs("hantek_linux.scope", level="DEBUG") as logs, \
                mock.patch.object(scope_module.time, "sleep"):
            Scope(ScopeFakeTransport()).initialize()
        self.assertIn("DEBUG:hantek_linux.scope:SYNC[1] B3 OUT 0f 03 03 03 00 00 00 00 00 00", logs.output)
        self.assertIn("DEBUG:hantek_linux.scope:INIT WRITE #5 08 00 00 12 38 01 02 00", logs.output)

    def test_hantek_trace_sync_env_prints_trace_to_stderr(self) -> None:
        logger = logging.getLogger("hantek_linux")
        saved = (logger.level, logger.propagate, list(logger.handlers))
        self.addCleanup(lambda: (logger.setLevel(saved[0]), setattr(logger, "propagate", saved[1]),
                                 logger.handlers.__setitem__(slice(None), saved[2])))
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HANTEK_TRACE_SYNC": "1"}), contextlib.redirect_stderr(stderr), \
                mock.patch.object(scope_module.time, "sleep"):
            Scope(ScopeFakeTransport()).initialize()
        self.assertIn("SYNC[1] B3 OUT 0f 03 03 03 00 00 00 00 00 00\n", stderr.getvalue())
        self.assertIn("INIT WRITE #1 08 00 00 77 47 12 04 00\n", stderr.getvalue())

    # ------------------------------------------------------------------ plans with timing
    def test_configuration_steps_carry_relay_delays(self) -> None:
        scope = Scope(ScopeFakeTransport())
        calibration = Calibration(zero=_FAKE_ZERO_CAL, amplitude=bytes(578), dds=bytes(8))
        config = ScopeConfig()
        steps = scope.configuration_steps(config, calibration)
        relay_first, relay_second = scope_module.relay_packets(config)
        self.assertEqual(tuple(payload for payload, _ in steps), scope.configuration_plan(config, calibration))
        self.assertEqual([(payload, delay) for payload, delay in steps if delay],
                         [(relay_first, 0.004), (relay_second, 0.050)])

    def test_configure_sleeps_right_after_each_relay_write(self) -> None:
        transport = ScopeFakeTransport()
        scope = Scope(transport)
        with mock.patch.object(scope_module.time, "sleep",
                               side_effect=lambda s: transport.events.append(("sleep", s))):
            scope.initialize()          # init has its own delays; only configure() is checked
            transport.events.clear()
            scope.configure(ScopeConfig())
        relay_first, relay_second = scope_module.relay_packets(ScopeConfig())
        events = transport.events
        self.assertEqual(events[events.index(("write", relay_first)) + 1], ("sleep", 0.004))
        self.assertEqual(events[events.index(("write", relay_second)) + 1], ("sleep", 0.050))
        self.assertEqual([e for e in events if e[0] == "sleep"],
                         [("sleep", 0.004), ("sleep", 0.050), ("sleep", scope_module._ANALOG_SETTLE_S)])

    def test_waveform_plan_steps_describe_the_handshake(self) -> None:
        plan = plan_waveform(GeneratorConfig(frequency_hz=10_000, amplitude_v=0.5))
        steps = plan.steps
        self.assertEqual(plan.packets, tuple(step.packet for step in steps))
        controls = [step for step in steps if step.packet[0] == 0xA0 and len(step.packet) == 7]
        self.assertEqual({step.ack_bytes for step in controls}, {10})
        self.assertEqual([(step.packet[1], step.delay_after_s) for step in steps if step.delay_after_s],
                         [(0x20, 0.100)])
        self.assertEqual((steps[1].packet[0], steps[1].ack_bytes), (0xA1, 1))     # download start
        self.assertEqual((steps[-5].packet[0], steps[-5].ack_bytes), (0xA1, 0))   # download end: write-only
        self.assertTrue(all(len(s.packet) == 64 and s.ack_bytes == 1 for s in steps[2:-5]))

    def test_generator_reads_acks_as_planned(self) -> None:
        transport = FakeTransport()
        plan = Generator(transport).apply(GeneratorConfig(), dry_run=False)
        expected = [("bulk", step.ack_bytes) for step in plan.steps if step.ack_bytes]
        self.assertEqual(transport.reads, expected)

    # ------------------------------------------------------------------ golden plan with real calibration
    def test_configuration_plan_reproduces_windows_golden_trace(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "calibration-6074bd-20260926.json"
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        calibration = Calibration(**{name: bytes.fromhex(value) for name, value in raw.items()})
        config = ScopeConfig(channels=(ChannelConfig(),) * 4, trigger_source=1,
                             trigger_level_v=0.0, trigger_sweep="auto")
        plan = Scope(FakeTransport()).configuration_plan(config, calibration)
        self.assertEqual([packet.hex(" ") for packet in plan], [
            "08 00 00 10 08 3a 04 00", "08 00 00 04 02 3b 04 00", "08 00 00 00 00 0f 04 00",
            "08 00 00 04 02 31 04 00", "08 00 00 00 00 2a 04 00", "0f 00 09 00 00 00",
            "10 00 04 54 00 00 00 00 50 50 00 00 00 00", "08 00 36 36 36 36 01 00", "08 00 06 06 06 06 01 01",
            "08 00 00 10 08 3a 04 00", "08 00 00 04 02 3b 04 00", "08 00 00 00 00 0f 04 00",
            "08 00 00 04 02 31 04 00", "08 00 00 00 00 2a 04 00", "10 00 04 54 00 00 00 00 50 50 00 00 00 00",
            "12 00 3d 00 00 00", "00 00 8a 8a", "01 00 05 7c", "02 00 7d 63", "04 00 01 54",
            "07 00 b6 b6 ae ae b6 b6 ae ae b6 b6 ae ae b6 b6 ae ae b2 b2 b2 b2 b2 b2 b2 b2",
            "11 00 00 00 00 00",
        ])

    # ------------------------------------------------------------------ USB identities
    def test_usb_ids_are_defined_once(self) -> None:
        from hantek_linux import GENERATOR_USB, SCOPE_USB
        self.assertEqual(tuple(SCOPE_USB), (0x04B5, 0x6CDE, 0x86, 512))
        self.assertEqual(tuple(GENERATOR_USB), (0x0483, 0x5726, 0x81, 64))
        self.assertEqual((Scope().transport.pid, Scope().transport.packet_size), (0x6CDE, 512))
        self.assertEqual((Generator().transport.pid, Generator().transport.packet_size), (0x5726, 64))

    def test_transport_packet_size_is_a_parameter(self) -> None:
        self.assertEqual(UsbTransport(0x04B5, 0x6CDE, 0x86).packet_size, 512)   # known Hantek id
        self.assertEqual(UsbTransport(0x0483, 0x5726, 0x81).packet_size, 64)
        self.assertIsNone(UsbTransport(0x1234, 0x5678, 0x81).packet_size)       # unknown: not checked
        self.assertEqual(UsbTransport(0x1234, 0x5678, 0x81, packet_size=64).packet_size, 64)

    def test_open_validates_endpoints_against_packet_size(self) -> None:
        def fake_device(size):
            endpoints = [mock.Mock(bEndpointAddress=address, bmAttributes=2, wMaxPacketSize=size)
                         for address in (0x02, 0x81)]
            config = {(0, 0): endpoints}
            return mock.Mock(bus=1, address=7, get_active_configuration=lambda: config,
                             is_kernel_driver_active=lambda i: False)

        def open_with(device, transport):
            with mock.patch("usb.core.find", return_value=[device]), \
                    mock.patch("usb.util.claim_interface"), mock.patch("usb.util.dispose_resources"):
                transport.open()

        open_with(fake_device(64), UsbTransport(0x1234, 0x5678, 0x81))            # unknown device: any size
        open_with(fake_device(64), UsbTransport(0x1234, 0x5678, 0x81, packet_size=64))
        with self.assertRaisesRegex(HantekError, "unexpected packet size"):
            open_with(fake_device(512), UsbTransport(0x1234, 0x5678, 0x81, packet_size=64))


if __name__ == "__main__":
    unittest.main()
