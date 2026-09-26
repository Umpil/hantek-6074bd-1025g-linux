"""Hantek web UI: poke the generator and the scope from a browser.

Run from this directory: python app.py   (or ./run.sh to start in the background)
Optional environment: HOST (0.0.0.0), PORT (5000), IDLE_RELEASE_S (300).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import replace

from flask import Flask, Response, jsonify, render_template, request

import schema
from hantek_linux import HantekError
from instruments import Instruments


def create_app(instruments: Instruments | None = None) -> Flask:
    app = Flask(__name__)
    inst = instruments or Instruments(idle_release_s=float(os.environ.get("IDLE_RELEASE_S", 300)))
    app.config["INSTRUMENTS"] = inst

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.errorhandler(HantekError)
    def device_error(exc):
        return jsonify(error=str(exc), operation=exc.operation), 502

    @app.errorhandler(Exception)
    def unexpected(exc):
        # e.g. usb.core.USBError "Resource busy" when another program holds the device
        code = getattr(exc, "code", None)
        if isinstance(code, int) and 400 <= code < 600:  # werkzeug HTTP errors
            return jsonify(error=str(exc)), code
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 500

    @app.get("/")
    def index():
        return render_template("index.html", meta=schema.meta())

    @app.get("/api/meta")
    def meta():
        return jsonify(schema.meta())

    @app.post("/api/generator/plan")
    def generator_plan():
        return jsonify(schema.plan_info(schema.parse_generator(body())))

    @app.post("/api/generator/apply")
    def generator_apply():
        config = schema.parse_generator(body())
        info = schema.plan_info(config)  # rejects unrepresentable waveforms before any USB write
        inst.apply_generator(config)
        info["applied_at"] = time.strftime("%H:%M:%S")
        return jsonify(info)

    @app.post("/api/generator/zero")
    def generator_zero():
        inst.zero_generator()
        return jsonify(ok=True, applied_at=time.strftime("%H:%M:%S"))

    @app.post("/api/capture")
    def capture():
        config = schema.parse_scope(body().get("scope", {}))
        result, elapsed = inst.capture(config, schema.capture_timeout(config))
        return jsonify(schema.capture_payload(result, elapsed))

    @app.post("/api/autolevel")
    def autolevel():
        # The current level may be off-screen (that is when auto-level is needed);
        # 0 V is always valid because it maps to the channel's lever code.
        config = schema.parse_scope({**body().get("scope", {}), "trigger_level_v": 0.0})
        auto = replace(config, trigger_sweep="auto")
        result, _ = inst.capture(auto, schema.capture_timeout(auto))
        volts = result.waveform(config.trigger_source).volts
        low, high = min(volts), max(volts)
        lo_limit, hi_limit = schema.trigger_range(config)
        level = min(max((low + high) / 2, lo_limit), hi_limit)
        four_codes = 4 * 8 * config.channels[config.trigger_source - 1].volts_per_div / 255
        warning = None if high - low > four_codes else "размах сигнала на источнике триггера почти нулевой"
        return jsonify(trigger_level_v=level, min_v=low, max_v=high, warning=warning)

    @app.get("/api/csv")
    def csv_download():
        if inst.last_capture is None:
            return jsonify(error="ещё не было захвата"), 404
        name = time.strftime("hantek-%Y%m%d-%H%M%S.csv")
        return Response(schema.capture_csv(inst.last_capture), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.post("/api/release")
    def release():
        inst.release()
        return jsonify(inst.status())

    @app.get("/api/status")
    def status():
        return jsonify(inst.status())

    return app


def _idle_watchdog(inst: Instruments) -> None:
    while True:
        time.sleep(10)
        if inst.release_if_idle():
            print("USB released after idle timeout", flush=True)


if __name__ == "__main__":
    application = create_app()
    threading.Thread(target=_idle_watchdog, args=(application.config["INSTRUMENTS"],), daemon=True).start()
    application.run(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 5000)), threaded=True)
