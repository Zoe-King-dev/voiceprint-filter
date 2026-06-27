"""Voiceprint enrollment wizard.

Records the user reading a fixed prompt for ~20 seconds, extracts a
single 192-d embedding via SpeakerEngine, and persists it to disk.

The recording is done with sounddevice's blocking rec() so this is also
safe to call from the GUI's dialog thread. Raw audio is NOT retained
on disk — only the embedding.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from .speaker_engine import SpeakerEngine

log = logging.getLogger(__name__)


PROMPT_TEXT = (
    "请用你日常说话的音量连续朗读以下内容。"
    "保持自然语速，距离麦克风约 20-40 厘米，环境尽量安静：\n\n"
    "今天天气不错，我正在测试声纹注册系统。\n"
    "一二三四五六七八九十。\n"
    "Hello world, this is a calibration test for voice biometrics.\n"
    "持续的、不间断的朗读，可以显著提高识别准确率。\n"
    "三二一，开始。"
)


class EnrollmentError(RuntimeError):
    pass


class EnrollmentRecorder:
    """Synchronous single-shot recording used by the wizard."""

    def __init__(self, duration_sec: int = 20, sample_rate: int = 16000):
        if duration_sec < 5:
            raise ValueError("enrollment duration must be at least 5 seconds")
        self.duration_sec = int(duration_sec)
        self.sample_rate = int(sample_rate)

    def record(self, device_idx: int) -> np.ndarray:
        frames = self.duration_sec * self.sample_rate
        log.info("Recording %ds from device %d ...", self.duration_sec, device_idx)
        rec = sd.rec(
            frames,
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            device=device_idx,
            blocking=True,
        )
        return rec.flatten()


class EnrollmentWizard:
    """High-level flow: prompt → record → extract → save.

    The GUI layer can subscribe to ``on_progress`` (called every ~0.5s
    during recording) and ``on_complete`` / ``on_error``.
    """

    def __init__(
        self,
        engine: SpeakerEngine,
        recorder: Optional[EnrollmentRecorder] = None,
        on_progress: Optional[Callable[[float, np.ndarray], None]] = None,
    ):
        self.engine = engine
        self.recorder = recorder or EnrollmentRecorder()
        self.on_progress = on_progress  # (elapsed_sec, latest_chunk) for VU meter
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self, mic_idx: int, save_path: str | Path) -> np.ndarray:
        """Record + extract + save. Returns the embedding."""
        self._cancel.clear()
        try:
            audio = self._record_with_progress(mic_idx)
        except Exception as e:
            raise EnrollmentError(f"Recording failed: {e}") from e

        if self._cancel.is_set():
            raise EnrollmentError("Enrollment cancelled by user.")

        # Sanity check: RMS should be non-trivial — if it's too quiet, warn.
        rms = float(np.sqrt(np.mean(np.square(audio))))
        if rms < 0.005:
            raise EnrollmentError(
                "Recording is too quiet (RMS < 0.005). "
                "Check your microphone or speak louder."
            )

        log.info("Extracting enrollment embedding ...")
        emb = self.engine.extract_embedding(audio, sample_rate=self.recorder.sample_rate)
        if not np.any(emb):
            raise EnrollmentError("Embedding extraction returned zeros — recording unusable.")

        self.engine.save_enrollment(emb, save_path)
        self.engine.load_enrollment(save_path)
        log.info("Enrollment saved to %s", save_path)
        return emb

    # --- internals --------------------------------------------------------

    def _record_with_progress(self, mic_idx: int) -> np.ndarray:
        """Record in chunks so the GUI can update a progress bar / VU meter."""
        sr = self.recorder.sample_rate
        chunk_sec = 0.5
        chunk_frames = int(chunk_sec * sr)
        total_frames = self.recorder.duration_sec * sr
        sr = self.recorder.sample_rate

        chunks: list[np.ndarray] = []
        captured = 0
        deadline = time.monotonic() + self.recorder.duration_sec + 0.5

        with sd.InputStream(
            device=mic_idx,
            channels=1,
            samplerate=sr,
            blocksize=chunk_frames,
            dtype="float32",
        ) as stream:
            while captured < total_frames and not self._cancel.is_set():
                if time.monotonic() > deadline:
                    raise EnrollmentError("Recording timed out.")
                data, _ = stream.read(chunk_frames)
                chunk = data.flatten()
                chunks.append(chunk)
                captured += len(chunk)
                if self.on_progress is not None:
                    try:
                        self.on_progress(captured / sr, chunk)
                    except Exception:  # don't let UI errors kill the recording
                        log.exception("on_progress callback raised")

        if self._cancel.is_set():
            raise EnrollmentError("Cancelled.")

        return np.concatenate(chunks)[:total_frames]