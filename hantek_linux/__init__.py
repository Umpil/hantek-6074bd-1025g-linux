"""Native Hantek USB adapter; no I/O at import time."""
from __future__ import annotations

from .generator import Generator, GeneratorConfig, TransferStep, WaveformPlan, plan_waveform
from .scope import (
    Capture,
    ChannelConfig,
    Scope,
    ScopeConfig,
    Waveform,
    estimate_frequency,
)
from .transport import GENERATOR_USB, SCOPE_USB, HantekError, UsbDeviceId, UsbTransport

__all__ = [
    "Capture",
    "GENERATOR_USB",
    "ChannelConfig",
    "Generator",
    "GeneratorConfig",
    "HantekError",
    "SCOPE_USB",
    "Scope",
    "ScopeConfig",
    "TransferStep",
    "UsbDeviceId",
    "UsbTransport",
    "Waveform",
    "WaveformPlan",
    "estimate_frequency",
    "plan_waveform",
]
