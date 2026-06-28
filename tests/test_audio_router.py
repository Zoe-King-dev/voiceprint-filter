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
    with patch("sounddevice.query_devices", return_value=_FAKE_DEVICES), \
         patch("sounddevice.query_hostapis", return_value=_FAKE_HOSTAPIS):
        dev = AudioRouter().find_input("Real Microphone")
    assert dev.name == "Real Microphone"
    assert dev.max_input_channels == 2
