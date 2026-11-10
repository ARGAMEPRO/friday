"""Instant offline Russian voice: a persistent Piper sidecar.

piper-tts has wheels only up to CPython 3.12, so the voice runs in a small
worker process under python3.12 and the main server talks to it over a pipe.
The model is loaded once, after that a short phrase takes ~0.05 s.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
VENDOR = BASE / "vendor_piper"
MODEL = BASE / "data" / "piper" / "ru_RU-irina-medium.onnx"
WORKER = BASE / "tools" / "piper_worker.py"
CACHE = BASE / "data" / "tts_piper"

_PYTHON = shutil.which("python3.12") or "/usr/local/bin/python3.12"
_lock = threading.RLock()
_process: subprocess.Popen | None = None


def available() -> bool:
    return bool(MODEL.exists() and VENDOR.exists() and Path(_PYTHON).exists())


def _spawn() -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(VENDOR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.Popen(
        [_PYTHON, str(WORKER), str(MODEL)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=str(BASE), env=env,
    )


def _request(text: str, target: Path) -> bool:
    global _process
    if _process is None or _process.poll() is not None:
        _process = _spawn()
    assert _process.stdin and _process.stdout
    try:
        _process.stdin.write(json.dumps({"text": text, "out": str(target)}) + "\n")
        _process.stdin.flush()
        reply = _process.stdout.readline()
    except (BrokenPipeError, OSError):
        _process = None
        return False
    try:
        return bool(json.loads(reply).get("ok")) and target.exists()
    except (ValueError, AttributeError):
        return False


def warmup() -> None:
    """Load the model before the first user request (called from server boot)."""
    if not available():
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    with _lock:
        _request("Проверка связи.", CACHE / "warmup.wav")


def synthesize(text: str) -> bytes | None:
    text = (text or "").strip()
    if not text or not available():
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()
    target = CACHE / f"{key}.wav"
    if target.exists() and target.stat().st_size > 512:
        return target.read_bytes()
    with _lock:
        if not _request(text, target):
            return None
    return target.read_bytes() if target.exists() else None
