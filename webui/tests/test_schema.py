"""Offline tests: request parsing, limits and response shaping (no USB)."""
from __future__ import annotations

import math
import unittest

import schema
from hantek_linux.scope import Capture, Waveform


def fake_capture(config, *, freq=10_000.0):
    rate = schema.timebase(config.time_div_index)["sample_rate_hz"]
    waveforms = []
    for ch in range(1, 5):
        lever = config.lever_for(ch)
        codes = tuple(
            max(0, min(255, int(lever + 40 * math.sin(2 * math.pi * freq * i / rate)))) for i in range(4096)
        )
        vpc = 8 * config.channels[ch - 1].volts_per_div / 255
        waveforms.append(Waveform(ch, rate, codes, tuple((c - lever) * vpc for c in codes)))
    return Capture(config, 0xC403, bytes(512), 0x1234, bytes(4096 * 4), tuple(waveforms))


class TimebaseTests(unittest.TestCase):
    def test_table_covers_supported_indices(self):
        self.assertEqual([tb["index"] for tb in schema.TIMEBASES], list(range(8, 25)))
        tb = schema.timebase(12)
        self.assertEqual(tb["sample_rate_hz"], 12_500_000.0)
        self.assertAlmostEqual(tb["sec_per_div"], 20e-6)
        self.assertAlmostEqual(tb["frame_s"], 4096 / 12.5e6)
        self.assertEqual(tb["label"], "20 µs/div · 12.5 MS/s · кадр 328 µs")

    def test_window_picks_smallest_covering_timebase(self):
        self.assertEqual(schema.window_to_index(1.0), 23)       # 1.64 s frame
        self.assertEqual(schema.window_to_index(0.8), 22)       # 0.819 s frame
        self.assertEqual(schema.window_to_index(10e-6), 8)
        self.assertEqual(schema.window_to_index(schema.TIMEBASES[-1]["frame_s"]), 24)

    def test_window_limits(self):
        for bad in (0, 5e-7, 3.3, "abc", float("nan")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schema.window_to_index(bad)

    def test_capture_timeout_grows_with_frame(self):
        fast = schema.parse_scope({"time_div_index": 12})
        slow = schema.parse_scope({"time_div_index": 24})
        self.assertEqual(schema.capture_timeout(fast), 2.0)
        self.assertGreater(schema.capture_timeout(slow), 3.28 + 1.0)


class GeneratorParsingTests(unittest.TestCase):
    def test_full_request(self):
        cfg = schema.parse_generator({
            "frequency_hz": "100000", "amplitude_v": 0.5, "offset_v": 0.1, "shape": "square",
            "duty_cycle": 0.25, "single": True, "external_trigger": False, "falling": False,
            "burst_cycles": None, "burst_interval_ms": 2,
        })
        self.assertEqual((cfg.frequency_hz, cfg.shape, cfg.duty_cycle, cfg.single), (100_000.0, "square", 0.25, True))
        self.assertIsNone(cfg.burst_cycles)
        self.assertAlmostEqual(cfg.burst_interval_s, 0.002)

    def test_burst_request(self):
        cfg = schema.parse_generator({"frequency_hz": 100_000, "amplitude_v": 0.5, "burst_cycles": 3})
        self.assertEqual(cfg.burst_cycles, 3)

    def test_rejects_out_of_range(self):
        for bad in ({"frequency_hz": 0}, {"frequency_hz": 26e6}, {"amplitude_v": 3, "offset_v": 1},
                    {"duty_cycle": 0.995}, {"shape": "noise"}, {"burst_cycles": 7},
                    {"burst_cycles": 3, "burst_interval_ms": 20}, {"frequency_hz": "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schema.parse_generator({"frequency_hz": 1000, "amplitude_v": 0.5, **bad})

    def test_plan_info_reports_sync_lock(self):
        locked = schema.plan_info(schema.parse_generator({"frequency_hz": 2e6, "amplitude_v": 0.5}))
        slipping = schema.plan_info(schema.parse_generator({"frequency_hz": 1.5e6, "amplitude_v": 0.5}))
        self.assertTrue(locked["sync_phase_locked"])
        self.assertFalse(slipping["sync_phase_locked"])
        self.assertEqual((slipping["samples"], slipping["periods"]), (4000, 30))
        self.assertEqual(locked["vpp"], 1.0)

    def test_plan_info_burst_density(self):
        info = schema.plan_info(schema.parse_generator({"frequency_hz": 200e3, "amplitude_v": 0.5, "burst_cycles": 3}))
        self.assertAlmostEqual(info["samples_per_carrier_cycle"], 10.42, places=1)
        self.assertAlmostEqual(info["burst_duration_s"], 15e-6)

    def test_plan_info_rejects_aliasing_burst(self):
        with self.assertRaises(ValueError):
            schema.plan_info(schema.parse_generator({"frequency_hz": 1e6, "amplitude_v": 0.5, "burst_cycles": 3}))


class ScopeParsingTests(unittest.TestCase):
    def test_defaults(self):
        cfg = schema.parse_scope({})
        self.assertEqual([cfg.lever_for(ch) for ch in range(1, 5)], [192, 160, 96, 64])
        self.assertEqual((cfg.time_div_index, cfg.trigger_source, cfg.trigger_sweep), (12, 2, "auto"))

    def test_full_request(self):
        cfg = schema.parse_scope({
            "time_div_index": 17, "trigger_source": 1, "trigger_level_v": -0.2,
            "trigger_slope": "falling", "trigger_sweep": "normal",
            "channels": [
                {"volts_per_div": 0.5, "coupling": "ac", "lever": 128, "bandwidth_limit": True},
                {"volts_per_div": "2", "coupling": "dc", "lever": 96},
                {"volts_per_div": 0.002, "coupling": "gnd", "lever": 10},
                {"volts_per_div": 10, "lever": 250},
            ],
        })
        self.assertEqual(cfg.channels[0].coupling, "ac")
        self.assertTrue(cfg.channels[0].bandwidth_limit)
        self.assertEqual(cfg.channels[1].volts_per_div, 2.0)
        self.assertEqual(cfg.channels[2].volts_per_div, 0.002)
        self.assertEqual((cfg.trigger_slope, cfg.trigger_sweep), ("falling", "normal"))

    def test_rejects_invalid(self):
        for bad in ({"time_div_index": 7}, {"time_div_index": 25}, {"trigger_source": 5},
                    {"trigger_level_v": 50}, {"channels": [{"volts_per_div": 0.3}] * 4},
                    {"channels": [{"lever": 300}] * 4}, {"channels": [{}] * 3},
                    {"trigger_slope": "both"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schema.parse_scope(bad)

    def test_trigger_range_matches_source_channel(self):
        lo, hi = schema.trigger_range(schema.parse_scope({"trigger_source": 2}))
        self.assertAlmostEqual(lo, -160 * 16 / 255)
        self.assertAlmostEqual(hi, 95 * 16 / 255)


class PayloadTests(unittest.TestCase):
    def test_capture_payload(self):
        cfg = schema.parse_scope({"time_div_index": 12})
        payload = schema.capture_payload(fake_capture(cfg), 0.05)
        self.assertEqual(payload["trigger_index"], 2048)
        self.assertEqual(payload["state"], "0xc403")
        self.assertEqual(len(payload["channels"]), 4)
        ch1 = payload["channels"][0]
        self.assertEqual((ch1["channel"], ch1["lever"], len(ch1["codes"])), (1, 192, 4096))
        self.assertAlmostEqual(ch1["freq_hz"], 10_000.0, delta=50)
        self.assertEqual(ch1["clipped"], 0)
        self.assertAlmostEqual(payload["capture_ms"], 50.0)

    def test_csv(self):
        cfg = schema.parse_scope({})
        text = schema.capture_csv(fake_capture(cfg))
        lines = text.splitlines()
        self.assertEqual(lines[0], "sample,t_from_trigger_s,ch1_v,ch2_v,ch3_v,ch4_v")
        self.assertEqual(len(lines), 4097)
        self.assertTrue(lines[2049].startswith("2048,0.0,"))


if __name__ == "__main__":
    unittest.main()
