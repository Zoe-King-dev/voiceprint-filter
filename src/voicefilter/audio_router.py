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


# Names of VB-CABLE's virtual endpoints. VB-CABLE is a one-way virtual
# cable: "CABLE Input" is what we *write* to (filter pipeline output),
# "CABLE Output" is what apps like 腾讯会议 *read* from as their mic.
# Neither endpoint has a physical transducer behind it, so neither can
# be used as a recording source — opening an InputStream on them yields
# silence (or stale buffer residue). Hide them from the input list so
# the user doesn't pick them by mistake during enrollment.
_VB_CABLE_HINTS = ("cable input", "cable output")


def _is_vb_cable_endpoint(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _VB_CABLE_HINTS)


# Windows exposes the same physical device under several host APIs (MME,
# DirectSound, WASAPI, WDM-KS, ...). They are NOT equally usable for our
# 16 kHz mono blocking stream:
#   * MME / DirectSound — resample freely, blocking works. Most reliable.
#   * WASAPI            — exclusive-mode often rejects 16 kHz ("Invalid
#                         sample rate" PaErrorCode -9993) because it only
#                         accepts the device's native rates.
#   * WDM-KS            — kernel-streaming; many drivers reject the
#                         blocking API outright ("Blocking API not
#                         supported yet" PaErrorCode -9999).
# When several devices match a substring (e.g. a mic shows up under 3
# host APIs), prefer the one most likely to actually open. Lower rank =
# preferred. Unknown host APIs sort last but ahead of known-bad ones.
_HOSTAPI_PREFERENCE = {
    "MME": 0,
    "Windows DirectSound": 1,
    "Windows WASAPI": 2,
    "ASIO": 3,
    "Windows WDM-KS": 4,
}
_BAD_HOSTAPI_RANK = 9  # unknown host apis rank here (bad but not impossible)


def _hostapi_rank(hostapi: str) -> int:
    return _HOSTAPI_PREFERENCE.get(hostapi, _BAD_HOSTAPI_RANK)


class AudioRouter:
    """Lists and resolves audio devices by name substring."""

    def list_input_devices(self) -> List[AudioDevice]:
        return self._list(max_ch_filter="in", exclude_vb_cable=True)

    def list_output_devices(self) -> List[AudioDevice]:
        return self._list(max_ch_filter="out", exclude_vb_cable=False)

    def _list(self, max_ch_filter: str, exclude_vb_cable: bool = False) -> List[AudioDevice]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        out: List[AudioDevice] = []
        for i, d in enumerate(devices):
            hostapi_name = hostapis[d["hostapi"]]["name"]
            if max_ch_filter == "in" and d["max_input_channels"] <= 0:
                continue
            if max_ch_filter == "out" and d["max_output_channels"] <= 0:
                continue
            if exclude_vb_cable and _is_vb_cable_endpoint(d["name"]):
                # VB-CABLE's "Output" endpoint shows up as an input in
                # sounddevice (because meeting apps read it as a mic),
                # but it's a one-way virtual pipe with no real mic
                # behind it — recording from it returns silence.
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
        if _is_vb_cable_endpoint(substring):
            raise DeviceNotFoundError(
                f"'{substring}' is a VB-CABLE virtual endpoint and cannot be used as a "
                f"recording source (VB-CABLE has no physical microphone behind it). "
                f"Pick your real microphone (e.g. 'Microphone', 'Headset Mic') instead."
            )
        return self._find(self.list_input_devices(), substring, kind="input", direction="in")

    def find_output(self, substring: str) -> AudioDevice:
        return self._find(self.list_output_devices(), substring, kind="output", direction="out")

    def _find(
        self,
        devices: List[AudioDevice],
        substring: str,
        kind: str,
        direction: str,
    ) -> AudioDevice:
        needle = substring.lower()
        matches = [d for d in devices if needle in d.name.lower()]
        if not matches:
            available = "\n  ".join(str(d) for d in devices) or "(none)"
            raise DeviceNotFoundError(
                f"No {kind} device matching '{substring}'.\nAvailable {kind}s:\n  {available}"
            )

        if len(matches) == 1:
            # Even a single match can be unopenable on Windows (e.g. a
            # WDM-KS endpoint that throws "Blocking API not supported" or a
            # WASAPI endpoint that rejects 16 kHz). Probe it; if it opens,
            # great. If not, fall back to the OS default input/output device
            # (usually the user's chosen mic in Windows sound settings, which
            # is typically openable) before giving up with an error.
            sole = matches[0]
            if self._probe_open(sole.idx, direction):
                return sole
            fallback = self._default_device_fallback(direction)
            if fallback is not None:
                log.warning(
                    "Only %s match for '%s' is [%d] (%s) which won't open at 16 kHz; "
                    "falling back to OS default device [%d] (%s).",
                    kind, substring, sole.idx, sole.hostapi, fallback.idx, fallback.hostapi,
                )
                return fallback
            raise DeviceNotFoundError(
                f"The only {kind} matching '{substring}' is [{sole.idx}] {sole.name} "
                f"({sole.hostapi}), but it cannot be opened at 16 kHz mono — this "
                f"usually means the Windows host API doesn't support our audio "
                f"format (common with WDM-KS / WASAPI). Try a different substring "
                f"that matches the same device under MME or DirectSound, or pick "
                f"another microphone from the device list."
            )

        # Several devices match (typically the same physical device under
        # different host APIs). Sort by host-API preference (MME first, the
        # most forgiving for 16 kHz mono blocking streams) and then probe
        # each — return the first that actually opens. Without this we used
        # to blindly take matches[0], which on Windows often landed on a
        # WDM-KS / WASAPI endpoint that throws paInvalidDevice (-9996) or
        # "Blocking API not supported" (-9999) at open time.
        ranked = sorted(matches, key=lambda d: _hostapi_rank(d.hostapi))
        for d in ranked:
            if self._probe_open(d.idx, direction):
                log.info(
                    "Resolved %s '%s' to [%d] (%s) after probing %d candidates.",
                    kind, substring, d.idx, d.hostapi, len(matches),
                )
                return d
        # None of them opened. Fall back to the most-preferred one so the
        # caller still gets a device reference (and a real error message
        # when it tries to open it for real), rather than a confusing
        # DeviceNotFoundError that hides the real cause.
        log.warning(
            "All %d %s devices matching '%s' failed probe-open; using the most "
            "preferred (%s) and letting the real open raise the error.",
            len(matches), kind, substring, ranked[0].hostapi,
        )
        return ranked[0]

    @staticmethod
    def _probe_open(device_idx: int, direction: str) -> bool:
        """Try to open the device for an instant and immediately close it.

        Returns True if the open succeeded (device is usable at 16 kHz mono
        float32 with our default blocksize), False on any error. This is
        cheap — we don't start() the stream, just exercise the open path
        that's where Windows host-API incompatibilities surface.
        """
        try:
            if direction == "in":
                s = sd.InputStream(
                    device=device_idx, channels=1, samplerate=16000,
                    blocksize=480, dtype="float32",
                )
            else:
                s = sd.OutputStream(
                    device=device_idx, channels=1, samplerate=16000,
                    blocksize=480, dtype="float32",
                )
            s.close()
            return True
        except Exception as e:
            log.debug("probe-open failed for device %d (%s): %s", device_idx, direction, e)
            return False

    def _default_device_fallback(self, direction: str) -> Optional[AudioDevice]:
        """Resolve the OS-default input/output device and verify it opens.

        Used when the substring-matched device won't open (e.g. a WDM-KS mic
        that rejects the blocking API) — the OS default is what the user
        picked in Windows sound settings and is usually openable under MME.
        Returns None if there's no usable default so the caller can raise.
        """
        try:
            idx = sd.default.device[0] if direction == "in" else sd.default.device[1]
        except Exception:
            return None
        if idx is None or idx < 0:
            return None
        try:
            d = sd.query_devices(idx)
        except Exception:
            return None
        hostapis = sd.query_hostapis()
        dev = AudioDevice(
            idx=int(idx),
            name=d["name"],
            hostapi=hostapis[d["hostapi"]]["name"],
            max_input_channels=int(d["max_input_channels"]),
            max_output_channels=int(d["max_output_channels"]),
        )
        if not self._probe_open(dev.idx, direction):
            return None
        return dev

    # --- convenience ------------------------------------------------------

    def has_cable(self) -> bool:
        """True if VB-CABLE's CABLE Input (or Output) device is visible.

        Visibility-only check — does NOT probe-open. We don't want a flaky
        host-API probe to hide the fact that VB-CABLE is installed (the
        user just needs to reboot / pick a different host API), and this
        method drives the 'no VB-CABLE' warning banner in the UI.
        """
        needle = "cable input"
        try:
            return any(needle in d.name.lower() for d in self.list_output_devices())
        except Exception:
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