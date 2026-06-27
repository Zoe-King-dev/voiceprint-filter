"""Tests for PathResolver — dev vs frozen (PyInstaller) path resolution.

Frozen mode is exercised by monkeypatching ``sys.frozen`` / ``sys._MEIPASS``
and ``APPDATA``, since the test process itself runs in dev mode.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from voicefilter import paths as paths_mod
from voicefilter.paths import PathResolver


def test_dev_mode_detects_repo_root():
    r = PathResolver()
    assert r.is_frozen is False
    # src/voicefilter/paths.py → parents[2] = repo root containing this test dir
    assert r.project_root == Path(__file__).resolve().parents[1]


def test_resource_under_project_root_in_dev(tmp_path):
    r = PathResolver(tmp_path)
    assert r.resource("models", "x.onnx") == tmp_path / "models" / "x.onnx"


def test_user_data_creates_parent_dirs_in_dev(tmp_path):
    r = PathResolver(tmp_path)
    p = r.user_data("data", "enrollment", "user_embedding.npy")
    assert p == tmp_path / "data" / "enrollment" / "user_embedding.npy"
    assert p.parent.exists()


def test_frozen_mode_uses_meipass_for_resources(tmp_path, monkeypatch):
    meipass = tmp_path / "meipass"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    r = PathResolver()
    assert r.is_frozen is True
    assert r.project_root == meipass
    assert r.resource("models", "spk.onnx") == meipass / "models" / "spk.onnx"


def test_frozen_mode_user_data_goes_to_appdata(tmp_path, monkeypatch):
    meipass = tmp_path / "meipass"
    meipass.mkdir()
    appdata = tmp_path / "AppData"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    r = PathResolver()
    p = r.user_data("logs", "voiceprint-filter.log")
    assert p == appdata / "voiceprint-filter" / "logs" / "voiceprint-filter.log"
    assert p.parent.exists()  # parent dirs created


def test_user_config_path_splits_in_frozen(tmp_path, monkeypatch):
    meipass = tmp_path / "meipass"
    meipass.mkdir()
    appdata = tmp_path / "AppData"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    r = PathResolver()
    # shipped default.yaml stays in _MEIPASS/config (read-only)
    assert r.config_dir() == meipass / "config"
    # user.yaml override is writable under %APPDATA%
    assert r.user_config_path() == appdata / "voiceprint-filter" / "config" / "user.yaml"


def test_appdata_fallback_without_env(monkeypatch, tmp_path):
    meipass = tmp_path / "meipass"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(paths_mod, "Path", Path)  # ensure stdlib Path
    # Reload so _appdata_base picks up the missing env → ~/AppData/Roaming/...
    importlib.reload(paths_mod)
    r = paths_mod.PathResolver()
    base = paths_mod._appdata_base()
    assert base.name == "voiceprint-filter"
    assert base.parent.name == "Roaming"
    # Restore module state for other tests
    monkeypatch.undo()
    importlib.reload(paths_mod)