"""End-to-end self-test for the speaker engine.

Usage:
    python scripts/verify_pipeline.py path/to/me.wav path/to/other.wav
    python scripts/verify_pipeline.py --record   # records two 10s clips from default mic

The script prints cosine scores and the accept/reject decision. Use it
to sanity-check the model before relying on the live filter.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import wave  # stdlib -- avoids pulling soundfile just to read .wav sanity-check clips

# sounddevice is only needed for the optional --record mode.
try:
    import sounddevice as sd  # type: ignore
except Exception:  # pragma: no cover
    sd = None  # type: ignore

# Allow running from project root without installing
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voicefilter.speaker_engine import SpeakerEngine  # noqa: E402

log = logging.getLogger(__name__)


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Read a PCM .wav as mono float32 in [-1, 1]. Uses stdlib, no soundfile.

    The verification script only needs 16k/mono PCM (the format we record
    and the format `tests/audio/*.wav` ships in), so stdlib `wave` is
    sufficient -- avoids adding `soundfile` to requirements.txt.
    """
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n_channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if sampwidth == 2:
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 4:
        audio = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sampwidth == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width: {sampwidth}")
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)
    return audio, sr


def _write_wav_mono(path: Path, audio: np.ndarray, sr: int) -> None:
    """Write mono 16-bit PCM .wav. Stdlib only."""
    pcm = np.clip(audio, -1.0, 1.0)
    pcm_i16 = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_i16.tobytes())


def _resample_if_needed(audio: np.ndarray, sr: int, target_sr: int = 16000) -> np.ndarray:
    if sr == target_sr:
        return audio
    # Cheap linear resample — good enough for verification sanity check
    ratio = target_sr / sr
    new_len = int(len(audio) * ratio)
    x_old = np.linspace(0, 1, len(audio))
    x_new = np.linspace(0, 1, new_len)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def _load_or_record(path: Path, record_sec: float, sr: int = 16000) -> np.ndarray:
    if path.exists() and path.stat().st_size > 0:
        audio, file_sr = _read_wav_mono(path)
        return _resample_if_needed(audio, file_sr, sr)
    if sd is None:
        raise RuntimeError(
            f"{path} not found and sounddevice is not installed -- "
            "install requirements.txt to use --record."
        )
    print(f"Recording {record_sec:.0f}s from default mic → {path} ...")
    rec = sd.rec(int(record_sec * sr), samplerate=sr, channels=1, dtype="float32", blocking=True)
    audio = rec.flatten()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_wav_mono(path, audio, sr)
    return audio


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("me", nargs="?", default="tests/audio/me_10s.wav", type=Path)
    ap.add_argument("other", nargs="?", default="tests/audio/other_10s.wav", type=Path)
    ap.add_argument("--record", action="store_true", help="record fresh clips from default mic")
    ap.add_argument("--threshold", type=float, default=0.62)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    project_root = ROOT
    cfg_path = project_root / "config" / "default.yaml"
    import yaml  # noqa: PLC0415

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    model_path = project_root / cfg["speaker"]["model_path"]
    emb_path = project_root / cfg["embedding_path"]

    engine = SpeakerEngine(str(model_path), threshold=args.threshold)
    if not emb_path.exists():
        print(f"No enrollment at {emb_path}.")
        print("Either run the GUI's enrollment wizard, or pass a me.wav and")
        print("re-run with --record to build one.")
        return 1
    engine.load_enrollment(emb_path)

    rec_sec = 10.0
    me_audio = _load_or_record(args.me, rec_sec)
    other_audio = _load_or_record(args.other, rec_sec)

    for label, audio in (("ME", me_audio), ("OTHER", other_audio)):
        t0 = time.perf_counter()
        is_me, score = engine.verify(audio, sample_rate=16000)
        dt = (time.perf_counter() - t0) * 1000
        verdict = "ACCEPT" if is_me else "REJECT"
        print(f"  {label:5s}  score={score:+.3f}  thr={args.threshold:.2f}  → {verdict}  ({dt:.0f} ms)")

    return 0


if __name__ == "__main__":
    sys.exit(main())