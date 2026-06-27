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

    return run()


if __name__ == "__main__":
    sys.exit(main())