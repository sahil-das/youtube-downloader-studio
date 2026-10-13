"""Root launcher script for YouTube Downloader Studio."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repository root is on sys.path when launched directly via `python run.py`
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.main import main

if __name__ == "__main__":
    sys.exit(main())
