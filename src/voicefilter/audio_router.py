"""Audio device enumeration and stream helpers around sounddevice.

On Windows, VB-CABLE registers two devices:
  - "CABLE Input"   — we WRITE here (treat as an *output* device in sounddevice terms)
  - "CABLE Output"  — apps like 腾讯会议 READ this as their microphone

This module only handles the CABLE Input side; the user must set
腾讯会议's microphone to CABLE Output manually (see README).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import sounddevice as sd

log = logging.getLogger(__name__)


class DeviceNotFoundError(RuntimeError):
    """Raised when no device matches the requested substring."""


@dataclass(frozen=True)
class AudioDevice:
    idx: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int

    def __str__(self) -> str:  # pragma: no cover — debug only
        return f"[{self.idx}] {self.name} ({self.hostapi})"


class AudioRouter:
    """Lists and resolves audio devices by name substring."""

    def list_input_devices(self) -> List[AudioDevice]:
        return self._list(max_ch_filter="in")

    def list_output_devices(self) -> List[AudioDevice]:
        return self._list(max_ch_filter="out")

    def _list(self, max_ch_filter: str) -> List[AudioDevice]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        out: List[AudioDevice] = []
        for i, d in enumerate(devices):
            hostapi_name = hostapis[d["hostapi"]]["name"]
            if max_ch_filter == "in" and d["max_input_channels"] <= 0:
                continue
            if max_ch_filter == "out" and d["max_output_channels"] <= 0:
                continue
            out.append(
                AudioDevice(
                    idx=i,
                    name=d["name"],
                    hostapi=hostapi_name,
                    max_input_channels=int(d["max_input_channels"]),
                    max_output_channels=int(d["max_output_channels"]),
                )
            )
        return out

    # --- matchers ---------------------------------------------------------

    def find_input(self, substring: str) -> AudioDevice:
        return self._find(self.list_input_devices(), substring, kind="input")

    def find_output(self, substring: str) -> AudioDevice:
        return self._find(self.list_output_devices(), substring, kind="output")

    def _find(self, devices: List[AudioDevice], substring: str, kind: str) -> AudioDevice:
        needle = substring.lower()
        matches = [d for d in devices if needle in d.name.lower()]
        if not matches:
            available = "\n  ".join(str(d) for d in devices) or "(none)"
            raise DeviceNotFoundError(
                f"No {kind} device matching '{substring}'.\nAvailable {kind}s:\n  {available}"
            )
        if len(matches) > 1:
            log.warning("Multiple %s devices match '%s': %s — using the first.", kind, substring, matches)
        return matches[0]

    # --- convenience ------------------------------------------------------

    def has_cable(self) -> bool:
        """True if VB-CABLE's CABLE Input (or Output) device is visible."""
        try:
            self.find_output("CABLE Input")
            return True
        except DeviceNotFoundError:
            return False

    @staticmethod
    def open_input_stream(
        device_idx: int,
        samplerate: int,
        blocksize: int,
        dtype: str,
        callback,
    ) -> sd.InputStream:
        return sd.InputStream(
            device=device_idx,
            channels=1,
            samplerate=samplerate,
            blocksize=blocksize,
            dtype=dtype,
            callback=callback,
        )

    @staticmethod
    def open_output_stream(
        device_idx: int,
        samplerate: int,
        blocksize: int,
        dtype: str,
        callback,
    ) -> sd.OutputStream:
        return sd.OutputStream(
            device=device_idx,
            channels=1,
            samplerate=samplerate,
            blocksize=blocksize,
            dtype=dtype,
            callback=callback,
        )

    @staticmethod
    def default_input_index() -> Optional[int]:
        try:
            return sd.default.device[0]
        except Exception:
            return None