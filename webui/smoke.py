"""Hardware smoke test of the running web UI (OUTPUT -> CH1, SYNC OUT -> CH2).

Usage (web UI running): python smoke.py [http://127.0.0.1:5000]
Leaves the generator in set_zero() and the USB released.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000"
RESULTS = []


def call(path, body=None, *, raw=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = res.read()
            return res.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def check(name, ok, **info):
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name, json.dumps(info, default=str)[:300], flush=True)


def scope(**overrides):
    cfg = {
        "time_div_index": 12, "trigger_source": 2, "trigger_level_v": 3.4,
        "trigger_slope": "rising", "trigger_sweep": "auto",
        "channels": [
            {"volts_per_div": 1, "coupling": "dc", "lever": 128},
            {"volts_per_div": 2, "coupling": "dc", "lever": 96},
            {"volts_per_div": 1, "coupling": "dc", "lever": 96},
            {"volts_per_div": 1, "coupling": "dc", "lever": 64},
        ],
    }
    cfg.update(overrides)
    return {"scope": cfg}


def ch(payload, number):
    return payload["channels"][number - 1]


def main():
    status, page = call("/", raw=True)
    check("page", status == 200 and b"uPlot" in page)
    for asset in ("/static/app.js", "/static/uPlot.iife.min.js", "/static/style.css", "/static/uPlot.min.css"):
        status, _ = call(asset, raw=True)
        check(f"asset {asset}", status == 200)
    status, meta = call("/api/meta")
    check("meta", status == 200 and len(meta["timebases"]) == 17)

    status, plan = call("/api/generator/plan", {"frequency_hz": 1.5e6, "amplitude_v": 0.5})
    check("plan 1.5 MHz (no USB)", status == 200 and plan["sync_phase_locked"] is False, plan=plan)
    status, err = call("/api/generator/apply", {"frequency_hz": 0, "amplitude_v": 0.5})
    check("invalid generator -> 400", status == 400, error=err.get("error"))

    status, info = call("/api/generator/apply", {"frequency_hz": 100e3, "amplitude_v": 0.5, "shape": "sine"})
    check("apply 100 kHz sine", status == 200, applied=info.get("applied_at"))
    time.sleep(0.1)

    status, first = call("/api/capture", scope())
    status2, second = call("/api/capture", scope())
    ok = (status == status2 == 200 and abs(ch(second, 1)["freq_hz"] / 1e5 - 1) < 0.005
          and 0.9 < ch(second, 1)["vpp"] < 1.1 and abs(ch(second, 2)["freq_hz"] / 1e5 - 1) < 0.005)
    check("capture CH1/CH2 100 kHz", ok, vpp=ch(second, 1)["vpp"], sync=ch(second, 2)["freq_hz"])
    check("same settings skip configure", second["capture_ms"] < first["capture_ms"] - 50,
          first_ms=first["capture_ms"], second_ms=second["capture_ms"])

    status, level = call("/api/autolevel", scope(trigger_level_v=50))
    check("autolevel from off-screen level", status == 200 and 1.0 < level["trigger_level_v"] < 6.0, **level)
    status, normal = call("/api/capture", scope(trigger_sweep="normal", trigger_level_v=level["trigger_level_v"]))
    check("NORMAL capture at auto level", status == 200 and normal["state"] == "0xc403", state=normal.get("state"))

    status, err = call("/api/capture", scope(trigger_level_v=50))
    check("invalid trigger level -> 400", status == 400, error=err.get("error"))
    status, err = call("/api/capture", scope(trigger_sweep="normal", trigger_level_v=5.9))
    _, st = call("/api/status")
    check("NORMAL timeout -> 502, session kept", status == 502 and "timed out" in err.get("error", "")
          and st["scope_open"], error=err.get("error", "")[:60])

    call("/api/generator/apply", {"frequency_hz": 10, "amplitude_v": 0.5, "shape": "sine"})
    idx = next(t["index"] for t in meta["timebases"] if t["frame_s"] >= 1.0)
    start = time.monotonic()
    status, slow = call("/api/capture", scope(time_div_index=idx))
    check("1 s window (idx %d) with 10 Hz" % idx, status == 200 and abs(ch(slow, 1)["freq_hz"] / 10 - 1) < 0.01,
          freq=ch(slow, 1)["freq_hz"], frame_s=slow["frame_s"], wall_s=round(time.monotonic() - start, 2))

    status, csv = call("/api/csv", raw=True)
    check("csv", status == 200 and csv.count(b"\n") == 4097)

    status, st = call("/api/release", {})
    check("release", status == 200 and not st["scope_open"] and not st["generator_open"])
    cli = [sys.executable, "-m", "hantek_linux.cli", "probe"]
    probe = json.loads(subprocess.run(cli, capture_output=True, text=True, timeout=30).stdout)
    check("CLI sees both devices after release", probe["generator"]["present"] and probe["scope"]["present"])

    call("/api/generator/apply", {"frequency_hz": 20e3, "amplitude_v": 1.0, "shape": "square"})
    status, again = call("/api/capture", scope(time_div_index=14))
    check("reopen after release", status == 200 and abs(ch(again, 1)["freq_hz"] / 2e4 - 1) < 0.005,
          vpp=ch(again, 1)["vpp"])

    status, _ = call("/api/generator/zero", {})
    check("generator zero", status == 200)
    call("/api/release", {})
    print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
