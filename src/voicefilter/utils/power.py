"""Audio power / dB helpers."""
from __future__ import annotations

import numpy as np


def linear_to_db(x: float) -> float:
    if x <= 0:
        return -120.0
    return 20.0 * np.log10(x)


def db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def rms_db(samples: np.ndarray) -> float:
    """Root-mean-square level in dBFS (relative to full-scale 1.0)."""
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    return linear_to_db(rms)