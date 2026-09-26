"""Long-lived USB session shared by all web requests.

Devices are opened on first use and kept open so live capture stays fast.
Other programs (CLI, scripts) cannot use the instruments while this holds them:
call release() or wait for the idle timeout.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from threading import RLock

from hantek_linux import Generator, HantekError, Scope


def _is_trigger_timeout(exc: HantekError) -> bool:
    # NORMAL sweep without a trigger: the USB session itself is still healthy.
    return exc.operation == "scope.arm" and "timed out" in str(exc)


class Instruments:
    def __init__(self, *, idle_release_s: float = 300.0, generator_factory=Generator, scope_factory=Scope):
        self.lock = RLock()
        self.idle_release_s = idle_release_s
        self._generator_factory = generator_factory
        self._scope_factory = scope_factory
        self._generator = None
        self._scope = None
        self._applied_scope_config = None
        self.last_capture = None
        self.last_generator_config = None
        self.last_used = time.monotonic()

    # ------------------------------------------------------------------ helpers
    def _touch(self) -> None:
        self.last_used = time.monotonic()

    def _open_generator(self):
        if self._generator is None:
            generator = self._generator_factory()
            generator.transport.open()
            self._generator = generator
        return self._generator

    def _open_scope(self):
        if self._scope is None:
            scope = self._scope_factory()
            scope.transport.open()
            self._scope = scope
            self._applied_scope_config = None
        return self._scope

    @staticmethod
    def _close(device) -> None:
        try:
            device.transport.close()
        except Exception:  # noqa: BLE001 - a wedged device may fail to release
            pass

    def _drop_generator(self) -> None:
        if self._generator is not None:
            self._close(self._generator)
        self._generator = None

    def _drop_scope(self) -> None:
        if self._scope is not None:
            self._close(self._scope)
        self._scope = None
        self._applied_scope_config = None

    # ------------------------------------------------------------------ generator
    def apply_generator(self, config) -> None:
        with self.lock:
            self._touch()
            generator = self._open_generator()
            try:
                generator.apply(config, dry_run=False)
            except HantekError:
                self._drop_generator()
                raise
            self.last_generator_config = config

    def zero_generator(self) -> None:
        with self.lock:
            self._touch()
            generator = self._open_generator()
            try:
                generator.set_zero(dry_run=False)
            except HantekError:
                self._drop_generator()
                raise
            self.last_generator_config = None

    # ------------------------------------------------------------------ scope
    def capture(self, config, timeout_s: float):
        """Configure only when settings changed, then arm + fetch. Returns (capture, seconds)."""
        with self.lock:
            self._touch()
            scope = self._open_scope()
            start = time.monotonic()
            try:
                if config != self._applied_scope_config:
                    self._applied_scope_config = None
                    scope.configure(config)
                    self._applied_scope_config = config
                capture = scope.capture(timeout_s=timeout_s)
            except HantekError as exc:
                if not _is_trigger_timeout(exc):
                    self._drop_scope()
                raise
            self.last_capture = capture
            self._touch()
            return capture, time.monotonic() - start

    # ------------------------------------------------------------------ session
    def release(self) -> None:
        with self.lock:
            self._drop_generator()
            self._drop_scope()

    def release_if_idle(self, now: float | None = None) -> bool:
        with self.lock:
            now = time.monotonic() if now is None else now
            if (self._generator or self._scope) and now - self.last_used > self.idle_release_s:
                self.release()
                return True
            return False

    def status(self) -> dict:
        # Lock-free on purpose: a slow capture (up to ~5 s) must not stall status polling.
        config = self.last_generator_config
        return {
            "generator_open": self._generator is not None,
            "scope_open": self._scope is not None,
            "idle_s": time.monotonic() - self.last_used,
            "idle_release_s": self.idle_release_s,
            "last_generator": asdict(config) if config is not None else None,
        }
