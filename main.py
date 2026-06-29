#!/usr/bin/env python
"""Entry point: `python main.py`."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    # Make `src/` importable without requiring `pip install -e .`
    project_root = Path(__file__).resolve().parent
    src = project_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from voicefilter.tray_app import run

    # --show forces the main window to the foreground on launch, which is
    # what you want when developing/testing (otherwise the app starts
    # minimized to the tray and you have to double-click the tray icon to
    # see anything). Without the flag, frozen-exe behavior is preserved:
    # show only on first launch (no enrollment yet).
    show_window = "--show" in sys.argv[1:]
    return run(show_window=show_window)


if __name__ == "__main__":
    sys.exit(main())