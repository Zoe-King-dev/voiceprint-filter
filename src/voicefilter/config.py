"""Configuration loading — pydantic-validated, layered default + user override."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    frame_ms: int = 30
    window_sec: float = 1.0
    hop_sec: float = 0.5
    input_device_substring: str = "Microphone"
    output_device_substring: str = "CABLE Input"

    @field_validator("frame_ms")
    @classmethod
    def _frame_ms_div_by_10(cls, v: int) -> int:
        # 30ms = 480 samples @ 16k; ensure sane frame size
        if v <= 0 or v > 200:
            raise ValueError("frame_ms must be in (0, 200]")
        return v


class VADConfig(BaseModel):
    enabled: bool = True
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 100


class SpeakerConfig(BaseModel):
    model_path: Path = Path("models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")
    threshold: float = 0.62
    other_gain_db: float = -30.0
    my_gain_db: float = 0.0
    no_speech_gain_db: float = -6.0

    @field_validator("threshold")
    @classmethod
    def _thr_range(cls, v: float) -> float:
        if not 0.3 <= v <= 0.95:
            raise ValueError("threshold must be in [0.3, 0.95]")
        return v


class AppConfig(BaseModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    embedding_path: Path = Path("data/enrollment/user_embedding.npy")
    log_level: str = "INFO"

    @classmethod
    def load(cls, project_root: Path) -> "AppConfig":
        """Merge default.yaml and user.yaml (if present). user.yaml wins."""
        default = project_root / "config" / "default.yaml"
        user = project_root / "config" / "user.yaml"

        merged: dict[str, Any] = {}
        if default.exists():
            merged = yaml.safe_load(default.read_text(encoding="utf-8")) or {}
        if user.exists():
            override = yaml.safe_load(user.read_text(encoding="utf-8")) or {}
            _deep_merge(merged, override)

        # Resolve relative paths against project_root
        if "speaker" in merged and "model_path" in merged["speaker"]:
            merged["speaker"]["model_path"] = str(
                (project_root / merged["speaker"]["model_path"]).resolve()
            )
        if "embedding_path" in merged:
            merged["embedding_path"] = str(
                (project_root / merged["embedding_path"]).resolve()
            )

        return cls.model_validate(merged)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """In-place deep merge: override wins, recurses into dicts."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v