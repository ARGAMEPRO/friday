"""Long-term memory: preferences, facts, custom aliases, command history."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .config import MEMORY_PATH

LISTS = ("likes", "dislikes", "facts", "notes")


def _clean(value: str) -> str:
    return " ".join((value or "").strip().strip(".,;:!?\"'«» ").split())


class Memory:
    def __init__(self, path: Path = MEMORY_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.data = self._load()

    # ------------------------------------------------------------- storage
    def _blank(self) -> dict:
        return {
            "name": None,
            "likes": [],
            "dislikes": [],
            "facts": [],
            "notes": [],
            "aliases": {},
            "corrections": [],
            "profile": {"humor": 2, "address": "босс", "language": "ru"},
            "stats": {"commands": 0, "tasks": 0, "created": time.time(), "last_seen": None},
            "history": [],
        }

    def _load(self) -> dict:
        data = self._blank()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, default in data.items():
                    value = raw.get(key, default)
                    if isinstance(default, list):
                        data[key] = value if isinstance(value, list) else default
                    elif isinstance(default, dict):
                        data[key] = value if isinstance(value, dict) else default
                    else:
                        data[key] = value
                # A hand-edited memory.json must never crash the assistant:
                # coerce stats/profile to dicts before touching them.
                if not isinstance(data.get("stats"), dict):
                    data["stats"] = {"commands": 0, "tasks": 0, "created": time.time(), "last_seen": None}
                data["stats"].setdefault("created", time.time())
                data["stats"].setdefault("commands", 0)
                data["stats"].setdefault("tasks", 0)
                profile = data.get("profile")
                if not isinstance(profile, dict):
                    profile = {"humor": 2, "address": "босс", "language": "ru"}
                profile.setdefault("humor", 2)
                profile.setdefault("address", "босс")
                profile.setdefault("language", "ru")
                data["profile"] = profile
                if not isinstance(data.get("corrections"), list):
                    data["corrections"] = []
        except (OSError, ValueError):
            pass
        return data

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------- writes
    def add(self, kind: str, value: str) -> bool:
        value = _clean(value)
        if not value or kind not in LISTS:
            return False
        with self._lock:
            items = self.data[kind]
            if any(_clean(x).casefold() == value.casefold() for x in items):
                return False
            items.append(value)
            self.save()
            return True

    def remove(self, kind: str, value: str) -> bool:
        value = _clean(value).casefold()
        with self._lock:
            if kind not in LISTS:
                return False
            before = len(self.data[kind])
            self.data[kind] = [x for x in self.data[kind] if _clean(x).casefold() != value]
            changed = len(self.data[kind]) != before
            if changed:
                self.save()
            return changed

    def set_name(self, name: str) -> None:
        with self._lock:
            self.data["name"] = _clean(name).capitalize() or None
            self.save()

    def add_alias(self, alias: str, url: str) -> None:
        alias = _clean(alias)
        if not alias:
            return
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        with self._lock:
            self.data["aliases"][alias] = url
            self.save()

    def add_correction(
        self,
        original: str,
        right: str,
        original_target: str = "",
        right_target: str = "",
        source: str = "voice",
    ) -> dict:
        entry = {
            "ts": time.time(),
            "original": _clean(original),
            "right": _clean(right),
            "original_target": _clean(original_target),
            "right_target": _clean(right_target),
            "source": source,
        }
        with self._lock:
            self.data["corrections"].append(entry)
            self.data["corrections"] = self.data["corrections"][-200:]
            self.save()
        return entry

    def set_profile(self, key: str, value) -> None:
        with self._lock:
            self.data["profile"][key] = value
            self.save()

    def record(self, text: str, kinds: list[str], tasks: int = 1) -> None:
        with self._lock:
            stats = self.data["stats"]
            stats["commands"] = int(stats.get("commands") or 0) + 1
            stats["tasks"] = int(stats.get("tasks") or 0) + tasks
            stats["last_seen"] = time.time()
            self.data["history"].append({"ts": time.time(), "text": text, "kinds": kinds})
            self.data["history"] = self.data["history"][-60:]
            self.save()

    # ------------------------------------------------------------- reads
    @property
    def aliases(self) -> dict[str, str]:
        return dict(self.data.get("aliases") or {})

    @property
    def name(self) -> str | None:
        return self.data.get("name")

    @property
    def profile(self) -> dict:
        return dict(self.data.get("profile") or {})

    @property
    def corrections(self) -> list[dict]:
        return list(self.data.get("corrections") or [])

    def correction_for(self, target: str) -> str | None:
        """Return the user-corrected target for an earlier wrong target."""
        from .catalog import canon

        wanted = canon(target)
        if not wanted:
            return None
        for entry in reversed(self.corrections):
            if entry.get("right_target") and canon(entry.get("original_target", "")) == wanted:
                return entry["right_target"]
        return None

    def summary(self) -> dict:
        with self._lock:
            return {
                "name": self.data.get("name"),
                "likes": list(self.data["likes"]),
                "dislikes": list(self.data["dislikes"]),
                "facts": list(self.data["facts"]),
                "notes": list(self.data["notes"]),
                "aliases": dict(self.data.get("aliases") or {}),
                "corrections": list(self.data.get("corrections") or [])[-8:],
                "profile": dict(self.data.get("profile") or {}),
                "stats": dict(self.data.get("stats") or {}),
                "history": list(self.data.get("history") or [])[-8:],
            }

    def describe(self) -> str:
        """Human readable dump used for the recall command."""
        s = self.summary()
        parts = []
        if s["name"]:
            parts.append(f"Тебя зовут {s['name']}.")
        if s["likes"]:
            parts.append("Ты любишь: " + ", ".join(s["likes"]) + ".")
        if s["dislikes"]:
            parts.append("Не любишь: " + ", ".join(s["dislikes"]) + ".")
        if s["facts"]:
            parts.append("Помню: " + "; ".join(s["facts"]) + ".")
        if s["aliases"]:
            parts.append("Мои ссылки: " + ", ".join(s["aliases"].keys()) + ".")
        if s["corrections"]:
            last = s["corrections"][-1]
            parts.append(f"Последняя правка: «{last['original']}» → «{last['right']}».")
        if not parts:
            parts.append("Пока почти ничего о тебе не знаю. Скажи «запомни, что мне нравится ...».")
        return " ".join(parts)
