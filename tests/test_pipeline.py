"""Tests for FilterPipeline's hop-gated decision logic.

The pipeline's heavy deps (sherpa-onnx, sounddevice, PyQt6) are all lazy,
so importing the module needs none of them. We drive the decision path
with a fake engine and the EnergyVAD fallback (no model file → make_vad
falls back to EnergyVAD, which is pure numpy). This exercises the real
_decide() gain-selection logic without any ONNX inference.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from voicefilter.config import AppConfig
from voicefilter.filter_pipeline import FilterPipeline, FilterStats


class FakeEngine:
    """Stand-in for SpeakerEngine with scriptable verify() output."""

    EMBEDDING_DIM = 192

    def __init__(self, *, enrolled=True, is_me=True, score=0.8):
        self._enrolled = enrolled
        self._is_me = is_me
        self._score = score
        self.threshold = 0.62
        self.verify_calls = 0

    def is_enrolled(self) -> bool:
        return self._enrolled

    def verify(self, audio, sample_rate=16000):
        self.verify_calls += 1
        return self._is_me, self._score

    def set_threshold(self, t: float) -> None:
        self.threshold = float(t)


def _cfg() -> AppConfig:
    cfg = AppConfig()
    # Force the EnergyVAD fallback so tests are deterministic regardless of
    # whether the real Silero model happens to be downloaded. A steady DC
    # ones-signal reads as speech under EnergyVAD (rms > threshold), which
    # is what the gain-selection assertions rely on. The real SileroVAD has
    # internal buffer state and doesn't treat a DC signal as speech.
    cfg.vad.model_path = Path("does-not-exist-vad.onnx")
    # Shrink window/hop so tests push few samples but trigger _decide().
    cfg.audio.window_sec = 0.1  # 1600 samples @ 16k
    cfg.audio.hop_sec = 0.05    # 800 samples
    cfg.speaker.my_gain_db = 0.0
    cfg.speaker.other_gain_db = -30.0
    cfg.speaker.no_speech_gain_db = -6.0
    return cfg


def _push_speech(p: FilterPipeline, n: int = 1600) -> np.ndarray:
    """Push a full-scale (speech-loud) window; triggers one _decide()."""
    frame = np.ones(n, dtype=np.float32)
    return p.push(frame)


def _push_silence(p: FilterPipeline, n: int = 1600) -> np.ndarray:
    frame = np.zeros(n, dtype=np.float32)
    return p.push(frame)


def test_enrolled_me_speech_keeps_full_gain():
    p = FilterPipeline(_cfg(), FakeEngine(is_me=True, score=0.8))
    _push_speech(p)
    assert p._latest_gain_db == p.cfg.speaker.my_gain_db


def test_enrolled_other_speech_attenuates():
    p = FilterPipeline(_cfg(), FakeEngine(is_me=False, score=0.2))
    _push_speech(p)
    assert p._latest_gain_db == p.cfg.speaker.other_gain_db
    assert p._latest_gain_db == -30.0


def test_silence_uses_no_speech_gain():
    p = FilterPipeline(_cfg(), FakeEngine(is_me=True, score=0.9))
    _push_silence(p)
    assert p._latest_gain_db == p.cfg.speaker.no_speech_gain_db
    assert p._latest_gain_db == -6.0


def test_not_enrolled_passes_through_full_gain():
    # No enrollment → cannot distinguish self from others; pass through.
    engine = FakeEngine(enrolled=False)
    p = FilterPipeline(_cfg(), engine)
    _push_speech(p)
    assert p._latest_gain_db == p.cfg.speaker.my_gain_db
    assert engine.verify_calls == 0  # verify is never called without enrollment


def test_paused_bypasses_processing_unchanged():
    p = FilterPipeline(_cfg(), FakeEngine())
    p.pause()
    frame = np.arange(1600, dtype=np.float32) * 0.001
    out = p.push(frame)
    np.testing.assert_array_equal(out, frame)
    # No verification happened while paused.
    assert p.engine.verify_calls == 0


def test_resume_restores_processing():
    p = FilterPipeline(_cfg(), FakeEngine(is_me=False, score=0.2))
    p.pause()
    p.resume()
    assert p.is_paused() is False
    _push_speech(p)
    assert p._latest_gain_db == p.cfg.speaker.other_gain_db


def test_decision_runs_once_per_hop_not_per_frame():
    # 1600-sample window, 800-sample hop. Push two halves; only the first
    # (which fills the window past the hop) should call verify at fill time,
    # but after fill every >=800 samples triggers another decide.
    cfg = _cfg()
    p = FilterPipeline(cfg, FakeEngine(is_me=True, score=0.8))
    # Fill window in two chunks of 800 each: after 1st chunk window not full
    # (800 < 1600), so no decide yet. After 2nd chunk (1600 >= 1600 full AND
    # frames_since_verify 1600 >= hop 800) → one decide.
    p.push(np.ones(800, dtype=np.float32))
    assert p.engine.verify_calls == 0
    p.push(np.ones(800, dtype=np.float32))
    assert p.engine.verify_calls == 1
    # Next 800 samples crosses the hop threshold again → another decide.
    p.push(np.ones(800, dtype=np.float32))
    assert p.engine.verify_calls == 2


def test_on_update_callback_receives_stats():
    received = []
    p = FilterPipeline(_cfg(), FakeEngine(is_me=True, score=0.75), on_update=received.append)
    _push_speech(p)
    assert len(received) == 1
    s = received[0]
    assert isinstance(s, FilterStats)
    assert math.isclose(s.last_score, 0.75)
    assert s.last_is_me is True
    assert s.last_gain_db == p.cfg.speaker.my_gain_db  # me + speech → full gain


def test_output_frame_lags_decision_by_one_push():
    """The hot path applies the *precomputed* gain, so the gain decided on
    push N only reaches the output on push N+1. This is by design —
    verification is hop-gated, gain is a precomputed float on the audio
    thread. Assert that ordering, not a same-frame coupling that doesn't exist.
    """
    p = FilterPipeline(_cfg(), FakeEngine(is_me=False, score=0.1))
    first = _push_speech(p)   # decides → other_gain (-30), but output uses prior gain (-6)
    initial_gain = p.cfg.speaker.no_speech_gain_db  # the constructor's starting gain
    np.testing.assert_allclose(first, np.ones(1600) * 10 ** (initial_gain / 20), rtol=1e-6)
    # Second push now sees the freshly-decided -30 dB gain.
    second = _push_speech(p)
    np.testing.assert_allclose(second, np.ones(1600) * 10 ** (-30.0 / 20), rtol=1e-6)


def test_output_frame_is_input_scaled_by_gain():
    """After a decision has set other_gain, a subsequent frame is scaled by it."""
    p = FilterPipeline(_cfg(), FakeEngine(is_me=False, score=0.1))
    _push_speech(p)          # triggers decision → _latest_gain_db = -30
    out = _push_speech(p)    # this frame applies the -30 dB gain
    expected = 10.0 ** (-30.0 / 20.0)
    np.testing.assert_allclose(out, np.ones(1600) * expected, rtol=1e-6)


def test_set_threshold_propagates_to_engine():
    p = FilterPipeline(_cfg(), FakeEngine())
    p.set_threshold(0.70)
    assert p.engine.threshold == 0.70


def test_set_other_gain_updates_cfg():
    p = FilterPipeline(_cfg(), FakeEngine())
    p.set_other_gain_db(-45.0)
    assert p.cfg.speaker.other_gain_db == -45.0