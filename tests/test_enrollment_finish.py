"""Tests for EnrollmentWizard's finish-early / cancel / full-duration paths.

The wizard records in chunks from a sounddevice InputStream. We swap the
real stream for a fake that yields a fixed-amplitude sine wave, then drive
all three exit paths:

  * finish()    — user clicks "完成录制" early; recording stops at the next
                  chunk boundary past the 5s floor, and the embedding is
                  still extracted from whatever was captured.
  * cancel()    — user clicks "取消"; recording aborts and EnrollmentError
                  is raised (no embedding extracted).
  * full 20s    — nobody intervenes; recording runs to the configured
                  duration and then extracts.

A FakeEngine stands in for SpeakerEngine so no ONNX model is loaded.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from voicefilter.enrollment import EnrollmentError, EnrollmentRecorder, EnrollmentWizard


class _FakeStream:
    """Yields ``chunk_frames`` of sine wave per read(). Mimics sd.InputStream."""

    def __init__(self, sr: int, chunk_frames: int, amplitude: float = 0.05):
        self.sr = sr
        self.chunk_frames = chunk_frames
        self.amp = amplitude
        self._t = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        # Generate ``n`` mono float32 samples of sine wave at audible RMS.
        t = np.arange(self._t, self._t + n) / self.sr
        self._t += n
        data = (self.amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
        return data.reshape(-1, 1), False


class _FakeEngine:
    """Stand-in SpeakerEngine — records that extract_embedding was called."""

    EMBEDDING_DIM = 192
    threshold = 0.62

    def __init__(self):
        self.extracted_audio: np.ndarray | None = None
        self.saved = False
        self.loaded = False

    def extract_embedding(self, audio, sample_rate=16000):
        self.extracted_audio = np.asarray(audio)
        # Return a non-zero 192-d vector so the "all zeros" guard passes.
        return np.full(192, 0.1, dtype=np.float32)

    def save_enrollment(self, emb, path):
        self.saved = True

    def load_enrollment(self, path):
        self.loaded = True


def _make_wizard(sr: int = 16000, duration: int = 20) -> tuple[EnrollmentWizard, _FakeEngine]:
    engine = _FakeEngine()
    rec = EnrollmentRecorder(duration_sec=duration, sample_rate=sr)
    wizard = EnrollmentWizard(engine=engine, recorder=rec)  # type: ignore[arg-type]
    return wizard, engine


def _patch_stream(sr: int, chunk_frames: int):
    return patch("voicefilter.enrollment.sd.InputStream",
                 lambda **kw: _FakeStream(sr, chunk_frames))


def test_finish_early_stops_recording_and_extracts_embedding():
    """User clicks 完成录制 — recording stops past 5s and embedding runs."""
    sr = 16000
    chunk_frames = int(0.5 * sr)  # 0.5s chunks, matching the wizard
    wizard, engine = _make_wizard(sr=sr, duration=20)

    # After 6s of chunks (12 chunks × 0.5s), call finish(). That's past the
    # 5s floor, so the loop should break on the next iteration.
    chunk_count = {"n": 0}

    def on_progress(elapsed, chunk):
        chunk_count["n"] += 1
        if chunk_count["n"] == 12:  # ~6s elapsed
            wizard.finish()

    wizard.on_progress = on_progress
    with _patch_stream(sr, chunk_frames):
        wizard.run(mic_idx=0, save_path="/tmp/fake-emb.npy")

    assert engine.saved, "finish() should still extract + save the embedding"
    assert engine.loaded
    # Should have captured roughly 6s, not the full 20s.
    captured_sec = len(engine.extracted_audio) / sr
    assert 5.0 <= captured_sec < 10.0, f"captured {captured_sec:.1f}s, expected ~6s"


def test_cancel_aborts_without_extracting():
    """User clicks 取消 — EnrollmentError, no embedding extracted."""
    sr = 16000
    chunk_frames = int(0.5 * sr)
    wizard, engine = _make_wizard(sr=sr, duration=20)

    chunk_count = {"n": 0}

    def on_progress(elapsed, chunk):
        chunk_count["n"] += 1
        if chunk_count["n"] == 4:  # ~2s elapsed
            wizard.cancel()

    wizard.on_progress = on_progress
    with _patch_stream(sr, chunk_frames):
        with pytest.raises(EnrollmentError):
            wizard.run(mic_idx=0, save_path="/tmp/fake-emb.npy")

    assert not engine.saved, "cancel() must NOT extract or save the embedding"


def test_full_duration_extracts_when_nobody_intervenes():
    """Nobody clicks anything — recording runs the full duration."""
    sr = 16000
    chunk_frames = int(0.5 * sr)
    wizard, engine = _make_wizard(sr=sr, duration=5)  # shorter to keep test fast

    with _patch_stream(sr, chunk_frames):
        wizard.run(mic_idx=0, save_path="/tmp/fake-emb.npy")

    assert engine.saved
    captured_sec = len(engine.extracted_audio) / sr
    assert captured_sec == pytest.approx(5.0, abs=0.6)


def test_finish_below_5s_floor_is_ignored_until_floor_reached():
    """finish() before 5s must NOT truncate — too-short audio gives bad embeddings."""
    sr = 16000
    chunk_frames = int(0.5 * sr)
    wizard, engine = _make_wizard(sr=sr, duration=20)

    # Call finish() after just 2s (4 chunks). The wizard should ignore it
    # until the 5s floor is crossed; to keep the test bounded, also finish()
    # for real at 6s.
    chunk_count = {"n": 0}

    def on_progress(elapsed, chunk):
        chunk_count["n"] += 1
        if chunk_count["n"] in (4, 12):  # 2s (too early) + 6s (real finish)
            wizard.finish()

    wizard.on_progress = on_progress
    with _patch_stream(sr, chunk_frames):
        wizard.run(mic_idx=0, save_path="/tmp/fake-emb.npy")

    assert engine.saved
    captured_sec = len(engine.extracted_audio) / sr
    # Must NOT have stopped at 2s — should be ~6s.
    assert captured_sec >= 5.0, f"stopped too early at {captured_sec:.1f}s"
