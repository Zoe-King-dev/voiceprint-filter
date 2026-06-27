"""Tests for config loading: deep-merge, resolver wiring, VAD path, validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from voicefilter.config import AppConfig, SpeakerConfig, VADConfig, _deep_merge
from voicefilter.paths import PathResolver


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_deep_merge_override_wins_and_recurses():
    base = {"a": 1, "b": {"x": 1, "y": 2}, "c": 3}
    override = {"b": {"y": 20, "z": 30}, "c": 30}
    _deep_merge(base, override)
    assert base == {"a": 1, "b": {"x": 1, "y": 20, "z": 30}, "c": 30}


def test_deep_merge_override_replaces_non_dict_with_dict():
    base = {"a": {"x": 1}}
    override = {"a": "now a string"}
    _deep_merge(base, override)
    assert base == {"a": "now a string"}


def test_load_resolves_all_paths_absolute_against_resolver(tmp_path):
    _write(
        tmp_path / "config" / "default.yaml",
        """
audio: {sample_rate: 16000}
vad: {enabled: true, model_path: "models/silero_vad.onnx"}
speaker:
  model_path: "models/spk.onnx"
  threshold: 0.62
embedding_path: "data/enrollment/user_embedding.npy"
""",
    )
    r = PathResolver(tmp_path)
    cfg = AppConfig.load(r)
    assert cfg.speaker.model_path.is_absolute()
    assert cfg.vad.model_path.is_absolute()
    assert cfg.embedding_path.is_absolute()
    assert cfg.speaker.model_path == (tmp_path / "models" / "spk.onnx").resolve()
    assert cfg.vad.model_path == (tmp_path / "models" / "silero_vad.onnx").resolve()
    assert cfg.embedding_path == (tmp_path / "data" / "enrollment" / "user_embedding.npy")


def test_load_uses_vad_model_path_from_config(tmp_path):
    """T8: VAD path must come from AppConfig, not a hardcoded constant."""
    _write(
        tmp_path / "config" / "default.yaml",
        'vad: {enabled: true, model_path: "models/custom_vad.onnx"}\n',
    )
    r = PathResolver(tmp_path)
    cfg = AppConfig.load(r)
    assert cfg.vad.model_path.name == "custom_vad.onnx"


def test_user_yaml_overrides_default(tmp_path):
    _write(tmp_path / "config" / "default.yaml", "speaker: {threshold: 0.62}\n")
    _write(tmp_path / "config" / "user.yaml", "speaker: {threshold: 0.70}\n")
    cfg = AppConfig.load(PathResolver(tmp_path))
    assert cfg.speaker.threshold == 0.70


def test_user_yaml_partial_override_keeps_default_untouched(tmp_path):
    _write(
        tmp_path / "config" / "default.yaml",
        "speaker: {threshold: 0.62, other_gain_db: -30.0}\n",
    )
    _write(tmp_path / "config" / "user.yaml", "speaker: {other_gain_db: -45.0}\n")
    cfg = AppConfig.load(PathResolver(tmp_path))
    assert cfg.speaker.other_gain_db == -45.0
    assert cfg.speaker.threshold == 0.62  # default retained


def test_threshold_validation_rejects_out_of_range():
    with pytest.raises(ValueError):
        SpeakerConfig(threshold=0.2)
    with pytest.raises(ValueError):
        SpeakerConfig(threshold=0.99)


def test_vadconfig_has_model_path_default():
    v = VADConfig()
    assert v.model_path == Path("models/silero_vad.onnx")


def test_frame_ms_validation():
    from voicefilter.config import AudioConfig

    with pytest.raises(ValueError):
        AudioConfig(frame_ms=0)
    with pytest.raises(ValueError):
        AudioConfig(frame_ms=300)
    AudioConfig(frame_ms=30)  # sane default accepted


def test_missing_default_yaml_is_valid_empty_config(tmp_path):
    # No config dir at all → empty merged dict → all defaults apply.
    cfg = AppConfig.load(PathResolver(tmp_path))
    assert cfg.speaker.threshold == 0.62
    assert cfg.vad.enabled is True