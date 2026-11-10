"""Known web sites, desktop apps and fuzzy name matching.

App discovery is platform aware: Windows uses known install paths and Start
Menu shortcuts, macOS scans ``/Applications`` (plus ``~/Applications`` and
``/System/Applications``), Linux uses ``.desktop`` entries.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

PLATFORM = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"

# ---------------------------------------------------------------- web sites
SITES: dict[str, tuple[str, str]] = {
    "яндекс музыка": ("https://music.yandex.ru", "Яндекс Музыку"),
    "yandex music": ("https://music.yandex.ru", "Яндекс Музыку"),
    "решу егэ": ("https://ege.sdamgia.ru", "Решу ЕГЭ"),
    "решу огэ": ("https://oge.sdamgia.ru", "Решу ОГЭ"),
    "сдамгиа": ("https://sdamgia.ru", "Сдам ГИА"),
    "ютуб": ("https://www.youtube.com", "YouTube"),
    "youtube": ("https://www.youtube.com", "YouTube"),
    "вк": ("https://vk.com", "ВКонтакте"),
    "вконтакте": ("https://vk.com", "ВКонтакте"),
    "телеграм": ("https://web.telegram.org", "Telegram Web"),
    "телеграм веб": ("https://web.telegram.org", "Telegram Web"),
    "гитхаб": ("https://github.com", "GitHub"),
    "github": ("https://github.com", "GitHub"),
    "чатгпт": ("https://chatgpt.com", "ChatGPT"),
    "chatgpt": ("https://chatgpt.com", "ChatGPT"),
    "дипсик": ("https://chat.deepseek.com", "DeepSeek"),
    "deepseek": ("https://chat.deepseek.com", "DeepSeek"),
    "яндекс": ("https://ya.ru", "Яндекс"),
    "гугл": ("https://www.google.com", "Google"),
    "google": ("https://www.google.com", "Google"),
    "почта": ("https://mail.yandex.ru", "Яндекс Почту"),
    "яндекс почта": ("https://mail.yandex.ru", "Яндекс Почту"),
    "карты": ("https://yandex.ru/maps", "Яндекс Карты"),
    "яндекс карты": ("https://yandex.ru/maps", "Яндекс Карты"),
    "переводчик": ("https://translate.yandex.ru", "Яндекс Переводчик"),
    "кинопоиск": ("https://www.kinopoisk.ru", "Кинопоиск"),
    "степик": ("https://stepik.org", "Stepik"),
    "википедия": ("https://ru.wikipedia.org", "Википедию"),
    "хабр": ("https://habr.com", "Хабр"),
    "авито": ("https://www.avito.ru", "Авито"),
    "озон": ("https://www.ozon.ru", "Ozon"),
    "дискорд веб": ("https://discord.com/app", "Discord"),
    "фигма": ("https://www.figma.com", "Figma"),
}

# ---------------------------------------------------------------- desktop apps
APP_CANDIDATES: dict[str, list[str]] = {
    "яндекс музыка": [r"%LOCALAPPDATA%\Programs\YandexMusic\Яндекс Музыка.exe"],
    "музыка": [r"%LOCALAPPDATA%\Programs\YandexMusic\Яндекс Музыка.exe"],
    "yandex music": [r"%LOCALAPPDATA%\Programs\YandexMusic\Яндекс Музыка.exe"],
    "vs code": [
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
    ],
    "вс код": [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"],
    "вскод": [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"],
    "visual studio code": [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"],
    "телеграм": [r"%APPDATA%\Telegram Desktop\Telegram.exe"],
    "телеграм десктоп": [r"%APPDATA%\Telegram Desktop\Telegram.exe"],
    "опера": [r"%LOCALAPPDATA%\Programs\Opera\opera.exe", r"%PROGRAMFILES%\Opera\opera.exe"],
    "opera": [r"%LOCALAPPDATA%\Programs\Opera\opera.exe", r"%PROGRAMFILES%\Opera\opera.exe"],
    "хром": [
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
        r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    ],
    "chrome": [r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"],
    "блокнот": ["notepad.exe"],
    "калькулятор": ["calc.exe"],
    "проводник": ["explorer.exe"],
    "paint": ["mspaint.exe"],
    "пейнт": ["mspaint.exe"],
    "стим": [r"%PROGRAMFILES(X86)%\Steam\steam.exe", r"%PROGRAMFILES%\Steam\steam.exe"],
    "steam": [r"%PROGRAMFILES(X86)%\Steam\steam.exe"],
    "дискорд": [r"%LOCALAPPDATA%\Discord\Update.exe"],
    "discord": [r"%LOCALAPPDATA%\Discord\Update.exe"],
    "спотифай": [r"%APPDATA%\Spotify\Spotify.exe"],
    "spotify": [r"%APPDATA%\Spotify\Spotify.exe"],
    "obs": [r"%PROGRAMFILES%\obs-studio\bin\64bit\obs64.exe"],
    "курсфорж": [r"%LOCALAPPDATA%\Programs\CurseForge Windows\CurseForge.exe"],
    "терминал": ["wt.exe"],
    "командная строка": ["cmd.exe"],
    "powershell": ["powershell.exe"],
}

# macOS ships most of these; the rest are common downloads. Order matters:
# the first installed app wins, so "музыка" prefers Яндекс Музыку when present.
MAC_APP_NAMES: dict[str, list[str]] = {
    "яндекс музыка": ["Яндекс Музыка", "Yandex Music"],
    "yandex music": ["Yandex Music", "Яндекс Музыка"],
    "музыка": ["Яндекс Музыка", "Music", "Spotify"],
    "vs code": ["Visual Studio Code", "VSCodium", "Cursor"],
    "вс код": ["Visual Studio Code"],
    "вскод": ["Visual Studio Code"],
    "visual studio code": ["Visual Studio Code"],
    "курсор": ["Cursor"],
    "cursor": ["Cursor"],
    "телеграм": ["Telegram"],
    "телеграм десктоп": ["Telegram"],
    "telegram": ["Telegram"],
    "опера": ["Opera", "Opera GX"],
    "opera": ["Opera", "Opera GX"],
    "хром": ["Google Chrome"],
    "chrome": ["Google Chrome"],
    "google chrome": ["Google Chrome"],
    "сафари": ["Safari"],
    "safari": ["Safari"],
    "фаерфокс": ["Firefox"],
    "firefox": ["Firefox"],
    "блокнот": ["TextEdit"],
    "заметки": ["Notes"],
    "калькулятор": ["Calculator"],
    "проводник": ["Finder"],
    "файндер": ["Finder"],
    "finder": ["Finder"],
    "терминал": ["Terminal", "iTerm", "Warp"],
    "terminal": ["Terminal", "iTerm", "Warp"],
    "стим": ["Steam"],
    "steam": ["Steam"],
    "дискорд": ["Discord", "Discord PTB", "Discord Canary"],
    "discord": ["Discord", "Discord PTB", "Discord Canary"],
    "спотифай": ["Spotify"],
    "spotify": ["Spotify"],
    "obs": ["OBS"],
    "почта": ["Mail"],
    "mail": ["Mail"],
    "календарь": ["Calendar"],
    "фото": ["Photos"],
    "карты": ["Maps"],
    "ollama": ["Ollama"],
    "docker": ["Docker"],
    "блендер": ["Blender"],
    "blender": ["Blender"],
    "кейноут": ["Keynote"],
    "пейджс": ["Pages"],
    "намберс": ["Numbers"],
}

_VOWELS = "аяуюеиыоьй"

APP_DISPLAY = {
    "яндекс музыка": "Яндекс Музыка",
    "музыка": "Яндекс Музыка",
    "yandex music": "Яндекс Музыка",
    "vs code": "VS Code",
    "вс код": "VS Code",
    "вскод": "VS Code",
    "visual studio code": "VS Code",
    "телеграм": "Telegram",
    "телеграм десктоп": "Telegram",
    "опера": "Opera",
    "opera": "Opera",
    "хром": "Google Chrome",
    "chrome": "Google Chrome",
    "блокнот": "Блокнот",
    "калькулятор": "Калькулятор",
    "проводник": "Проводник",
    "стим": "Steam",
    "steam": "Steam",
    "дискорд": "Discord",
    "discord": "Discord",
    "спотифай": "Spotify",
    "spotify": "Spotify",
    "obs": "OBS Studio",
}


def pretty(name: str) -> str:
    key = canon(name)
    for alias, label in APP_DISPLAY.items():
        if canon(alias) == key:
            return label
    return name[:1].upper() + name[1:]



def canon(value: str) -> str:
    """Lowercase, strip punctuation and crude Russian case endings."""
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9 ]+", " ", value)
    words = []
    for word in value.split():
        if len(word) > 4 and word[-1] in _VOWELS:
            word = word[:-1]
        words.append(word)
    return " ".join(words).strip()


def resolve(name: str, table: dict[str, tuple[str, str]], extra: dict[str, str] | None = None):
    """Return (key, url, label) for a site name, best match first."""
    q = canon(name)
    if not q:
        return None
    if extra:
        for alias, url in extra.items():
            if canon(alias) == q:
                return alias, url, alias.title()
        for alias, url in extra.items():
            ca = canon(alias)
            if q and len(q) >= 3 and (ca.startswith(q) or q.startswith(ca)):
                return alias, url, alias.title()
    if q in table:
        url, label = table[q]
        return q, url, label
    best = None
    for key, (url, label) in table.items():
        ck = canon(key)
        if not ck:
            continue
        if ck == q or (len(q) >= 4 and (q.startswith(ck) or ck.startswith(q))):
            score = abs(len(ck) - len(q))
            if best is None or score < best[0]:
                best = (score, key, url, label)
    if best:
        return best[1], best[2], best[3]
    # Voice recognition mangles names ("ютоб", "телеграмм"). Accept a close
    # enough spelling instead of dumping the user into a web search.
    if len(q) >= 4:
        closest = None
        for key, (url, label) in table.items():
            ck = canon(key)
            if not ck:
                continue
            ratio = SequenceMatcher(None, q, ck).ratio()
            if ratio >= 0.74 and (closest is None or ratio > closest[0]):
                closest = (ratio, key, url, label)
        if closest:
            return closest[1], closest[2], closest[3]
    return None


def find_site(name: str, extra: dict[str, str] | None = None):
    return resolve(name, SITES, extra)


@lru_cache(maxsize=1)
def start_menu_links() -> tuple[tuple[str, str], ...]:
    """Shortcuts/app bundles installed on this machine, as (name, path)."""
    if PLATFORM == "macos":
        roots = [
            Path("/Applications"),
            Path("/System/Applications"),
            Path("/System/Applications/Utilities"),
            Path("/System/Library/CoreServices"),
            Path.home() / "Applications",
        ]
        found: list[tuple[str, str]] = []
        for root in roots:
            if not root.exists():
                continue
            try:
                for child in sorted(root.iterdir()):
                    if child.suffix == ".app":
                        found.append((child.stem, str(child)))
                    elif child.is_dir():
                        # Adobe/DaVinci style wrappers keep the bundle one level deep.
                        try:
                            for nested in sorted(child.glob("*.app")):
                                found.append((nested.stem, str(nested)))
                        except OSError:
                            continue
            except OSError:
                continue
        return tuple(found)

    if PLATFORM == "linux":
        roots = [Path("/usr/share/applications"), Path.home() / ".local" / "share" / "applications"]
        found = []
        for root in roots:
            if not root.exists():
                continue
            try:
                for entry in sorted(root.glob("*.desktop")):
                    found.append((entry.stem, str(entry)))
            except OSError:
                continue
        return tuple(found)

    roots = [
        Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")),
        Path(os.path.expandvars(r"%PROGRAMDATA%\Microsoft\Windows\Start Menu\Programs")),
    ]
    found = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                found.append((lnk.stem, str(lnk)))
        except OSError:
            continue
    return tuple(found)


def _existing(candidates: list[str]) -> str | None:
    for raw in candidates:
        if raw.endswith(".exe") and "\\" not in raw and "%" not in raw:
            which = shutil.which(raw)
            if which:
                return which
            continue
        path = Path(os.path.expandvars(raw))
        if path.exists():
            return str(path)
    return None


def _find_mac_app(name: str, fuzzy: bool = True) -> str | None:
    """Resolve a human name ("телеграм") to an installed .app bundle."""
    entries = list(start_menu_links())
    if not entries:
        return None
    q = canon(name)
    if not q:
        return None
    for title, path in entries:
        if canon(title) == q:
            return path
    if not fuzzy or len(q) < 4:
        return None
    best = None
    for title, path in entries:
        ct = canon(title)
        if len(ct) < 3:
            continue
        if q in ct.split() or ct in q.split():
            return path
        ratio = SequenceMatcher(None, q, ct).ratio()
        if ratio >= 0.84 and (best is None or ratio > best[0]):
            best = (ratio, path)
    return best[1] if best else None


def find_app(name: str):
    """Return (display_name, launch_path) for an installed app."""
    q = canon(name)
    if not q:
        return None

    if PLATFORM == "macos":
        # A curated alias table prevents substring accidents like
        # "obs" matching "Obsidian".
        preferred = MAC_APP_NAMES.get(name.casefold()) or MAC_APP_NAMES.get(q)
        if preferred:
            for wanted in preferred:
                path = _find_mac_app(wanted, fuzzy=False)
                if path:
                    return Path(path).stem, path
            return None
        path = _find_mac_app(name)
        if path:
            return Path(path).stem, path
        return None

    for key, candidates in APP_CANDIDATES.items():
        ck = canon(key)
        if q == ck or (len(q) >= 3 and (q in ck or ck in q)):
            path = _existing(candidates)
            if path:
                return key, path
    for title, path in start_menu_links():
        ct = canon(title)
        if not ct:
            continue
        if q == ct or (len(q) >= 4 and (q in ct or ct in q)):
            return title, path
    return None


def looks_like_url(text: str) -> bool:
    text = text.strip()
    if re.match(r"^https?://\S+$", text, re.I):
        return True
    return bool(re.match(r"^[\w-]+(\.[\w-]+)+([/\w\-?=&#%.]*)?$", text)) and "." in text


def search_url(query: str, engine: str = "yandex") -> str:
    if engine == "google":
        return "https://www.google.com/search?q=" + quote(query)
    return "https://yandex.ru/search/?text=" + quote(query)
