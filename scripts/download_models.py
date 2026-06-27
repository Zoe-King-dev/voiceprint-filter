"""Download 3D-Speaker embedding model + Silero VAD into ./models/.

Run once after `pip install`:
    python scripts/download_models.py
"""
from __future__ import annotations

import hashlib
import logging
import sys
import tarfile
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "asr-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.tar.bz2"
)
EMBEDDING_FILES = (
    "models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
)

VAD_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "asr-models/silero_vad.onnx"
)
VAD_FILE = "models/silero_vad.onnx"


def _download(url: str, dst: Path) -> None:
    if dst.exists():
        log.info("Already present: %s", dst)
        return
    log.info("Downloading %s → %s", url, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, dst.open("wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        chunk = 64 * 1024
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if total:
                pct = got * 100 / total
                print(f"\r  {pct:5.1f}%  {got/1e6:6.1f} / {total/1e6:6.1f} MB", end="", flush=True)
        print()
    log.info("Done: %s (%s bytes)", dst, dst.stat().st_size)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = Path(__file__).resolve().parents[1]
    models = root / "models"
    models.mkdir(exist_ok=True)

    # Embedding: tar.bz2
    tar_path = models / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.tar.bz2"
    _download(EMBEDDING_URL, tar_path)
    if tar_path.exists() and not (models / "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx").exists():
        log.info("Extracting %s ...", tar_path)
        with tarfile.open(tar_path) as t:
            t.extractall(models)
        tar_path.unlink()
    log.info("Embedding model ready: %s", EMBEDDING_FILES)

    # VAD
    _download(VAD_URL, models / "silero_vad.onnx")

    print("\nAll models downloaded under:", models)
    return 0


if __name__ == "__main__":
    sys.exit(main())