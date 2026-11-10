"""Neural TTS through edge-tts (Microsoft voices, needs internet).

Install once to enable it:
    python -m pip install --target vendor edge-tts

Without it the UI falls back to the browser's speech synthesis.

Edge's voices insert surprisingly long pauses at punctuation.  When ffmpeg is
available the rendered file is tightened (silence > 0.28 s is compressed),
which removes the robotic gaps without changing the words.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from .config import BASE_DIR, DATA_DIR

VENDOR = BASE_DIR / "vendor"
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))

try:  # pragma: no cover - optional dependency
    import edge_tts  # type: ignore
except Exception:  # pragma: no cover
    edge_tts = None

CACHE_DIR = DATA_DIR / "tts_cache"


def available() -> bool:
    return edge_tts is not None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def voice_for(text: str, voice_ru: str, voice_en: str) -> str:
    return voice_ru if any("а" <= ch.lower() <= "я" for ch in text) else voice_en


PRONOUNCE = (
    ("F.R.I.D.A.Y.", "Пятница"), ("F.R.I.D.A.Y", "Пятница"),
    ("FRIDAY", "Пятница"), ("Friday", "Пятница"),
    ("ИИ-ассистент", "помощник с искусственным интеллектом"),
    ("ИИ", "искусственный интеллект"),
    ("Wi-Fi", "вай-фай"), ("WiFi", "вай-фай"),
    ("VS Code", "ви эс код"), ("Telegram", "телеграм"),
    ("ЕГЭ", "егэ"), ("ОГЭ", "огэ"),
)


def normalize_text(text: str) -> str:
    """Keep intonation but drop the ~1 s pauses edge-tts makes on sentence ends.

    Sentence boundaries become commas: the voice keeps its melody, while the
    long silence after a full stop disappears. Times (21:30) stay intact.
    """
    cleaned = text or ""
    for source, target in PRONOUNCE:
        cleaned = cleaned.replace(source, target)
    cleaned = re.sub(r"\bИИ\b", "искусственный интеллект", cleaned)
    cleaned = re.sub(r"(?<!\d):(?!\d)", " ", cleaned)
    cleaned = re.sub(r"[.!?;…]+(?=\s|$)", ", ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,")
    return cleaned[:700]


def communicate(text: str, voice: str, rate: str = "+0%", pitch: str = "+0Hz"):
    if edge_tts is None:
        return None
    return edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)


def cache_path(text: str, voice: str, rate: str = "+0%", pitch: str = "+0Hz") -> Path:
    key = hashlib.sha1(f"{voice}|{rate}|{pitch}|{text}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{key}.mp3"


async def _render(text: str, voice: str, rate: str, pitch: str, target: Path) -> None:
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    await communicate.save(str(target))


def _tighten(source: Path, target: Path) -> bool:
    """Compress pauses and trailing silence; returns False when ffmpeg fails."""
    if not ffmpeg_available():
        return False
    filters = (
        "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-45dB,"
        "silenceremove=stop_periods=-1:stop_duration=0.28:stop_threshold=-38dB"
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-af", filters,
                "-codec:a", "libmp3lame", "-b:a", "64k",
                str(target),
            ],
            timeout=20,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return target.exists() and target.stat().st_size > 512


def tighten_into(source: Path, target: Path) -> bool:
    return _tighten(source, target)


def synthesize(
    text: str,
    voice: str,
    rate: str = "+0%",
    pitch: str = "+0Hz",
    tighten: bool = True,
) -> Path | None:
    text = normalize_text(text)
    if not text or edge_tts is None:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = cache_path(text, voice, rate, pitch)
    if target.exists() and target.stat().st_size > 512:
        return target

    part = CACHE_DIR / f"{uuid.uuid4().hex}.part.mp3"
    try:
        asyncio.run(_render(text, voice, rate, pitch, part))
        if not part.exists() or part.stat().st_size <= 512:
            return None
        if tighten and not _tighten(part, target):
            part.replace(target)
        elif not target.exists():
            part.replace(target)
        return target if target.exists() and target.stat().st_size > 512 else None
    except Exception:
        if part.exists():
            part.unlink(missing_ok=True)
        return None
    finally:
        if part.exists():
            part.unlink(missing_ok=True)
