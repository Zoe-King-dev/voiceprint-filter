"""Tests for AudioRouter: VB-CABLE endpoints must not appear as input candidates.

VB-CABLE is a one-way virtual cable. Its "CABLE Output" endpoint shows up in
sounddevice as an *input* device (because meeting apps read it as their mic),
but it has no physical transducer behind it -- opening an InputStream on it
returns silence / stale buffer residue. Recording enrollment audio from it
triggers RMS-too-low errors that look like "speak louder" but are actually
"wrong device entirely". Hide it from the input list and reject lookups that
target it explicitly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from voicefilter.audio_router import (
    AudioRouter,
    DeviceNotFoundError,
)


# Two devices with VB-CABLE-shaped names but inverted channel counts --
# mirrors how VB-Audio Virtual Cable actually registers on Windows:
#   * "CABLE Input"  -> max_input=0,  max_output=2  (we WRITE to it)
#   * "CABLE Output" -> max_input=2,  max_output=0  (apps READ it as a mic)
_FAKE_DEVICES = [
    {"name": "Real Microphone",   "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "CABLE Output (VB-Audio Virtual Cable)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers",          "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
]
_FAKE_HOSTAPIS = [{"name": "MME"}]


def _patched_query():
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS):
        yield


def test_list_input_devices_excludes_vb_cable_endpoints():
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS):
        names = [d.name for d in AudioRouter().list_input_devices()]
    # Only the real mic should be visible to enrollment. CABLE Output must
    # be hidden even though it advertises 2 input channels.
    assert "Real Microphone" in names
    assert not any("CABLE" in n for n in names), \
        f"VB-CABLE endpoints leaked into input list: {names}"


def test_list_output_devices_includes_vb_cable_input():
    # Output list is where CABLE Input SHOULD appear -- that's where we write.
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS):
        names = [d.name for d in AudioRouter().list_output_devices()]
    assert any("CABLE Input" in n for n in names)
    assert "Speakers" in names


def test_find_input_rejects_explicit_vb_cable_substring():
    # If someone has "CABLE Output" in their config file as the input
    # device substring, we want a clear error -- not a silent miss later.
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS):
        with pytest.raises(DeviceNotFoundError) as exc_info:
            AudioRouter().find_input("CABLE Output")
    msg = str(exc_info.value).lower()
    assert "vb-cable" in msg or "virtual" in msg
    assert "physical microphone" in msg or "real microphone" in msg


def test_find_input_accepts_real_mic_substring():
    # Single-match path now also probe-opens, so stub InputStream to succeed.
    class _OkStream:
        def __init__(self, device=None, **kw):
            pass
        def close(self):
            pass
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS), \
         patch("sounddevice.InputStream", _OkStream):
        dev = AudioRouter().find_input("Real Microphone")
    assert dev.name == "Real Microphone"
    assert dev.max_input_channels == 2


def test_find_input_single_match_that_cannot_open_raises_clear_error():
    """A single matching device that fails probe-open, with NO usable OS
    default fallback, must raise a DeviceNotFoundError with an actionable
    message — not silently return a device that will crash InputStream
    later with PaErrorCode -9996."""
    class _FailStream:
        def __init__(self, device=None, **kw):
            raise RuntimeError("Error opening InputStream: Invalid device [PaErrorCode -9996]")
        def close(self):
            pass
    # Force the OS-default fallback to be unusable so we exercise the
    # raise path. default.device = (-1, -1) means "no default" → fallback
    # returns None → DeviceNotFoundError is raised.
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS), \
         patch("sounddevice.InputStream", _FailStream), \
         patch("sounddevice.default.device", (-1, -1)):
        with pytest.raises(DeviceNotFoundError) as exc_info:
            AudioRouter().find_input("Real Microphone")
    msg = str(exc_info.value)
    assert "cannot be opened" in msg
    assert "Real Microphone" in msg


def test_find_input_falls_back_to_os_default_when_match_wont_open():
    """If the only substring match won't open but the OS default device
    does, use the default instead of erroring — this is the recovery path
    for the user's 'Microphone' substring landing on a broken WDM-KS
    endpoint while their real mic is the Windows default."""
    class _FailStream:
        # The substring match (idx 0 in _FAKE_DEVICES) fails to open...
        _FAIL_IDX = {0}
        def __init__(self, device=None, **kw):
            if device in self._FAIL_IDX:
                raise RuntimeError("Blocking API not supported")
        def close(self):
            pass
    # ...but the OS default input (idx 2 = CABLE Output in _FAKE_DEVICES,
    # which has input channels) opens fine. Stub query_devices(idx) to
    # return that single device dict, and default.device = (input, output)
    # to point the INPUT default at idx 2.
    fake_default_dev = _FAKE_DEVICES[2]
    with patch("sounddevice.query_devices",
               side_effect=lambda idx=None: _FAKE_DEVICES if idx is None else fake_default_dev), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS), \
         patch("sounddevice.InputStream", _FailStream), \
         patch("sounddevice.default.device", (2, -1)):
        dev = AudioRouter().find_input("Real Microphone")
    assert dev.idx == 2, f"expected fallback to OS default input idx 2, got {dev}"


# --- host-API preference + probe-open tests -----------------------------
#
# Windows registers the same physical mic under several host APIs (MME,
# DirectSound, WASAPI, WDM-KS). They are not equally usable for our 16 kHz
# mono blocking stream — WDM-KS often throws "Blocking API not supported"
# and WASAPI rejects 16 kHz as "Invalid sample rate". When several devices
# match a substring, find_input must prefer one that actually opens rather
# than blindly taking the first enumeration order (which is how users hit
# PaErrorCode -9996 paInvalidDevice on 启动过滤).

# Same mic name under two host APIs. In the fake list below, idx 1 is MME
# (opens fine) and idx 2 is WDM-KS (broken — blocking API not supported).
# We list WDM-KS with a LATER index here, but the probe-open + host-API
# preference logic must still pick MME regardless of enumeration order.
_MULTI_HOSTAPI_DEVICES = [
    # idx 0 — unrelated, filtered out (no input channels)
    {"name": "Speakers",                 "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    # idx 1 — MME, works
    {"name": "Microphone (AMD Audio)",   "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    # idx 2 — WDM-KS, broken (blocking API not supported)
    {"name": "Microphone (AMD Audio)",   "hostapi": 4, "max_input_channels": 2, "max_output_channels": 0},
]
_MULTI_HOSTAPIS = [
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "ASIO"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
]


class _FakeInputStream:
    """Fakes sd.InputStream: raising on the WDM-KS device idx, OK elsewhere."""

    # idx 2 = the WDM-KS mic in _MULTI_HOSTAPI_DEVICES; opening it throws.
    _FAIL_IDX = {2}

    def __init__(self, device=None, **kw):
        if device in self._FAIL_IDX:
            raise RuntimeError("Error opening InputStream: Invalid device [PaErrorCode -9996]")

    def close(self):
        pass


def test_find_input_prefers_hostapi_that_actually_opens():
    """Two mics match 'Microphone' — must pick the MME one (idx 1) that
    opens, not the WDM-KS one (idx 2) that throws."""
    with patch("sounddevice.query_devices", return_value=_MULTI_HOSTAPI_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_MULTI_HOSTAPIS), \
         patch("sounddevice.InputStream", _FakeInputStream):
        dev = AudioRouter().find_input("Microphone")
    assert dev.idx == 1, f"should pick the openable MME device, got {dev}"
    assert dev.hostapi == "MME"


def test_find_input_falls_back_to_most_preferred_when_all_probes_fail():
    """If NO matching device opens, don't raise DeviceNotFoundError (which
    hides the real cause) — return the most-preferred one so the real open
    later surfaces a meaningful error."""
    _FakeInputStream._FAIL_IDX = {1, 2}  # both fail now
    try:
        with patch("sounddevice.query_devices", return_value=_MULTI_HOSTAPI_DEVICES), \
             patch("sounddevice.query_hostapis", return_value=_MULTI_HOSTAPIS), \
             patch("sounddevice.InputStream", _FakeInputStream):
            dev = AudioRouter().find_input("Microphone")
        assert dev.hostapi == "MME"  # most-preferred fallback
    finally:
        _FakeInputStream._FAIL_IDX = {2}  # restore for other tests


def test_has_cable_is_visibility_only_and_ignores_probe_failures():
    """has_cable must return True if CABLE Input is visible, even if a
    probe-open would fail — it drives the 'no VB-CABLE' warning banner
    and must not hide an installed-but-flaky cable."""
    # CABLE Input present under WDM-KS only, and make ALL outputs fail probe.
    devices = [
        {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 4,
         "max_input_channels": 0, "max_output_channels": 2},
        {"name": "Speakers", "hostapi": 0,
         "max_input_channels": 0, "max_output_channels": 2},
    ]
    _FakeInputStream._FAIL_IDX = {0, 1}
    try:
        with patch("sounddevice.query_devices", return_value=devices), \
             patch("sounddevice.query_hostapis", return_value=_MULTI_HOSTAPIS), \
             patch("sounddevice.OutputStream", _FakeInputStream):
            assert AudioRouter().has_cable() is True
    finally:
        _FakeInputStream._FAIL_IDX = {2}
