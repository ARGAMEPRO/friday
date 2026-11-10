"""Persistent app settings (LLM provider, TTS voices)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import DATA_DIR

SETTINGS_PATH = DATA_DIR / "settings.json"

DEFAULTS: dict = {
    "llm": {
        "provider": "auto",
        "model": "",
        "base_url": "http://127.0.0.1:11434",
        "api_key": "",
    },
    "tts": {
        "enabled": True,
        "engine": "auto",
        "voice_ru": "ru-RU-SvetlanaNeural",
        "voice_en": "en-IE-EmilyNeural",
        "rate": "+0%",
    },
    "telegram": {
        "enabled": False,
        "token": "",
        "allow_ids": [],
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return _merge(DEFAULTS, raw)
        except (OSError, ValueError):
            pass
        return json.loads(json.dumps(DEFAULTS))

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, section: str, key: str, default=None):
        return (self.data.get(section) or {}).get(key, default)

    def section(self, name: str) -> dict:
        return dict(self.data.get(name) or {})

    def update(self, section: str, values: dict) -> dict:
        with self._lock:
            current = dict(self.data.get(section) or {})
            current.update({k: v for k, v in values.items() if v is not None})
            self.data[section] = current
            self.save()
            return current

    @staticmethod
    def parse_ids(raw) -> list[int]:
        """Normalise "123, 456" / [123, "456"] into a list of ints."""
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = raw.replace(";", ",").replace(" ", ",").split(",")
        ids = []
        for item in raw:
            text = str(item).strip()
            if not text:
                continue
            try:
                ids.append(int(text))
            except ValueError:
                continue
        return sorted(set(ids))

    def public(self) -> dict:
        llm = dict(self.data.get("llm") or {})
        key = llm.pop("api_key", "")
        llm["api_key_set"] = bool(key)
        telegram = dict(self.data.get("telegram") or {})
        bot_token = telegram.pop("token", "")
        telegram["token_set"] = bool(bot_token)
        telegram["token_hint"] = (f"{bot_token[:6]}…{bot_token[-4:]}" if len(bot_token) > 12 else "")
        telegram["allow_ids"] = self.parse_ids(telegram.get("allow_ids"))
        return {"llm": llm, "tts": dict(self.data.get("tts") or {}), "telegram": telegram}
