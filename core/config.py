from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
MEMORY_PATH = DATA_DIR / "memory.json"

HOST = "127.0.0.1"
PORT = int(os.environ.get("FRIDAY_PORT", os.environ.get("JARVIS_PORT", "8765")))
DRY_RUN = (os.environ.get("FRIDAY_DRY_RUN") or os.environ.get("JARVIS_DRY_RUN")) == "1"
