"""Request parsing, limits and response shaping for the web UI. No USB access here."""
from __future__ import annotations

import csv
import io
import math

from hantek_linux import ChannelConfig, GeneratorConfig, ScopeConfig, estimate_frequency
from hantek_linux.generator import plan_waveform
from hantek_linux.scope import scope_sample_rate_hz, trigger_code

SAMPLES = 4096
TRIGGER_INDEX = SAMPLES // 2          # hardware trigger point (50 % horizontal position)
SAMPLES_PER_DIV = 250                 # 4096 samples = 16.4 divisions
VOLTS_PER_DIV = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
DEFAULT_LEVERS = (192, 160, 96, 64)      # same defaults as hantek_linux.ScopeConfig
DEFAULT_VOLTS_PER_DIV = (1.0, 2.0, 1.0, 1.0)
SHAPES = ("sine", "square", "triangle", "sawtooth")
COUPLINGS = ("dc", "ac", "gnd")
SLOPES = ("rising", "falling")
SWEEPS = ("auto", "normal")
MIN_WINDOW_S = 1e-6

LIMITS = {
    "frequency_hz": (1.0, 25e6),
    "amplitude_v": (0.0, 3.5),
    "offset_v": (-3.5, 3.5),
    "duty_cycle": (0.01, 0.99),
    "burst_cycles": (2, 5),
    "burst_interval_ms": (1.0, 10.0),
    "lever": (0, 255),
    "time_div_index": (8, 24),
    "live_interval_s": (0.2, 5.0),
}


def si(value: float, unit: str) -> str:
    """Compact engineering notation: 2e-5 s -> '20 µs', 12.5e6 S/s -> '12.5 MS/s'."""
    for factor, prefix in ((1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(value) >= factor * 0.9999:
            return f"{value / factor:.3g} {prefix}{unit}"
    return f"{value:.3g} {unit}"


def timebase(index: int) -> dict:
    rate = scope_sample_rate_hz(ScopeConfig(time_div_index=index))
    tb = {
        "index": index,
        "sample_rate_hz": rate,
        "sec_per_div": SAMPLES_PER_DIV / rate,
        "frame_s": SAMPLES / rate,
    }
    tb["label"] = (f"{si(tb['sec_per_div'], 's')}/div · {si(rate, 'S/s')} · "
                   f"кадр {si(tb['frame_s'], 's')}")
    return tb


TIMEBASES = tuple(timebase(i) for i in range(LIMITS["time_div_index"][0], LIMITS["time_div_index"][1] + 1))
MAX_WINDOW_S = TIMEBASES[-1]["frame_s"]


def _number(value, name: str, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: не число ({value!r})") from None
    if not math.isfinite(number) or not lo <= number <= hi:
        raise ValueError(f"{name}: {value!r} вне диапазона [{lo:g}, {hi:g}]")
    return number


def _integer(value, name: str, lo: int, hi: int) -> int:
    number = _number(value, name, lo, hi)
    if number != int(number):
        raise ValueError(f"{name}: нужно целое число ({value!r})")
    return int(number)


def _choice(value, options, name: str) -> str:
    text = str(value).lower()
    if text not in options:
        raise ValueError(f"{name}: {value!r}, допустимо {', '.join(options)}")
    return text


def _flag(value) -> bool:
    return value is True or str(value).lower() in ("1", "true", "on", "yes")


def window_to_index(window_s) -> int:
    """Smallest direct-capture time base whose 4096-sample frame covers the window."""
    window = _number(window_s, "окно, с", MIN_WINDOW_S, MAX_WINDOW_S)
    for tb in TIMEBASES:
        if tb["frame_s"] >= window * (1 - 1e-9):
            return tb["index"]
    return TIMEBASES[-1]["index"]


def parse_generator(data: dict) -> GeneratorConfig:
    burst = data.get("burst_cycles")
    burst_cycles = None if burst in (None, "", 0, "0", False) else _integer(burst, "burst_cycles", *LIMITS["burst_cycles"])
    return GeneratorConfig(
        frequency_hz=_number(data.get("frequency_hz", 1000), "frequency_hz", *LIMITS["frequency_hz"]),
        amplitude_v=_number(data.get("amplitude_v", 0.1), "amplitude_v", *LIMITS["amplitude_v"]),
        offset_v=_number(data.get("offset_v", 0.0), "offset_v", *LIMITS["offset_v"]),
        shape=_choice(data.get("shape", "sine"), SHAPES, "shape"),
        duty_cycle=_number(data.get("duty_cycle", 0.5), "duty_cycle", *LIMITS["duty_cycle"]),
        single=_flag(data.get("single")),
        external_trigger=_flag(data.get("external_trigger")),
        falling=_flag(data.get("falling")),
        burst_cycles=burst_cycles,
        burst_interval_s=_number(data.get("burst_interval_ms", 1.0), "burst_interval_ms",
                                 *LIMITS["burst_interval_ms"]) / 1000.0,
    )


def plan_info(config: GeneratorConfig) -> dict:
    """Dry-run plan; raises ValueError for waveforms the DDS memory cannot represent."""
    freq = plan_waveform(config).frequency
    requested = config.frequency_hz if config.burst_cycles is None else 1.0 / config.burst_interval_s
    info = {
        "requested_hz": requested,
        "actual_hz": freq.actual_hz,
        "error_pct": (freq.actual_hz / requested - 1) * 100,
        "samples": freq.samples,
        "periods": freq.periods,
        "divider": freq.divider,
        "sync_phase_locked": freq.samples % freq.periods == 0,
        "vpp": 2 * config.amplitude_v,
        "burst": config.burst_cycles is not None,
    }
    if config.burst_cycles is not None:
        dds_rate = freq.samples * freq.actual_hz / freq.periods
        info["samples_per_carrier_cycle"] = dds_rate / config.frequency_hz
        info["burst_duration_s"] = config.burst_cycles / config.frequency_hz
    return info


def parse_scope(data: dict) -> ScopeConfig:
    raw_channels = data.get("channels") or [{}, {}, {}, {}]
    if len(raw_channels) != 4:
        raise ValueError("нужны настройки ровно для 4 каналов")
    channels = []
    for number, raw in enumerate(raw_channels, start=1):
        vdiv = _number(raw.get("volts_per_div", DEFAULT_VOLTS_PER_DIV[number - 1]), f"CH{number} V/div", VOLTS_PER_DIV[0], VOLTS_PER_DIV[-1])
        match = [v for v in VOLTS_PER_DIV if math.isclose(v, vdiv, rel_tol=1e-6)]
        if not match:
            raise ValueError(f"CH{number} V/div: {vdiv:g} не из списка {VOLTS_PER_DIV}")
        channels.append(ChannelConfig(
            volts_per_div=match[0],
            coupling=_choice(raw.get("coupling", "dc"), COUPLINGS, f"CH{number} coupling"),
            bandwidth_limit=_flag(raw.get("bandwidth_limit")),
            lever=_integer(raw.get("lever", DEFAULT_LEVERS[number - 1]), f"CH{number} lever", *LIMITS["lever"]),
        ))
    config = ScopeConfig(
        channels=tuple(channels),
        trigger_source=_integer(data.get("trigger_source", 2), "trigger_source", 1, 4),
        trigger_level_v=_number(data.get("trigger_level_v", 3.0), "trigger_level_v", -1000, 1000),
        trigger_slope=_choice(data.get("trigger_slope", "rising"), SLOPES, "trigger_slope"),
        trigger_sweep=_choice(data.get("trigger_sweep", "auto"), SWEEPS, "trigger_sweep"),
        time_div_index=_integer(data.get("time_div_index", 12), "time_div_index", *LIMITS["time_div_index"]),
    )
    trigger_code(config)  # ValueError with the allowed range if the level is off-screen
    return config


def trigger_range(config: ScopeConfig) -> tuple[float, float]:
    channel = config.channels[config.trigger_source - 1]
    lever = config.lever_for(config.trigger_source)
    step = 8 * channel.volts_per_div / 255
    return (0 - lever) * step, (255 - lever) * step


def capture_timeout(config: ScopeConfig) -> float:
    frame = timebase(config.time_div_index)["frame_s"]
    return max(2.0, 1.5 * frame + 1.0)


def measure(waveform) -> dict:
    volts = waveform.volts
    mean = sum(volts) / len(volts)
    return {
        "freq_hz": estimate_frequency(waveform.codes, waveform.sample_rate_hz),
        "vmin": min(volts),
        "vmax": max(volts),
        "vpp": max(volts) - min(volts),
        "mean": mean,
        "rms_ac": math.sqrt(sum((v - mean) ** 2 for v in volts) / len(volts)),
        "clipped": sum(1 for code in waveform.codes if code in (0, 255)),
    }


def capture_payload(capture, elapsed_s: float) -> dict:
    config = capture.config
    tb = timebase(config.time_div_index)
    channels = []
    for waveform in capture.waveforms:
        channel = config.channels[waveform.channel - 1]
        channels.append({
            "channel": waveform.channel,
            "codes": list(waveform.codes),
            "lever": config.lever_for(waveform.channel),
            "volts_per_div": channel.volts_per_div,
            "volts_per_code": 8 * channel.volts_per_div / 255,
            "coupling": channel.coupling,
            **measure(waveform),
        })
    return {
        "time_div_index": config.time_div_index,
        "sample_rate_hz": tb["sample_rate_hz"],
        "sec_per_div": tb["sec_per_div"],
        "frame_s": tb["frame_s"],
        "trigger_index": TRIGGER_INDEX,
        "trigger_source": config.trigger_source,
        "trigger_level_v": config.trigger_level_v,
        "trigger_slope": config.trigger_slope,
        "trigger_sweep": config.trigger_sweep,
        "trigger_range_v": trigger_range(config),
        "state": f"0x{capture.state:04x}",
        "ring_address": f"0x{capture.ring_address:04x}",
        "capture_ms": elapsed_s * 1000,
        "channels": channels,
    }


def capture_csv(capture) -> str:
    rate = capture.waveforms[0].sample_rate_hz
    waveforms = sorted(capture.waveforms, key=lambda w: w.channel)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["sample", "t_from_trigger_s", *(f"ch{w.channel}_v" for w in waveforms)])
    for i in range(len(waveforms[0].volts)):
        writer.writerow([i, (i - TRIGGER_INDEX) / rate, *(w.volts[i] for w in waveforms)])
    return out.getvalue()


def meta() -> dict:
    return {
        "timebases": list(TIMEBASES),
        "volts_per_div": list(VOLTS_PER_DIV),
        "default_levers": list(DEFAULT_LEVERS),
        "shapes": list(SHAPES),
        "couplings": list(COUPLINGS),
        "limits": LIMITS,
        "window_s": [MIN_WINDOW_S, MAX_WINDOW_S],
        "samples": SAMPLES,
        "trigger_index": TRIGGER_INDEX,
    }
