# Web UI

A small Flask app for exploring both instruments from a browser. You set the generator, capture
all four scope channels, and see how the waveforms and measurements change as you adjust
settings.

![Web UI: 100 kHz 3-cycle Hann burst on CH1, NORMAL trigger on SYNC OUT (CH2)](docs/screenshot.png)

The interface text is in Russian.

## Features

- **Generator:** frequency, peak amplitude, offset, shape, duty cycle, Hann tone bursts
  (cycles and interval), single mode, and external-trigger bits.
  - A plan preview updates as you type, without any USB traffic. It shows the exact DDS
    frequency, samples/periods, whether SYNC is phase-locked to OUTPUT, and burst sample density.
  - **Apply** programs the generator; **Zero** sets OUTPUT to 0 V DC.
- **Scope:**
  - All direct-capture time bases (1 µs/div … 200 ms/div, frames 16.4 µs … 3.28 s).
  - A **Window** field: enter e.g. `1` s and it picks the smallest covering time base and zooms
    to exactly that window.
  - Per channel: V/div (2 mV … 10 V), DC/AC/GND coupling, zero position (`lever`), bandwidth
    limit, and show/hide.
  - Trigger: source, level (with the allowed range shown), slope, AUTO/NORMAL.
  - **Auto level:** takes an AUTO capture and sets the level to the midpoint of the signal swing.
- **View:**
  - Single capture, or **Live** refresh every 0.2–5 s.
  - Drag to zoom, double-click to reset; dashed lines mark the trigger time and trigger level.
  - Per-channel table: frequency, Vpp, min, max, mean, AC RMS, and clipped samples (red when
    the trace is off-screen).
  - CSV export of the last capture.
- **Inputs:** every input is validated against the library limits. Errors are shown, not
  swallowed.
- **Settings:** remembered in the browser.

## Install and run

From the repository root, in the same virtual environment as the library:

```sh
.venv/bin/pip install ".[web]"      # adds Flask
cd webui
./run.sh                             # background; prints http://<host>:5000
./stop.sh                            # stop and release USB
```

Or run it in the foreground: `python app.py`. Optional environment variables:

- `HOST` — default `0.0.0.0`
- `PORT` — default `5000`
- `IDLE_RELEASE_S` — default `300`
- `PYTHON` — the interpreter `run.sh` uses

`run.sh` looks for `../.venv/bin/python`, `.venv/bin/python`, then `python3`.

> **No authentication.** Anyone who can reach the port can drive the generator. Keep it on a
> trusted LAN, or bind to localhost with `HOST=127.0.0.1`.

## How it works

| File | Role |
|---|---|
| `app.py` | Flask routes: `/api/generator/plan\|apply\|zero`, `/api/capture`, `/api/autolevel`, `/api/csv`, `/api/status`, `/api/release` |
| `instruments.py` | One long-lived USB session shared by all requests |
| `schema.py` | Request parsing and limits, time-base table, response shaping (no USB) |
| `templates/`, `static/` | Single page (vanilla JS) and [uPlot](https://github.com/leeoniya/uPlot) 1.6.31 (MIT, vendored) |
| `tests/` | Offline tests with fake instruments |
| `smoke.py` | End-to-end check against real hardware through HTTP |

Details of the USB session in `instruments.py`:

- The devices are opened on first use and kept open, so live refresh is fast: about 30 ms per
  frame at 20 µs/div.
- `configure()` runs only when the scope settings change; it costs about 0.2 s of relay and
  analog settling.
- A USB error closes the session, and the next request reopens and reinitializes the device.
  A NORMAL-trigger timeout keeps the session open.

While the web UI holds the USB interfaces, the `hantek-linux` CLI and other scripts cannot use
the instruments. Press **Освободить USB** (release USB), stop the app, or wait for the idle
timeout (5 minutes by default).

## Tests

```sh
cd webui
python -m unittest discover -s tests   # offline, no hardware
./run.sh && python smoke.py            # hardware: OUTPUT -> CH1, SYNC OUT -> CH2
```

`smoke.py` ends with the generator in `set_zero()` and USB released.
