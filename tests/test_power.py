"""Tests for the numpy power/dB helpers (allocation-light, hot-path)."""
from __future__ import annotations

import math

import numpy as np

from voicefilter.utils.power import db_to_linear, linear_to_db, rms_db


def test_db_to_linear_round_trips():
    for db in (-30.0, -6.0, 0.0, 3.0):
        # db → linear → db should be identity within float tolerance
        back = linear_to_db(db_to_linear(db))
        assert math.isclose(back, db, abs_tol=1e-6), db


def test_db_to_linear_known_values():
    # -30 dB ≈ 1/31.6, -6 dB ≈ 1/2, 0 dB = 1
    assert math.isclose(db_to_linear(-30.0), 10.0 ** (-1.5), rel_tol=1e-9)
    assert math.isclose(db_to_linear(0.0), 1.0, rel_tol=1e-12)
    assert math.isclose(db_to_linear(20.0), 10.0, rel_tol=1e-12)


def test_linear_to_db_clamps_non_positive():
    assert linear_to_db(0.0) == -120.0
    assert linear_to_db(-1.0) == -120.0


def test_rms_db_full_scale_sine_is_near_zero():
    # A full-scale sine wave has RMS = 1/sqrt(2) ≈ -3.01 dBFS
    t = np.linspace(0, 1, 16000, endpoint=False, dtype=np.float64)
    sine = np.sin(2 * np.pi * 220 * t).astype(np.float32)
    assert math.isclose(rms_db(sine), 20 * math.log10(1 / math.sqrt(2)), abs_tol=0.05)


def test_rms_db_empty_is_floor():
    assert rms_db(np.array([], dtype=np.float32)) == -120.0


def test_rms_db_silence_is_floor():
    assert rms_db(np.zeros(1000, dtype=np.float32)) == -120.0