"""Tests for the thread-safe RingBuffer (sliding window + output cushion)."""
from __future__ import annotations

import numpy as np

from voicefilter.utils.ringbuffer import RingBuffer


def test_extend_below_capacity_snapshot_in_order():
    rb = RingBuffer(100)
    rb.extend(np.arange(10, dtype=np.float32))
    snap = rb.snapshot()
    assert len(rb) == 10
    np.testing.assert_array_equal(snap, np.arange(10))


def test_fill_exactly_capacity():
    rb = RingBuffer(8)
    rb.extend(np.arange(8, dtype=np.float32))
    assert len(rb) == 8
    np.testing.assert_array_equal(rb.snapshot(), np.arange(8))


def test_extend_beyond_capacity_keeps_most_recent():
    rb = RingBuffer(8)
    rb.extend(np.arange(20, dtype=np.float32))
    assert len(rb) == 8
    np.testing.assert_array_equal(rb.snapshot(), np.arange(12, 20))


def test_wrap_around_preserves_order():
    rb = RingBuffer(10)
    rb.extend(np.arange(6, dtype=np.float32))   # [0..5], filled=6
    rb.extend(np.arange(6, 9, dtype=np.float32)) # [0..8], filled=9
    rb.extend(np.arange(9, 13, dtype=np.float32))  # wraps; oldest dropped
    # capacity 10, pushed 13 total → holds [3..12]
    np.testing.assert_array_equal(rb.snapshot(), np.arange(3, 13))


def test_multiple_small_extends_match_one_large_extend():
    cap = 50
    a = RingBuffer(cap)
    b = RingBuffer(cap)
    data = np.arange(80, dtype=np.float32)
    a.extend(data)
    for i in range(0, 80, 7):
        b.extend(data[i : i + 7])
    np.testing.assert_array_equal(a.snapshot(), b.snapshot())
    assert len(a) == len(b) == cap


def test_filled_ratio():
    rb = RingBuffer(10)
    assert rb.filled_ratio() == 0.0
    rb.extend(np.ones(5, dtype=np.float32))
    assert rb.filled_ratio() == 0.5
    rb.extend(np.ones(5, dtype=np.float32))
    assert rb.filled_ratio() == 1.0
    rb.extend(np.ones(5, dtype=np.float32))  # overflow, stays full
    assert rb.filled_ratio() == 1.0


def test_clear_resets_but_capacity_unchanged():
    rb = RingBuffer(16)
    rb.extend(np.ones(16, dtype=np.float32))
    rb.clear()
    assert len(rb) == 0
    assert rb.filled_ratio() == 0.0
    assert rb.capacity == 16
    # Usable again after clear
    rb.extend(np.arange(4, dtype=np.float32))
    np.testing.assert_array_equal(rb.snapshot(), np.arange(4))


def test_empty_extend_is_noop():
    rb = RingBuffer(8)
    rb.extend(np.array([], dtype=np.float32))
    assert len(rb) == 0


def test_non_positive_capacity_rejected():
    import pytest

    with pytest.raises(ValueError):
        RingBuffer(0)
    with pytest.raises(ValueError):
        RingBuffer(-3)