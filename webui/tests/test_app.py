"""Offline tests of the Flask routes and the instrument session with fake devices."""
from __future__ import annotations

import unittest
from threading import RLock

from app import create_app
from instruments import Instruments
from hantek_linux import HantekError
from test_schema import fake_capture


class FakeTransport:
    def __init__(self):
        self.lock = RLock()
        self.is_open = False
        self.opens = 0

    def open(self):
        self.is_open = True
        self.opens += 1

    def close(self):
        self.is_open = False


class FakeGenerator:
    def __init__(self):
        self.transport = FakeTransport()
        self.applied = []

    def apply(self, config, *, dry_run=True):
        self.applied.append(config)

    def set_zero(self, *, dry_run=True):
        self.applied.append("zero")


class FakeScope:
    def __init__(self):
        self.transport = FakeTransport()
        self.configured = []
        self.fail_with = None
        self._config = None

    def configure(self, config):
        self.configured.append(config)
        self._config = config

    def capture(self, config=None, *, timeout_s=2.0):
        if self.fail_with:
            raise self.fail_with
        return fake_capture(self._config)


class InstrumentsTests(unittest.TestCase):
    def setUp(self):
        self.scopes, self.gens = [], []

        def scope_factory():
            self.scopes.append(FakeScope())
            return self.scopes[-1]

        def gen_factory():
            self.gens.append(FakeGenerator())
            return self.gens[-1]

        self.inst = Instruments(idle_release_s=60, generator_factory=gen_factory, scope_factory=scope_factory)

    def test_configure_only_when_settings_change(self):
        import schema
        a, b = schema.parse_scope({}), schema.parse_scope({"time_div_index": 17})
        self.inst.capture(a, 2.0)
        self.inst.capture(a, 2.0)
        self.inst.capture(b, 2.0)
        self.assertEqual(len(self.scopes), 1)
        self.assertEqual(self.scopes[0].configured, [a, b])

    def test_usb_error_drops_scope_and_reopens(self):
        import schema
        cfg = schema.parse_scope({})
        self.inst.capture(cfg, 2.0)
        self.scopes[0].fail_with = HantekError("EIO", operation="bulk_out")
        with self.assertRaises(HantekError):
            self.inst.capture(cfg, 2.0)
        self.assertFalse(self.inst.status()["scope_open"])
        self.inst.capture(cfg, 2.0)
        self.assertEqual(len(self.scopes), 2)
        self.assertEqual(self.scopes[1].configured, [cfg])

    def test_trigger_timeout_keeps_session(self):
        import schema
        cfg = schema.parse_scope({})
        self.inst.capture(cfg, 2.0)
        self.scopes[0].fail_with = HantekError("scope acquisition timed out", operation="scope.arm")
        with self.assertRaises(HantekError):
            self.inst.capture(cfg, 2.0)
        self.assertTrue(self.inst.status()["scope_open"])

    def test_release_and_idle_release(self):
        import schema
        self.inst.capture(schema.parse_scope({}), 2.0)
        self.inst.apply_generator(schema.parse_generator({"frequency_hz": 1000, "amplitude_v": 0.1}))
        self.assertFalse(self.inst.release_if_idle(now=self.inst.last_used + 30))
        self.assertTrue(self.inst.release_if_idle(now=self.inst.last_used + 61))
        status = self.inst.status()
        self.assertFalse(status["scope_open"] or status["generator_open"])


class AppTests(unittest.TestCase):
    def setUp(self):
        self.gen, self.scope = FakeGenerator(), FakeScope()
        inst = Instruments(generator_factory=lambda: self.gen, scope_factory=lambda: self.scope)
        self.client = create_app(inst).test_client()

    def test_index_and_meta(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("uPlot.iife.min.js", page.get_data(as_text=True))
        meta = self.client.get("/api/meta").get_json()
        self.assertEqual(len(meta["timebases"]), 17)
        self.assertEqual(len(meta["volts_per_div"]), 12)

    def test_generator_plan_does_not_touch_usb(self):
        res = self.client.post("/api/generator/plan", json={"frequency_hz": 1.5e6, "amplitude_v": 0.5})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.get_json()["sync_phase_locked"])
        self.assertEqual(self.gen.applied, [])
        self.assertFalse(self.gen.transport.is_open)

    def test_generator_apply_and_zero(self):
        res = self.client.post("/api/generator/apply", json={"frequency_hz": 1e5, "amplitude_v": 0.5})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.gen.applied[0].frequency_hz, 1e5)
        self.assertEqual(self.client.post("/api/generator/zero").status_code, 200)
        self.assertEqual(self.gen.applied[-1], "zero")

    def test_invalid_input_is_400(self):
        res = self.client.post("/api/generator/apply", json={"frequency_hz": 0})
        self.assertEqual(res.status_code, 400)
        self.assertIn("frequency", res.get_json()["error"])
        self.assertEqual(self.gen.applied, [])

    def test_capture_csv_and_status(self):
        self.assertEqual(self.client.get("/api/csv").status_code, 404)
        res = self.client.post("/api/capture", json={"scope": {"time_div_index": 12}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.get_json()["channels"]), 4)
        csv = self.client.get("/api/csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("attachment", csv.headers["Content-Disposition"])
        self.assertTrue(self.client.get("/api/status").get_json()["scope_open"])
        self.client.post("/api/release")
        self.assertFalse(self.client.get("/api/status").get_json()["scope_open"])

    def test_usb_error_is_502(self):
        self.scope.fail_with = HantekError("EIO", operation="bulk_out")
        res = self.client.post("/api/capture", json={"scope": {}})
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.get_json()["operation"], "bulk_out")

    def test_autolevel_returns_midpoint(self):
        res = self.client.post("/api/autolevel", json={"scope": {"trigger_source": 1, "trigger_sweep": "normal"}})
        body = res.get_json()
        self.assertEqual(res.status_code, 200)
        self.assertAlmostEqual(body["trigger_level_v"], (body["min_v"] + body["max_v"]) / 2)
        self.assertEqual(self.scope.configured[-1].trigger_sweep, "auto")


if __name__ == "__main__":
    unittest.main()
