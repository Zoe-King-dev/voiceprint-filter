"""Thread-safe fixed-size ring buffer for float32 audio frames.

Used to bridge the sounddevice input callback (real-time thread) and
the verification worker (worker thread). Push from the audio callback,
drain from any consumer.
"""
from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int, dtype=np.float32):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._cap = int(capacity)
        self._buf = np.empty(self._cap, dtype=dtype)
        self._write = 0
        self._filled = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._cap

    def __len__(self) -> int:
        with self._lock:
            return self._filled

    def extend(self, samples: np.ndarray) -> None:
        """Append samples; if they exceed capacity, oldest are overwritten."""
        n = len(samples)
        if n == 0:
            return
        if n >= self._cap:
            # Whole buffer is replaced — keep only the most recent `cap` samples
            with self._lock:
                np.copyto(self._buf, samples[-self._cap :].astype(self._buf.dtype, copy=False))
                self._write = 0
                self._filled = self._cap
            return
        with self._lock:
            end = self._write + n
            if end <= self._cap:
                self._buf[self._write : end] = samples
            else:
                first = self._cap - self._write
                self._buf[self._write :] = samples[:first]
                self._buf[: n - first] = samples[first:]
            self._write = (self._write + n) % self._cap
            self._filled = min(self._filled + n, self._cap)

    def snapshot(self) -> np.ndarray:
        """Return a contiguous copy of all buffered samples, in order."""
        with self._lock:
            if self._filled < self._cap:
                # Not yet wrapped; data lives in buf[:filled]
                return self._buf[: self._filled].copy()
            # Wrapped: oldest at write_pos, newest at write_pos-1
            return np.concatenate(
                (self._buf[self._write :], self._buf[: self._write])
            ).copy()

    def filled_ratio(self) -> float:
        with self._lock:
            return self._filled / self._cap

    def clear(self) -> None:
        with self._lock:
            self._write = 0
            self._filled = 0