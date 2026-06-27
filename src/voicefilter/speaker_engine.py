"""Speaker embedding extraction & verification backed by sherpa-onnx 3D-Speaker.

Model: 3dspeaker_speech_campplus_sv_zh-cn_16k-common (192-d embedding).
Both the enrollment path and the streaming path call ``compute()`` and
share the same normalization & cosine-similarity math.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

# Lazy import — sherpa-onnx is heavy; we don't want a top-level import to
# block GUI startup if the model isn't downloaded yet.
_sherpa_onnx = None


def _sherpa():
    global _sherpa_onnx
    if _sherpa_onnx is None:
        import sherpa_onnx  # type: ignore

        _sherpa_onnx = sherpa_onnx
    return _sherpa_onnx


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < 1e-9:
        return v
    return v / n


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1]. Inputs are L2-normalized internally."""
    a_n = _normalize(a.astype(np.float64, copy=False))
    b_n = _normalize(b.astype(np.float64, copy=False))
    return float(np.dot(a_n, b_n))


class SpeakerEngine:
    """Wrap sherpa_onnx.SpeakerEmbeddingExtractor with a clean Pythonic API."""

    EMBEDDING_DIM = 192  # 3D-Speaker campplus zh-cn 16k-common

    def __init__(self, model_path: str | Path, threshold: float = 0.62, num_threads: int = 2):
        self.model_path = Path(model_path)
        self.threshold = float(threshold)
        self.num_threads = int(num_threads)
        self._extractor = None  # lazy
        self._my_emb: Optional[np.ndarray] = None

    # --- lifecycle --------------------------------------------------------

    def _ensure_extractor(self):
        if self._extractor is not None:
            return
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Speaker model not found: {self.model_path}\n"
                f"Run `python scripts/download_models.py` to fetch it."
            )
        sherpa = _sherpa()
        log.info("Loading speaker model: %s", self.model_path)
        self._extractor = sherpa.SpeakerEmbeddingExtractor(
            model=str(self.model_path),
            num_threads=self.num_threads,
        )
        log.info("Speaker model loaded.")

    def is_enrolled(self) -> bool:
        return self._my_emb is not None

    def load_enrollment(self, path: str | Path) -> None:
        emb = np.load(path)
        if emb.ndim != 1 or emb.shape[0] != self.EMBEDDING_DIM:
            raise ValueError(
                f"Enrollment embedding must be 1-D of length {self.EMBEDDING_DIM}, "
                f"got shape {emb.shape}"
            )
        self._my_emb = _normalize(emb.astype(np.float32))
        log.info("Loaded enrollment embedding from %s", path)

    def save_enrollment(self, emb: np.ndarray, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        np.save(path, emb.astype(np.float32))

    # --- core API ---------------------------------------------------------

    def extract_embedding(self, audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
        """Compute a 192-d L2-normalized speaker embedding for the given mono audio."""
        self._ensure_extractor()
        if audio.ndim != 1:
            raise ValueError(f"audio must be 1-D, got shape {audio.shape}")
        if audio.size < int(sample_rate * 0.3):
            # < 300ms of audio is too short to be meaningful; return zeros
            return np.zeros(self.EMBEDDING_DIM, dtype=np.float32)
        samples = audio.astype(np.float32, copy=False)
        sherpa = _sherpa()
        emb = self._extractor.compute(samples, sample_rate=sample_rate)
        # sherpa returns a numpy array; normalize so cosine = dot product
        return _normalize(np.asarray(emb, dtype=np.float32))

    def verify(self, audio: np.ndarray, sample_rate: int = 16000) -> Tuple[bool, float]:
        """Return (is_me, similarity). is_me is True if similarity >= threshold."""
        if self._my_emb is None:
            return False, 0.0
        emb = self.extract_embedding(audio, sample_rate=sample_rate)
        if not np.any(emb):
            return False, 0.0
        score = float(np.dot(emb, self._my_emb))  # both already L2-normalized
        return score >= self.threshold, score

    def set_threshold(self, threshold: float) -> None:
        if not 0.3 <= threshold <= 0.95:
            raise ValueError("threshold must be in [0.3, 0.95]")
        self.threshold = float(threshold)