"""Measure the filter pipeline's latency budget.

Two latencies matter for a real-time voice gate, and they are different:

  - **Audio-path latency** -- how long a sample takes from the input
    callback to the output callback. For this pipeline that is ~one block
    (frame_ms) plus PortAudio's device buffers; the output ring buffer is
    snapshot-based (always emits the most recent samples), so it does NOT
    accumulate delay. We measure the in-process push() cost instead -- the
    hard real-time budget: push() must finish well under one frame (30 ms)
    or the audio thread drops frames.

  - **Gain-reaction latency** -- how long after a *new speaker starts*
    before the gate actually attenuates them. This is governed by the
    sliding window: the window must slide the old speaker's audio out
    (window_sec) and the next hop must fire (hop_sec). So the budget is
    window_sec + hop_sec, plus the one-frame output lag. We measure it
    empirically with a content-aware fake engine: "is_me" is derived from
    the window's mean amplitude, so flipping the input from loud to quiet
    forces the gate to react, and we count frames until it does.

The script runs without sherpa-onnx / models (EnergyVAD fallback + a fake
engine). If sherpa and the speaker model are importable, it also reports
a real verify() inference time; otherwise it skips that line.

Usage:
    python scripts/measure_latency.py
    python scripts/measure_latency.py --window 1.0 --hop 0.5
"""
from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voicefilter.config import AppConfig  # noqa: E402
from voicefilter.filter_pipeline import FilterPipeline  # noqa: E402

log = logging.getLogger("measure_latency")


class FlagEngine:
    """Speaker stand-in: is_me is a test-toggled flag, audio stays loud.

    A real speaker switch is "a different LOUD person starts talking" --
    is_speech stays True (the EnergyVAD sees loudness), is_me flips. We
    model exactly that: the engine returns the current ``is_me`` flag
    regardless of window contents, while the test pushes loud audio so the
    VAD reads speech. This isolates the *pipeline's* reaction cadence
    (hop-gated decide + one-frame output lag) from the engine's own
    window-content robustness, which is a separate concern validated by
    real sherpa runs via stats.last_infer_ms.
    """

    EMBEDDING_DIM = 192

    def __init__(self, is_me: bool = True):
        self.is_me = is_me
        self.threshold = 0.62
        self.verify_calls = 0

    def is_enrolled(self) -> bool:
        return True

    def verify(self, audio, sample_rate: int = 16000):
        self.verify_calls += 1
        return self.is_me, (0.9 if self.is_me else 0.1)

    def set_threshold(self, t: float) -> None:
        self.threshold = float(t)


def _build_cfg(window_sec: float, hop_sec: float) -> AppConfig:
    cfg = AppConfig()
    cfg.audio.window_sec = window_sec
    cfg.audio.hop_sec = hop_sec
    return cfg


def measure_processing_latency(cfg: AppConfig, n_frames: int = 2000) -> tuple[float, float]:
    """Time push() per frame. Returns (mean_ms, p99_ms). Must be << frame_ms."""
    p = FilterPipeline(cfg, FlagEngine())
    frame = int(cfg.audio.frame_ms * cfg.audio.sample_rate / 1000)
    # Warm up: fill the window so decide() is on the hot path, not first-fill.
    warm = int(cfg.audio.window_sec * cfg.audio.sample_rate) + frame
    p.push(np.ones(warm, dtype=np.float32))
    times = []
    chunk = np.ones(frame, dtype=np.float32)
    for _ in range(n_frames):
        t0 = time.perf_counter()
        p.push(chunk)
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.mean(times), statistics.quantiles(times, n=100)[98]


def measure_gain_reaction(cfg: AppConfig) -> tuple[float, float, float]:
    """Measure cold-start detection and steady-state speaker-switch reaction.

    Returns (cold_start_ms, steady_reaction_ms, theoretical_steady_ms).

      cold_start_ms     -- frames from first push until the gate's FIRST
                          decide fires (window must fill). Budget ≈ window_sec.
      steady_reaction_ms -- after warm-up (gate open, window full & sliding),
                          flip is_me to False and count frames until the gate
                          attenuates (other_gain). Budget ≈ hop_sec + 1 frame.
    """
    sr = cfg.audio.sample_rate
    frame = int(cfg.audio.frame_ms * sr / 1000)
    loud = np.ones(frame, dtype=np.float32)

    # --- cold start: first decide fires once the window fills ---
    cold = FlagEngine(is_me=True)
    p = FilterPipeline(cfg, cold)
    initial_gain = p._latest_gain_db
    cold_frames = -1
    cap = int(cfg.audio.window_sec * sr / frame) * 2 + 5
    for i in range(cap):
        p.push(loud)
        if cold.verify_calls >= 1:  # first decide has run
            cold_frames = i + 1
            break

    # --- steady state: warm up open, then flip the speaker ---
    eng = FlagEngine(is_me=True)
    p = FilterPipeline(cfg, eng)
    warm_cap = int(cfg.audio.window_sec * sr / frame) * 2 + 5
    for _ in range(warm_cap):
        p.push(loud)
    if p._latest_gain_db != cfg.speaker.my_gain_db:
        # Gate never opened (unexpected); report what we have.
        steady_ms = float("nan")
    else:
        eng.is_me = False  # a different loud person starts talking
        reacted = -1
        react_cap = int(cfg.audio.hop_sec * sr / frame) * 3 + 5
        for i in range(react_cap):
            p.push(loud)
            if p._latest_gain_db <= cfg.speaker.other_gain_db + 0.5:
                reacted = i + 1
                break
        steady_ms = (reacted * cfg.audio.frame_ms) if reacted > 0 else float("nan")

    cold_ms = (cold_frames * cfg.audio.frame_ms) if cold_frames > 0 else float("nan")
    theoretical_steady = cfg.audio.hop_sec * 1000.0 + cfg.audio.frame_ms
    return cold_ms, steady_ms, theoretical_steady


def maybe_measure_real_inference(cfg: AppConfig) -> float | None:
    """If sherpa + speaker model are available, time one verify(); else None."""
    try:
        from voicefilter.speaker_engine import SpeakerEngine  # noqa: WPS433
        if not Path(cfg.speaker.model_path).exists():
            return None
        eng = SpeakerEngine(cfg.speaker.model_path, threshold=cfg.speaker.threshold)
        sr = cfg.audio.sample_rate
        audio = np.ones(sr, dtype=np.float32) * 0.1  # 1s of mild speech-level audio
        # Warm up (first call loads the model)
        eng.verify(audio, sample_rate=sr)
        t0 = time.perf_counter()
        eng.verify(audio, sample_rate=sr)
        return (time.perf_counter() - t0) * 1000.0
    except Exception as e:  # noqa: BLE001
        log.debug("real inference skipped: %s", e)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", type=float, default=1.0, help="window_sec (default 1.0)")
    ap.add_argument("--hop", type=float, default=0.5, help="hop_sec (default 0.5)")
    ap.add_argument("--frames", type=int, default=2000, help="frames to time for processing latency")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = _build_cfg(args.window, args.hop)
    sr = cfg.audio.sample_rate
    frame_ms = cfg.audio.frame_ms
    frame = int(frame_ms * sr / 1000)

    print("=" * 64)
    print("voiceprint-filter latency budget")
    print("=" * 64)
    print(f"config:   sample_rate={sr}  frame={frame_ms}ms ({frame} samples)")
    print(f"          window_sec={cfg.audio.window_sec}  hop_sec={cfg.audio.hop_sec}")
    print(f"          gains: my={cfg.speaker.my_gain_db}dB  other={cfg.speaker.other_gain_db}dB  "
          f"silence={cfg.speaker.no_speech_gain_db}dB")

    print("\n-- audio path (in-process push cost) --")
    mean_ms, p99_ms = measure_processing_latency(cfg, n_frames=args.frames)
    budget = frame_ms
    headroom = budget - p99_ms
    print(f"push() mean = {mean_ms:.3f} ms   p99 = {p99_ms:.3f} ms   "
          f"(budget {budget} ms/frame -> {headroom:.2f} ms headroom)")
    if p99_ms >= budget:
        print(f"  [!] p99 push() exceeds the frame budget -- audio thread may drop frames.")
    else:
        print(f"  [OK] within frame budget; real-time safe at the pipeline layer.")

    print("\n-- gain-reaction --")
    cold_ms, steady_ms, theoretical_steady = measure_gain_reaction(cfg)
    cold_theory = cfg.audio.window_sec * 1000.0
    print(f"cold-start (first detection): measured {cold_ms:.0f} ms  "
          f"(budget {cold_theory:.0f} ms = window_sec; the sliding window must fill once)")
    print(f"steady-state (mid-run speaker switch): measured {steady_ms:.0f} ms  "
          f"(budget {theoretical_steady:.0f} ms = hop_sec {cfg.audio.hop_sec*1000:.0f}ms + 1 frame)")
    print("  cold-start dominates the first ~1 s after launch; steady-state is the")
    print("  relevant number for an in-progress meeting (a new person starts talking).")
    if not np.isnan(steady_ms):
        ratio = steady_ms / theoretical_steady if theoretical_steady else 0
        print(f"  steady measured/budget = {ratio:.2f}x")

    print("\n-- speaker verify() inference (real model, if available) --")
    infer_ms = maybe_measure_real_inference(cfg)
    if infer_ms is None:
        print("skipped -- sherpa-onnx or speaker model not present (run scripts/download_models.py).")
        print("During live runs this is captured as stats.last_infer_ms and shown in the UI.")
    else:
        per_hop_budget = cfg.audio.hop_sec * 1000.0
        print(f"one verify() = {infer_ms:.1f} ms   (hop budget {per_hop_budget:.0f} ms)")
        if infer_ms >= per_hop_budget:
            print(f"  [!] inference slower than the hop interval -- will back up the audio thread.")
        else:
            print(f"  [OK] fits inside one hop; does not block the per-frame hot path.")

    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())