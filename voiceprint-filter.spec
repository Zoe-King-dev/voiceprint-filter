# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for voiceprint-filter (single-file Windows exe).

KNOWN HARD PART (flagged in the design doc, ~3 days reserved):
  sherpa-onnx bundles its own ONNX Runtime as *dynamically-loaded* shared
  libraries. PyInstaller's static import analysis cannot see them, so a
  naive build crashes at runtime with "model not found" / DLL load errors.
  We work around it with collect_all('sherpa_onnx'), which pulls the entire
  package tree -- compiled extensions AND the bundled ORT binaries -- into
  the bundle. sounddevice is treated the same way (it ships PortAudio).

  If, after real debugging, this still fails within ~1 week, fall back to
  Approach A from the design doc: ship a detailed Python-env install guide
  instead of a frozen exe. Do NOT silently ship a broken build.

BUNDLE LAYOUT (onefile):
  The exe extracts to a temp _MEIPASS at launch. Bundled read-only
  resources land at _MEIPASS/models/*.onnx and _MEIPASS/config/default.yaml.
  PathResolver.resource() (src/voicefilter/paths.py) reads from exactly
  there when sys.frozen is set, so no code changes are needed between dev
  and frozen. Writable per-user files (enrollment, logs) go to
  %APPDATA%/voiceprint-filter/ via PathResolver.user_data().

ONEFILE vs ONEDIR:
  This spec builds ONEFILE (a single .exe) -- the stated goal for
  non-technical users. Tradeoffs: ~100 MB exe, multi-second first launch
  (temp extraction), and Windows Defender is more likely to flag a large
  unsigned single binary. If Defender false-positives or slow startup
  become a problem, switch to ONEDIR (a folder): replace the EXE(...) call
  below with the COLLECT pattern shown in the comment at the bottom.

BUILD:
  python -m PyInstaller voiceprint-filter.spec --noconfirm
  # models must already be downloaded first:
  python scripts/download_models.py
"""
from PyInstaller.utils.hooks import collect_all

# Pull the full package trees (data + binaries + hidden imports) for the
# two deps that ship native libs PyInstaller can't statically see.
sherpa_datas, sherpa_binaries, sherpa_hidden = collect_all("sherpa_onnx")
sd_datas, sd_binaries, sd_hidden = collect_all("sounddevice")

# PyQt6 has bundled hooks in PyInstaller; pydantic/yaml are pure-ish and
# auto-discovered, but we list them explicitly to be safe.
hiddenimports = sherpa_hidden + sd_hidden + [
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "pydantic",
    "yaml",
]

datas = [
    ("models", "models"),               # bundled ONNX models (read-only)
    ("config/default.yaml", "config"),  # shipped default config (read-only)
] + sherpa_datas + sd_datas

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["src"],            # src-layout: Analysis finds `voicefilter` here
    binaries=sherpa_binaries + sd_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ONEFILE: pass a.binaries + a.datas straight into EXE (no COLLECT).
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="voiceprint-filter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # UPX compression triggers Windows Defender false positives
    runtime_tmpdir=None,
    console=False,    # GUI app; runtime errors land in %APPDATA%/.../logs/
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,        # no .ico asset checked in yet
)

# --- ONEDIR fallback (use instead of the EXE block above if one-file causes
#     Defender/speed issues) -----------------------------------------------
# exe = EXE(
#     pyz, a.scripts, [], exclude_binaries=True, name="voiceprint-filter",
#     console=False, upx=False,
# )
# coll = COLLECT(
#     exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False,
#     name="voiceprint-filter",
# )
