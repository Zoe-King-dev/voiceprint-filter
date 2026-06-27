"""Silero VAD wrapper via sherpa-onnx.

Detects whether a window of audio contains speech. We use the result
to avoid running the (relatively expensive) speaker verification on
pure silence — in silence windows we just apply no_speech_gain_db.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


_sherpa = None


def _get_sherpa():
    global _sherpa
    if _sherpa is None:
        import sherpa_onnx  # type: ignore

        _sherpa = sherpa_onnx
    return _sherpa


class SileroVAD:
    """Stateful VAD that processes one frame at a time."""

    def __init__(
        self,
        model_path: str | Path = "models/silero_vad.onnx",
        threshold: float = 0.5,
        sample_rate: int = 16000,
        min_speech_duration: float = 0.25,
        min_silence_duration: float = 0.10,
    ):
        self.model_path = Path(model_path)
        self.threshold = float(threshold)
        self.sample_rate = int(sample_rate)
        self._vad = None  # lazy
        self._last_speech = False
        self._min_speech = float(min_speech_duration)
        self._min_silence = float(min_silence_duration)

    def _ensure(self) -> None:
        if self._vad is not None:
            return
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"VAD model not found: {self.model_path}\n"
                f"Run `python scripts/download_models.py` to fetch it."
            )
        sherpa = _get_sherpa()
        cfg = sherpa.VadModelConfig()
        cfg.silero_vad.model = str(self.model_path)
        cfg.silero_vad.threshold = self.threshold
        cfg.silero_vad.min_speech_duration = self._min_speech
        cfg.silero_vad.min_silence_duration = self._min_silence
        cfg.sample_rate = self.sample_rate
        log.info("Loading Silero VAD: %s", self.model_path)
        self._vad = sherpa.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        log.info("Silero VAD ready.")

    def accept(self, frame: np.ndarray) -> None:
        """Feed one frame (~30 ms) to the VAD's internal buffer."""
        self._ensure()
        if frame.ndim != 1:
            frame = frame.flatten()
        self._vad.accept_waveform(frame.astype(np.float32, copy=False))

    def is_speech(self) -> bool:
        """Whether the VAD currently believes the buffer contains speech."""
        self._ensure()
        return bool(self._vad.is_speech_detected())

    def reset(self) -> None:
        if self._vad is not None:
            self._vad.reset()
        self._last_speech = False

    @property
    def last_speech(self) -> bool:
        return self._last_speech


class EnergyVAD:
    """Tiny CPU-only fallback used when the Silero model is unavailable.

    Not as accurate as Silero, but it never blocks startup and uses no
    external model file. Useful for first-launch sanity checking.
    """

    def __init__(self, threshold_db: float = -45.0, sample_rate: int = 16000):
        self.threshold_db = float(threshold_db)
        self.sample_rate = int(sample_rate)
        self._last = False

    def accept(self, frame: np.ndarray) -> None:
        from .utils.power import rms_db

        self._last = rms_db(frame) > self.threshold_db

    def is_speech(self) -> bool:
        return self._last

    def reset(self) -> None:
        self._last = False

    @property
    def last_speech(self) -> bool:
        return self._last


def make_vad(model_path: str | Path, threshold: float, sample_rate: int = 16000):
    """Factory: prefer SileroVAD, fall back to EnergyVAD if model is missing."""
    p = Path(model_path)
    if p.exists():
        try:
            return SileroVAD(model_path=p, threshold=threshold, sample_rate=sample_rate)
        except Exception as e:  # pragma: no cover
            log.warning("SileroVAD init failed (%s); falling back to energy VAD.", e)
    log.warning("VAD model missing at %s — using energy VAD fallback.", p)
    return EnergyVAD(sample_rate=sample_rate)