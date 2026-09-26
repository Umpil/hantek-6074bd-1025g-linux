"""Command-line tools for the native Linux Hantek adapter."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .generator import Generator, GeneratorConfig
from .scope import ChannelConfig, Scope, ScopeConfig, estimate_frequency
from .transport import GENERATOR_USB, SCOPE_USB, UsbTransport


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--signal-channel", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--sync-channel", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--trigger-source", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--trigger-level", type=float, default=1.0, help="trigger level in volts")
    parser.add_argument("--trigger-slope", choices=["rising", "falling"], default="rising")
    # AUTO by default: NORMAL only fires when --trigger-level lies inside the signal swing.
    parser.add_argument("--trigger-sweep", choices=["normal", "auto"], default="auto")
    parser.add_argument(
        "--time-div-index",
        type=int,
        choices=range(8, 25),
        default=12,
        help="direct capture timebase: 8=1us/div, 12=20us/div, 17=1ms/div",
    )
    parser.add_argument("--ch1-vdiv", type=float, default=1.0)
    parser.add_argument("--ch2-vdiv", type=float, default=2.0)
    parser.add_argument("--ch3-vdiv", type=float, default=1.0)
    parser.add_argument("--ch4-vdiv", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--csv", type=Path, help="optional waveform CSV output")


def _scope_config(args: argparse.Namespace) -> ScopeConfig:
    channels = tuple(
        ChannelConfig(volts_per_div=value)
        for value in (args.ch1_vdiv, args.ch2_vdiv, args.ch3_vdiv, args.ch4_vdiv)
    )
    return ScopeConfig(
        channels=channels,
        signal_channel=args.signal_channel,
        sync_channel=args.sync_channel,
        trigger_source=args.trigger_source,
        trigger_level_v=args.trigger_level,
        trigger_slope=args.trigger_slope,
        trigger_sweep=args.trigger_sweep,
        time_div_index=args.time_div_index,
    )


def _capture_summary(capture) -> dict:
    channels = {}
    for waveform in capture.waveforms:
        volts = waveform.volts
        frequency = estimate_frequency(waveform.codes, waveform.sample_rate_hz)
        channels[f"ch{waveform.channel}"] = {
            "samples": len(waveform.codes),
            "min_v": min(volts),
            "max_v": max(volts),
            "mean_v": sum(volts) / len(volts),
            "vpp": max(volts) - min(volts),
            "frequency_hz": frequency,
        }
    return {
        "state": f"0x{capture.state:04x}",
        "ring_address": f"0x{capture.ring_address:04x}",
        "sample_rate_hz": capture.signal.sample_rate_hz,
        "sample_count": len(capture.signal.codes),
        "time_div_index": capture.config.time_div_index,
        "signal_channel": capture.config.signal_channel,
        "sync_channel": capture.config.sync_channel,
        "trigger_source": capture.config.trigger_source,
        "channels": channels,
    }


def _write_csv(capture, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    waveforms = sorted(capture.waveforms, key=lambda item: item.channel)
    count = min(len(w.volts) for w in waveforms)
    sample_rate = waveforms[0].sample_rate_hz
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample", "time_s", *(f"ch{w.channel}_v" for w in waveforms)])
        for index in range(count):
            writer.writerow(
                [index, index / sample_rate, *(waveform.volts[index] for waveform in waveforms)]
            )


def _capture(scope: Scope, config: ScopeConfig, args: argparse.Namespace):
    capture = scope.capture(config, timeout_s=args.timeout)
    if args.csv is not None:
        _write_csv(capture, args.csv)
    return capture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="read USB identity and optional factory calibration")
    probe.add_argument("--calibration", action="store_true")

    gen = sub.add_parser("generate", help="plan or explicitly program the Hantek1025G")
    gen.add_argument("--frequency", type=float, default=1000)
    gen.add_argument("--amplitude", type=float, default=0.1, help="peak volts, NOT Vpp")
    gen.add_argument("--offset", type=float, default=0)
    gen.add_argument("--shape", choices=["sine", "square", "triangle", "sawtooth"], default="sine")
    gen.add_argument("--duty-cycle", type=float, default=0.5)
    gen.add_argument("--burst-cycles", type=int, choices=range(2, 6))
    gen.add_argument(
        "--burst-interval-ms",
        type=float,
        default=1.0,
        help="tone-burst repetition interval in milliseconds",
    )
    gen.add_argument("--apply", action="store_true", help="physically program the output")

    capture = sub.add_parser("capture", help="capture one scope frame")
    _add_scope_args(capture)

    loopback = sub.add_parser(
        "loopback",
        help="capture CH signal/sync roles; generator programming is opt-in",
    )
    _add_scope_args(loopback)
    loopback.add_argument("--frequency", type=float, default=10_000.0)
    loopback.add_argument("--amplitude", type=float, default=1.0, help="peak volts, NOT Vpp")
    loopback.add_argument("--offset", type=float, default=0.0)
    loopback.add_argument(
        "--shape", choices=["sine", "square", "triangle", "sawtooth"], default="square"
    )
    loopback.add_argument("--duty-cycle", type=float, default=0.5)
    loopback.add_argument("--burst-cycles", type=int, choices=range(2, 6))
    loopback.add_argument(
        "--burst-interval-ms",
        type=float,
        default=1.0,
        help="tone-burst repetition interval in milliseconds",
    )
    loopback.add_argument(
        "--apply-generator",
        action="store_true",
        help="physically reprogram the generator before capture",
    )

    args = parser.parse_args()

    if args.command == "probe":
        result = {}
        try:
            with UsbTransport.for_device(GENERATOR_USB) as transport:
                result["generator"] = {"present": True, **transport.describe()}
        except Exception as exc:
            result["generator"] = {
                "present": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

        try:
            with UsbTransport.for_device(SCOPE_USB) as transport:
                scope = Scope(transport)
                result["scope"] = {"present": True, **transport.describe(), **scope.identity()}
                if args.calibration:
                    calibration = scope.calibration()
                    result["calibration"] = {
                        name: {
                            "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "hex": data.hex(),
                        }
                        for name, data in asdict(calibration).items()
                    }
        except Exception as exc:
            result["scope"] = {
                "present": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        print(json.dumps(result, indent=2))
        return

    if args.command == "generate":
        config = GeneratorConfig(
            frequency_hz=args.frequency,
            amplitude_v=args.amplitude,
            offset_v=args.offset,
            shape=args.shape,
            duty_cycle=args.duty_cycle,
            burst_cycles=args.burst_cycles,
            burst_interval_s=args.burst_interval_ms / 1000.0,
        )
        generator = Generator()
        plan = generator.apply(config)
        if args.apply:
            with generator.transport:
                generator.apply(config, dry_run=False)
        print(
            json.dumps(
                {
                    "applied": args.apply,
                    "config": asdict(config),
                    "frequency": asdict(plan.frequency),
                    "packet_count": len(plan.packets),
                    "bytes_out": sum(map(len, plan.packets)),
                },
                indent=2,
            )
        )
        return

    scope_config = _scope_config(args)

    if args.command == "capture":
        scope = Scope()
        with scope.transport:
            result = _capture(scope, scope_config, args)
        print(json.dumps(_capture_summary(result), indent=2))
        return

    generator_config = GeneratorConfig(
        frequency_hz=args.frequency,
        amplitude_v=args.amplitude,
        offset_v=args.offset,
        shape=args.shape,
        duty_cycle=args.duty_cycle,
        burst_cycles=args.burst_cycles,
        burst_interval_s=args.burst_interval_ms / 1000.0,
    )
    generator = Generator()
    generator_plan = generator.apply(generator_config)
    if args.apply_generator:
        with generator.transport:
            generator.apply(generator_config, dry_run=False)

    scope = Scope()
    with scope.transport:
        result = _capture(scope, scope_config, args)

    print(
        json.dumps(
            {
                "generator": {
                    "applied": args.apply_generator,
                    "config": asdict(generator_config),
                    "frequency": asdict(generator_plan.frequency),
                },
                "capture": _capture_summary(result),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
