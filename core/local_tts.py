"""Local, offline voices.

Two engines, picked automatically:

* **Piper** — Windows ships a bundled ``piper.exe`` in ``vendor/piper``; on
  macOS/Linux an installed ``piper`` binary from PATH (e.g.
  ``brew install piper-tts``) is used. Fast, neural, needs the Russian model in
  ``data/piper``.
* **macOS ``say``** — built into every Mac. Writes 22 kHz WAV through
  ``--file-format=WAVE``, so playback starts instantly without network or
  extra installs. Russian voices (Milena/Yuri) are auto-detected.

Everything is cached on disk; pitch shifts reuse ffmpeg when it is available.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SOURCE_DIR = BASE_DIR / "vendor" / "piper"
VOICE_SOURCE = DATA_DIR / "piper" / "ru_RU-irina-medium.onnx"
CACHE = DATA_DIR / "tts_cache"
CACHE_LIMIT = 120

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _run_kwargs() -> dict:
    """Hide the console window when spawning helpers on Windows."""
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}

_LOCK = threading.Lock()
_ENSURE_STARTED = False
SAY_VOICE_FALLBACKS = ("milena", "yuri", "katya", "alena", "anna", "ольга")


# --------------------------------------------------------------------- piper
def _piper_root() -> Path:
    """Working copy of Piper in an ASCII-only path.

    Piper's Windows build cannot read Cyrillic paths, so the binary and voice
    get copied to %LOCALAPPDATA%\\FridayTTS first.
    """
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA") or ""
        if local and local.isascii():
            return Path(local) / "FridayTTS"
        return Path(os.environ.get("TEMP") or "C:/Temp") / "FridayTTS"
    return Path.home() / ".cache" / "friday" / "piper"


ROOT = _piper_root()
EXE = ROOT / ("piper.exe" if IS_WINDOWS else "piper")
VOICE = ROOT / "voice.onnx"


def _bundled_piper() -> Path | None:
    candidate = SOURCE_DIR / ("piper.exe" if IS_WINDOWS else "piper")
    return candidate if candidate.exists() else None


def _path_piper() -> str | None:
    return shutil.which("piper") or shutil.which("piper-tts")


def piper_exe() -> str | None:
    if EXE.exists() and os.access(EXE, os.X_OK):
        return str(EXE)
    bundled = _bundled_piper()
    if bundled and os.access(bundled, os.X_OK):
        return str(bundled)
    return _path_piper()


def _piper_model() -> Path | None:
    for candidate in (VOICE, VOICE_SOURCE):
        if candidate.exists():
            return candidate
    return None


def sources_available() -> bool:
    return piper_exe() is not None and _piper_model() is not None


def installed() -> bool:
    return piper_exe() is not None and _piper_model() is not None


def ensure() -> bool:
    """Copy Piper to a safe working directory when it is bundled. Idempotent."""
    global _ENSURE_STARTED
    if not IS_WINDOWS:
        return sources_available()
    if EXE.exists() and VOICE.exists():
        return True
    bundled = _bundled_piper()
    if not bundled or not VOICE_SOURCE.exists():
        return False
    with _LOCK:
        if EXE.exists() and VOICE.exists():
            return True
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            shutil.copytree(SOURCE_DIR, ROOT, dirs_exist_ok=True)
            shutil.copyfile(VOICE_SOURCE, VOICE)
            shutil.copyfile(VOICE_SOURCE.with_suffix(".onnx.json"), VOICE.with_suffix(".onnx.json"))
        except OSError:
            return False
    return EXE.exists()


def ensure_async() -> None:
    global _ENSURE_STARTED
    if not IS_WINDOWS or _ENSURE_STARTED or not sources_available():
        return
    _ENSURE_STARTED = True
    threading.Thread(target=ensure, name="piper-copy", daemon=True).start()


# --------------------------------------------------------------------- macOS
@lru_cache(maxsize=1)
def _say_voices() -> tuple[tuple[str, str], ...]:
    """Return ((name, locale), ...) for every installed system voice."""
    if not IS_MAC or not shutil.which("say"):
        return ()
    try:
        result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ()
    voices = []
    for line in (result.stdout or "").splitlines():
        match = re.match(r"^(?P<name>.+?)\s{2,}(?P<locale>[A-Za-z_-]{4,})\s*(?:#|$)", line)
        if match:
            voices.append((match.group("name").strip(), match.group("locale").strip()))
    return tuple(voices)


def say_voice() -> str | None:
    """Best installed Russian voice for ``say``, honouring $FRIDAY_SAY_VOICE."""
    if not IS_MAC or not shutil.which("say"):
        return None
    override = (os.environ.get("FRIDAY_SAY_VOICE") or "").strip()
    voices = _say_voices()
    if override:
        return override
    russian = [(name, loc) for name, loc in voices if loc.lower().startswith("ru")]
    if not russian:
        return None
    for wanted in SAY_VOICE_FALLBACKS:
        for name, _ in russian:
            if wanted in name.casefold():
                return name
    return russian[0][0]


def say_available() -> bool:
    return say_voice() is not None


# --------------------------------------------------------------------- public
def engine() -> str:
    if installed():
        return "piper"
    if say_available():
        return "say"
    return ""


def available() -> bool:
    return bool(engine())


def engine_label() -> str:
    kind = engine()
    if kind == "piper":
        return "piper"
    if kind == "say":
        return f"macOS say ({say_voice()})"
    return ""


def _sample_rate() -> int:
    for candidate in (VOICE.with_suffix(".onnx.json"), VOICE_SOURCE.with_suffix(".onnx.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            return int(data.get("audio", {}).get("sample_rate") or 22050)
        except (OSError, ValueError, TypeError):
            continue
    return 22050


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _trim_cache() -> None:
    try:
        files = sorted(CACHE.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[CACHE_LIMIT:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _apply_pitch(source: Path, target: Path, pitch: float, sample: int) -> bool:
    """Shift pitch while keeping tempo, using ffmpeg. False when unavailable."""
    if abs(pitch - 1.0) < 0.005:
        try:
            source.replace(target)
            return True
        except OSError:
            return False
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        try:
            source.replace(target)
            return True
        except OSError:
            return False
    effect = f"asetrate={sample}*{pitch:.4f},aresample={sample},atempo={1.0 / pitch:.4f}"
    try:
        done = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(source), "-af", effect, str(target)],
            capture_output=True,
            timeout=25,
            **_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        done = None
    source.unlink(missing_ok=True)
    if done is None or done.returncode != 0 or not target.exists() or target.stat().st_size < 1024:
        target.unlink(missing_ok=True)
        return False
    return True


def _synthesize_piper(text: str, target: Path, rate: float, pitch: float) -> bool:
    exe = piper_exe()
    model = _piper_model()
    if not exe or not model:
        return False
    CACHE.mkdir(parents=True, exist_ok=True)
    raw = CACHE / f"{uuid.uuid4().hex}.raw.wav"
    variants = [
        ["--model", str(model), "--output_file", str(raw), "--length_scale", f"{1.0 / rate:.3f}",
         "--sentence_silence", "0.12", "--quiet"],
        ["--model", str(model), "--output_file", str(raw), "--length-scale", f"{1.0 / rate:.3f}",
         "--sentence-silence", "0.12", "-q"],
        ["--model", str(model), "--output_file", str(raw)],
    ]
    for args in variants:
        try:
            result = subprocess.run(
                [exe, *args],
                input=(text + "\n").encode("utf-8"),
                capture_output=True,
                timeout=30,
                **_run_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            raw.unlink(missing_ok=True)
            return False
        if result.returncode == 0 and raw.exists() and raw.stat().st_size > 1024:
            return _apply_pitch(raw, target, pitch, _sample_rate())
        raw.unlink(missing_ok=True)
    return False


def _synthesize_say(text: str, target: Path, rate: float, pitch: float) -> bool:
    voice = say_voice()
    if not voice:
        return False
    CACHE.mkdir(parents=True, exist_ok=True)
    raw = CACHE / f"{uuid.uuid4().hex}.raw.wav"
    wpm = max(80, min(400, int(175 * rate)))
    command = [
        "say", "-v", voice, "-r", str(wpm),
        "-o", str(raw), "--file-format=WAVE", "--data-format=LEI16@22050", text,
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30, **_run_kwargs())
    except (OSError, subprocess.SubprocessError):
        raw.unlink(missing_ok=True)
        return False
    if result.returncode != 0 or not raw.exists() or raw.stat().st_size < 1024:
        raw.unlink(missing_ok=True)
        return False
    return _apply_pitch(raw, target, pitch, 22050)


def synthesize(text: str, rate: float = 1.0, pitch: float = 1.0) -> Path | None:
    text = " ".join((text or "").split())
    if not text:
        return None
    kind = engine()
    if not kind:
        return None
    if kind == "piper":
        ensure()
    rate = max(0.6, min(1.4, float(rate or 1.0)))
    pitch = max(0.4, min(1.6, float(pitch or 1.0)))
    CACHE.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{kind}|{rate:.3f}|{pitch:.3f}|{text}".encode("utf-8")).hexdigest()
    target = CACHE / f"{key}.wav"
    if target.exists() and target.stat().st_size > 1024:
        return target

    ok = _synthesize_piper(text, target, rate, pitch) if kind == "piper" else _synthesize_say(
        text, target, rate, pitch
    )
    if not ok:
        return None
    _trim_cache()
    return target
