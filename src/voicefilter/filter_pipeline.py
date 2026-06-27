"""Real-time voiceprint filter pipeline.

Data flow per audio frame (typically 30 ms):

  input frame
    → push to sliding window (size = window_sec)
    → every hop_sec, run VAD + SpeakerEngine.verify() on the window
    → compute gain_db (me / other / silence)
    → return output frame = input * 10^(gain_db/20)

The pipeline is single-threaded and synchronous — it is driven by the
sounddevice input callback on the audio thread. Hot path is allocation-
free: ``latest_gain_db`` is a precomputed float, and verification is
gated by a hop counter.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .config import AppConfig
from .speaker_engine import SpeakerEngine
from .utils.power import db_to_linear
from .utils.ringbuffer import RingBuffer
from .vad import EnergyVAD, SileroVAD, make_vad

log = logging.getLogger(__name__)


@dataclass
class FilterStats:
    last_score: float = 0.0
    last_is_me: bool = False
    last_is_speech: bool = False
    last_gain_db: float = -6.0
    last_window_sec: float = 0.0
    last_infer_ms: float = 0.0
    frames_processed: int = 0
    accepted_frames: int = 0
    rejected_frames: int = 0


class FilterPipeline:
    """Stateful processor. One instance per running filter session."""

    def __init__(
        self,
        cfg: AppConfig,
        engine: SpeakerEngine,
        on_update: Optional[Callable[[FilterStats], None]] = None,
    ):
        self.cfg = cfg
        self.engine = engine
        self.on_update = on_update
        self._lock = threading.Lock()

        sr = cfg.audio.sample_rate
        self.window = RingBuffer(int(cfg.audio.window_sec * sr))
        self.hop_frames = max(1, int(cfg.audio.hop_sec * sr))
        self._frames_since_verify = 0

        self.vad = make_vad(
            "models/silero_vad.onnx",
            threshold=cfg.vad.threshold,
            sample_rate=sr,
        )
        # If the VAD is disabled at config level, replace with a permissive energy VAD
        if not cfg.vad.enabled:
            self.vad = EnergyVAD(threshold_db=-90.0, sample_rate=sr)

        self._latest_gain_db = cfg.speaker.no_speech_gain_db
        self._stats = FilterStats(last_gain_db=self._latest_gain_db)
        self._paused = False

    # --- control ----------------------------------------------------------

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        log.info("Pipeline paused.")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        log.info("Pipeline resumed.")

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def set_threshold(self, threshold: float) -> None:
        self.engine.set_threshold(threshold)

    def set_other_gain_db(self, db: float) -> None:
        with self._lock:
            self.cfg.speaker.other_gain_db = float(db)

    def stats(self) -> FilterStats:
        with self._lock:
            return FilterStats(**vars(self._stats))

    # --- hot path ---------------------------------------------------------

    def push(self, frame: np.ndarray) -> np.ndarray:
        """Process one input frame. Returns the (gained) output frame."""
        if frame.ndim != 1:
            frame = frame.flatten()
        frame = frame.astype(np.float32, copy=False)

        with self._lock:
            paused = self._paused
            gain_db = self._latest_gain_db

        if paused:
            # Bypass: pass through unchanged, no verification work
            with self._lock:
                self._stats.frames_processed += 1
            return frame

        # 1) accumulate into sliding window
        self.window.extend(frame)
        self._frames_since_verify += len(frame)
        self.vad.accept(frame)

        # 2) hop-triggered decision
        if self._frames_since_verify >= self.hop_frames and self.window.filled_ratio() >= 1.0:
            self._frames_since_verify = 0
            self._decide()

        # 3) apply current gain
        out = frame * db_to_linear(gain_db)
        with self._lock:
            self._stats.frames_processed += 1
            if gain_db > self.cfg.speaker.other_gain_db + 0.5:
                self._stats.accepted_frames += 1
            elif gain_db < self.cfg.speaker.my_gain_db - 0.5:
                self._stats.rejected_frames += 1
        return out

    # --- internals --------------------------------------------------------

    def _decide(self) -> None:
        if not self.engine.is_enrolled():
            # Without an enrollment, we can't tell self from others; pass through.
            gain_db = self.cfg.speaker.my_gain_db
            is_speech = True
            score = 0.0
            is_me = True
        else:
            audio = self.window.snapshot()
            is_speech = self.vad.is_speech()
            t0 = time.perf_counter()
            is_me, score = self.engine.verify(audio, sample_rate=self.cfg.audio.sample_rate)
            infer_ms = (time.perf_counter() - t0) * 1000.0

            with self._lock:
                self._stats.last_score = score
                self._stats.last_is_me = is_me
                self._stats.last_is_speech = is_speech
                self._stats.last_infer_ms = infer_ms
                self._stats.last_window_sec = len(audio) / self.cfg.audio.sample_rate

            if not is_speech:
                gain_db = self.cfg.speaker.no_speech_gain_db
            elif is_me:
                gain_db = self.cfg.speaker.my_gain_db
            else:
                gain_db = self.cfg.speaker.other_gain_db

        with self._lock:
            self._latest_gain_db = gain_db
            self._stats.last_gain_db = gain_db

        if self.on_update is not None:
            try:
                self.on_update(self.stats())
            except Exception:  # never let UI errors affect audio
                log.exception("on_update callback raised")