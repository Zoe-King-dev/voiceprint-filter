"""Path resolution across dev / frozen (PyInstaller) / appdata modes.

The program runs in two shapes:

  - **dev**: launched from a source checkout via ``python main.py``.
    Everything — models, config, enrollment, logs — lives under the
    project root. This is the historical behavior and must not change.
  - **frozen**: launched from a PyInstaller-built ``.exe``. Bundled,
    read-only resources (ONNX models, shipped ``default.yaml``) live in
    ``sys._MEIPASS`` (PyInstaller's temp extraction dir). Writable,
    per-user files (enrollment embedding, ``user.yaml``, logs) live in
    ``%APPDATA%/voiceprint-filter/`` so they survive moving the exe.

``PathResolver`` is the single source of truth for which base a relative
path resolves against. ``resource()`` for read-only bundled files,
``user_data()`` for writable per-user files. Downstream code never
branches on ``sys.frozen`` itself — it asks the resolver.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "voiceprint-filter"


def _appdata_base() -> Path:
    """Per-user writable base dir. Frozen mode writes here."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    # Fallback for non-Windows or stripped env: ~/AppData/Roaming/voiceprint-filter
    return Path.home() / "AppData" / "Roaming" / APP_NAME


class PathResolver:
    """Resolve relative paths against the right base for the current mode."""

    def __init__(self, project_root: Path | None = None):
        self._project_root = (project_root or self._detect_root()).resolve()

    @staticmethod
    def _detect_root() -> Path:
        # PyInstaller sets sys.frozen and unpacks bundled data into sys._MEIPASS.
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            return Path(sys._MEIPASS)
        # Dev: this file is src/voicefilter/paths.py → parents[2] = repo root.
        return Path(__file__).resolve().parents[2]

    # --- mode ---------------------------------------------------------------

    @property
    def is_frozen(self) -> bool:
        return bool(getattr(sys, "frozen", False))

    @property
    def project_root(self) -> Path:
        """Bundled-resource root (repo in dev, _MEIPASS when frozen)."""
        return self._project_root

    # --- resolvers ----------------------------------------------------------

    def resource(self, *parts: str) -> Path:
        """Read-only bundled resource (ONNX model, shipped default.yaml).

        Frozen → ``_MEIPASS/parts``; dev → ``project_root/parts``.
        Never creates directories — resources are shipped, not generated.
        """
        return self._project_root.joinpath(*parts)

    def user_data(self, *parts: str) -> Path:
        """Writable per-user file (enrollment, user.yaml, logs).

        Frozen → ``%APPDATA%/voiceprint-filter/parts``; dev →
        ``project_root/parts`` (preserves historical dev layout). Parent
        directories are created on demand.
        """
        base = _appdata_base() if self.is_frozen else self._project_root
        p = base.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def config_dir(self) -> Path:
        """Where shipped ``default.yaml`` lives (read-only)."""
        return self._project_root / "config"

    def user_config_path(self) -> Path:
        """Where the user's optional ``user.yaml`` override lives (writable)."""
        if self.is_frozen:
            return _appdata_base() / "config" / "user.yaml"
        return self._project_root / "config" / "user.yaml"
