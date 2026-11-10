"""Cross-platform desktop integration.

Windows, macOS and Linux each get a thin backend for the same four jobs:
launching things, media keys, system telemetry and VS Code history. Every
function degrades gracefully: it either does the thing or reports honestly
that it could not, instead of pretending success.

Public API (kept compatible with the old ``core/win.py``):
    PLATFORM, LABEL, open_url, open_path, launch_app, close_app, media,
    system_status, foreground_window, recent_vscode, notify
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import catalog
from .config import DATA_DIR, DRY_RUN

PLATFORM = "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"
LABEL = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}[PLATFORM]
IS_WINDOWS = PLATFORM == "windows"
IS_MAC = PLATFORM == "macos"
IS_LINUX = PLATFORM == "linux"

# Windows-only bits are imported lazily so the module stays importable anywhere.
if IS_WINDOWS:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    CREATE_NO_WINDOW = 0x08000000
    user32 = ctypes.windll.user32
else:
    CREATE_NO_WINDOW = 0
    user32 = None

VK_MEDIA = {
    "next": 0xB0,
    "prev": 0xB1,
    "stop": 0xB2,
    "play_pause": 0xB3,
    "mute": 0xAD,
    "vol_down": 0xAE,
    "vol_up": 0xAF,
}

MAC_PLAYERS = ("Music", "Spotify", "VLC", "IINA")
MEDIA_WORDS = {
    "play_pause": "Пауза.",
    "next": "Следующий трек.",
    "prev": "Предыдущий трек.",
    "stop": "Остановил воспроизведение.",
}


# ------------------------------------------------------------------ helpers
def _popen(args, **kwargs):
    """Spawn a detached process without leaking a console window."""
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("stdout", subprocess.DEVNULL)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    if IS_WINDOWS:
        kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    else:
        kwargs.setdefault("start_new_session", True)
    return subprocess.Popen(args, **kwargs)


def _run(args, timeout: float = 10.0):
    """Run a short command and return (returncode, stdout, stderr) as text."""
    kwargs = {"capture_output": True, "text": True, "timeout": timeout}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        result = subprocess.run(args, **kwargs)
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _osascript(script: str, timeout: float = 8.0):
    return _run(["osascript", "-e", script], timeout=timeout)


# ------------------------------------------------------------------ launching
def open_url(url: str, browser: str = "") -> bool:
    if DRY_RUN:
        print(f"[dry-run] open url {url}")
        return True
    try:
        if IS_WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        if IS_MAC:
            if browser:
                return _run(["open", "-a", browser, url])[0] == 0
            return _run(["open", url])[0] == 0
        for opener in ("xdg-open", "gio", "sensible-browser"):
            if not shutil.which(opener):
                continue
            args = [opener, "open", url] if opener == "gio" else [opener, url]
            if _run(args)[0] == 0:
                return True
        return False
    except OSError:
        return False


def open_path(path: str | Path) -> bool:
    target = str(path)
    if DRY_RUN:
        print(f"[dry-run] open path {target}")
        return True
    try:
        if IS_WINDOWS:
            os.startfile(target)  # type: ignore[attr-defined]
            return True
        if IS_MAC:
            return _run(["open", target])[0] == 0
        return _run(["xdg-open", target])[0] == 0
    except OSError:
        return False


def _launch_path(path: str) -> bool:
    """Launch an app path/command in a platform-appropriate way."""
    lowered = path.lower()
    if IS_WINDOWS:
        try:
            if lowered.endswith((".lnk", ".url")):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                _popen([path])
            return True
        except OSError:
            try:
                os.startfile(path)  # type: ignore[attr-defined]
                return True
            except OSError:
                return False
    if IS_MAC:
        if lowered.endswith(".app"):
            return _run(["open", "-a", path])[0] == 0
        if "/" in path:
            return _run(["open", path])[0] == 0
        return _run(["open", "-a", path])[0] == 0
    try:
        _popen([path])
        return True
    except OSError:
        return False


def launch_app(name: str):
    """Find and launch an installed application. Returns (label, path) or None."""
    found = catalog.find_app(name)
    if not found:
        return None
    label, path = found
    if DRY_RUN:
        print(f"[dry-run] launch {label} -> {path}")
        return label, path
    if not _launch_path(path):
        return None
    return label, path


def close_app(name: str) -> tuple[bool, str]:
    """Ask the named application to quit. Returns (ok, human readable detail)."""
    found = catalog.find_app(name)
    target = found[1] if found else name
    if DRY_RUN:
        print(f"[dry-run] close {target}")
        return True, target

    if IS_WINDOWS:
        exe = Path(target).name if target.lower().endswith(".exe") else f"{Path(target).stem}.exe"
        rc, out, err = _run(["taskkill", "/IM", exe, "/F", "/T"])
        return rc == 0, (out or err).strip()

    if IS_MAC:
        app = Path(target).name.removesuffix(".app") if target.lower().endswith(".app") else target
        rc, out, err = _osascript(f'tell application "{app}" to quit')
        if rc == 0:
            return True, f"{app} закрыт"
        rc2, out2, err2 = _run(["pkill", "-x", app])
        if rc2 == 0:
            return True, f"{app} завершён"
        return False, (err or err2 or "приложение не найдено").strip()

    exe = Path(target).name if "/" in target else target
    rc, out, err = _run(["pkill", "-f", exe])
    return rc == 0, (out or err).strip()


# ------------------------------------------------------------------ media keys
def _windows_press(vk: int, extended: bool = True) -> bool:
    if not user32:
        return False
    try:
        flags_down = 0x0001 if extended else 0
        user32.keybd_event(vk, 0, flags_down, 0)
        user32.keybd_event(vk, 0, flags_down | 0x0002, 0)
        return True
    except Exception:
        return False


def _mac_volume(delta: int | None = None, mute: bool | None = None) -> tuple[bool, str]:
    if mute is not None:
        rc, _, err = _osascript(f"set volume output muted {'true' if mute else 'false'}")
        return (rc == 0), ("Звук переключён." if rc == 0 else err)
    rc, out, err = _osascript("output volume of (get volume settings)")
    if rc != 0:
        return False, err or "не удалось прочитать громкость"
    try:
        current = int(float(out.strip() or 50))
    except ValueError:
        current = 50
    level = max(0, min(100, current + (delta or 0)))
    rc, _, err = _osascript(f"set volume output volume {level}")
    if rc != 0:
        return False, err or "не удалось изменить громкость"
    return True, f"Громкость {level}%."


def _mac_running_players() -> list[str]:
    running = []
    for player in MAC_PLAYERS:
        rc, out, _ = _osascript(f'application "{player}" is running')
        if rc == 0 and out.strip().lower() == "true":
            running.append(player)
    return running


def _mac_media(action: str) -> tuple[bool, str]:
    verbs = {
        "play_pause": "playpause",
        "stop": "stop",
        "next": "next track",
        "prev": "previous track",
    }
    verb = verbs.get(action)
    if not verb:
        return False, "Не поняла, что сделать с медиа."
    for player in _mac_running_players():
        rc, _, err = _osascript(f'tell application "{player}" to {verb}')
        if rc == 0:
            return True, MEDIA_WORDS[action]
        return False, err or f"{player} не ответил"
    # Nothing recognizable is playing: fall back to the system media keys.
    keycodes = {"play_pause": 16, "next": 17, "prev": 18, "stop": 16}
    code = keycodes.get(action, 16)
    rc, _, err = _osascript(f'tell application "System Events" to key code {code}')
    if rc == 0:
        return True, MEDIA_WORDS[action]
    return False, (
        "Не вижу запущенного плеера (Music/Spotify/VLC). Если музыка играет в браузере — "
        "разреши «Универсальный доступ» Терминалу, чтобы я могла нажимать медиа-клавиши."
    )


def _linux_media(action: str) -> tuple[bool, str]:
    if action in ("play_pause", "next", "prev", "stop"):
        if not shutil.which("playerctl"):
            return False, "playerctl не установлен — медиа-клавиши недоступны."
        command = {"play_pause": "play-pause", "stop": "stop", "next": "next", "prev": "previous"}[action]
        rc, _, err = _run(["playerctl", command])
        return (rc == 0), (MEDIA_WORDS[action] if rc == 0 else err or "плеер не найден")
    if not shutil.which("pactl"):
        return False, "pactl не установлен — громкость недоступна."
    if action == "mute":
        rc, _, err = _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        return (rc == 0), ("Звук переключён." if rc == 0 else err)
    step = "5%+" if action == "volume_up" else "5%-"
    rc, _, err = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", step])
    return (rc == 0), ("Сделал громче." if action == "volume_up" and rc == 0
                       else "Сделал тише." if rc == 0 else err)


def media(action: str) -> dict:
    """Perform a media action. Returns {ok, speech} — honestly."""
    if DRY_RUN:
        print(f"[dry-run] media {action}")
        return {"ok": True, "speech": MEDIA_WORDS.get(action, "Медиа.")}
    if IS_WINDOWS:
        if not user32:
            return {"ok": False, "speech": "Медиа-клавиши недоступны в этой системе."}
        if action == "volume_up":
            for _ in range(4):
                _windows_press(VK_MEDIA["vol_up"], extended=False)
            return {"ok": True, "speech": "Сделал громче."}
        if action == "volume_down":
            for _ in range(4):
                _windows_press(VK_MEDIA["vol_down"], extended=False)
            return {"ok": True, "speech": "Сделал тише."}
        if action == "mute":
            _windows_press(VK_MEDIA["mute"], extended=False)
            return {"ok": True, "speech": "Звук переключён."}
        if action in MEDIA_WORDS:
            _windows_press(VK_MEDIA[action])
            return {"ok": True, "speech": MEDIA_WORDS[action]}
        return {"ok": False, "speech": "Не поняла, что сделать с медиа."}

    if IS_MAC:
        if action == "volume_up":
            ok, text = _mac_volume(delta=8)
            return {"ok": ok, "speech": text if not ok else f"Сделал громче. {text}"}
        if action == "volume_down":
            ok, text = _mac_volume(delta=-8)
            return {"ok": ok, "speech": text if not ok else f"Сделал тише. {text}"}
        if action == "mute":
            rc, out, _ = _osascript("output muted of (get volume settings)")
            muted = out.strip().lower() == "true"
            ok, text = _mac_volume(mute=not muted)
            return {"ok": ok, "speech": text}
        ok, text = _mac_media(action)
        return {"ok": ok, "speech": text}

    ok, text = _linux_media(action)
    return {"ok": ok, "speech": text}


# ------------------------------------------------------------------ telemetry
def _bytes_gb(value: float) -> float:
    return round(value / 1024**3, 1)


def _mac_memory() -> dict:
    rc, out, _ = _run(["sysctl", "-n", "hw.memsize"])
    total = int(out.strip() or 0) if rc == 0 and out.strip().isdigit() else 0
    if not total:
        return {}
    rc, vm, _ = _run(["vm_stat"])
    page_size = 4096
    match = re.search(r"page size of (\d+) bytes", vm)
    if match:
        page_size = int(match.group(1))
    values = {}
    for line in vm.splitlines():
        m = re.match(r"([^:]+):\s+([\d.]+)", line)
        if m:
            values[m.group(1).strip()] = float(m.group(2))
    free_pages = values.get("Pages free", 0) + values.get("Pages speculative", 0)
    # Inactive pages are reclaimable cache, the way Activity Monitor counts
    # "free" memory. Counting only Pages free reports ~98% used on macOS.
    available_pages = free_pages + values.get("Pages inactive", 0)
    available = min(available_pages * page_size, total)
    used = max(0, total - available)
    return {
        "ram_total_gb": _bytes_gb(total),
        "ram_free_gb": _bytes_gb(available),
        "ram_percent": int(round(used / total * 100)) if total else 0,
    }


def _mac_battery() -> dict:
    rc, out, _ = _run(["pmset", "-g", "batt"])
    if rc != 0:
        return {}
    match = re.search(r"(\d+)%", out)
    if not match:
        return {}
    percent = int(match.group(1))
    return {
        "battery": percent,
        "charging": bool(re.search(r"(charging|AC Power|full)", out, re.IGNORECASE))
        and not re.search(r"discharging", out, re.IGNORECASE),
    }


def _mac_uptime() -> float | None:
    rc, out, _ = _run(["sysctl", "-n", "kern.boottime"])
    match = re.search(r"sec = (\d+)", out)
    if rc != 0 or not match:
        return None
    return round((time.time() - int(match.group(1))) / 3600, 1)


def _linux_memory() -> dict:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            info[key.strip()] = int(rest.strip().split()[0]) * 1024
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        if not total:
            return {}
        return {
            "ram_total_gb": _bytes_gb(total),
            "ram_free_gb": _bytes_gb(available),
            "ram_percent": int(round((total - available) / total * 100)),
        }
    except (OSError, ValueError):
        return {}


def _linux_battery() -> dict:
    base = Path("/sys/class/power_supply")
    if not base.exists():
        return {}
    for battery in sorted(base.glob("BAT*")):
        try:
            percent = int((battery / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
        status = ""
        try:
            status = (battery / "status").read_text().strip()
        except OSError:
            pass
        return {"battery": percent, "charging": status.lower() in ("charging", "full")}
    return {}


def _windows_status() -> dict:  # pragma: no cover - Windows only
    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _POWERSTATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    data: dict = {}
    try:
        mem = _MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(mem)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            data["ram_percent"] = int(mem.dwMemoryLoad)
            data["ram_total_gb"] = _bytes_gb(mem.ullTotalPhys)
            data["ram_free_gb"] = _bytes_gb(mem.ullAvailPhys)
    except (OSError, AttributeError):
        pass
    try:
        power = _POWERSTATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)):
            data["battery"] = int(power.BatteryLifePercent)
            data["charging"] = power.ACLineStatus == 1
    except (OSError, AttributeError):
        pass
    try:
        data["uptime_h"] = round(ctypes.windll.kernel32.GetTickCount64() / 3_600_000, 1)
    except (OSError, AttributeError):
        pass
    return data


def _system_drive() -> str:
    if IS_WINDOWS:
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def system_status() -> dict:
    data: dict = {"cpu_count": os.cpu_count(), "platform": LABEL}
    if IS_WINDOWS:
        data.update(_windows_status())
    elif IS_MAC:
        data.update(_mac_memory())
        data.update(_mac_battery())
        uptime = _mac_uptime()
        if uptime is not None:
            data["uptime_h"] = uptime
    else:
        data.update(_linux_memory())
        data.update(_linux_battery())
        try:
            data["uptime_h"] = round(
                float(Path("/proc/uptime").read_text().split()[0]) / 3600, 1
            )
        except (OSError, ValueError, IndexError):
            pass
    try:
        usage = shutil.disk_usage(_system_drive())
        data["disk_free_gb"] = _bytes_gb(usage.free)
        data["disk_total_gb"] = _bytes_gb(usage.total)
    except OSError:
        pass
    data["time"] = dt.datetime.now().strftime("%H:%M")
    return data


def foreground_window() -> dict:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        if not user32:
            return {}
        try:
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            return {"title": buf.value}
        except Exception:
            return {}
    if IS_MAC:
        rc, out, _ = _osascript(
            'tell application "System Events" to get name of first application process whose frontmost is true'
        )
        if rc == 0 and out.strip():
            return {"title": out.strip()}
    return {}


# ------------------------------------------------------------------ extras
def screenshot(path: Path | str | None = None) -> Path | None:
    """Capture the whole screen to a PNG. Returns the file or None."""
    target = Path(path) if path else DATA_DIR / "shots" / f"shot-{int(time.time())}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        rc, _, _ = _run(["screencapture", "-x", "-t", "png", str(target)], timeout=15)
    elif IS_WINDOWS:  # pragma: no cover - Windows only
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.Screen]::PrimaryScreen.Bounds;"
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);"
            "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
            f"$bmp.Save('{str(target).replace(chr(92), '/')}')"
        )
        rc, _, _ = _run(["powershell", "-NoProfile", "-Command", script], timeout=25)
    else:
        for tool in (["gnome-screenshot", "-f"], ["scrot", "-o"], ["import", "-window", "root"]):
            if shutil.which(tool[0]):
                rc, _, _ = _run(tool + [str(target)], timeout=20)
                break
        else:
            return None
    if rc == 0 and target.exists() and target.stat().st_size > 0:
        return target
    return None


def find_files(query: str, limit: int = 8) -> list[str]:
    """Find files by name in the user's home. Spotlight on macOS, find elsewhere."""
    query = (query or "").strip()
    if not query:
        return []
    home = str(Path.home())
    if IS_MAC:
        rc, out, _ = _run(["mdfind", "-onlyin", home, "-name", query], timeout=12)
    elif IS_WINDOWS:  # pragma: no cover - Windows only
        rc, out, _ = _run(["where", "/r", home, f"*{query}*"], timeout=20)
    else:
        rc, out, _ = _run(["find", home, "-maxdepth", "6", "-iname", f"*{query}*",
                           "-type", "f", "-not", "-path", "*/.git/*"], timeout=20)
    if rc not in (0, 1) or not out:
        return []
    # mdfind also returns folders and app bundles; keep real files.
    files = [line for line in out.splitlines() if line.strip() and Path(line).is_file()]
    return files[:limit]


def lock_screen() -> tuple[bool, str]:
    if DRY_RUN:
        return True, "Заблокировала бы экран (dry-run)."
    if IS_MAC:
        rc, _, _ = _run(["pmset", "displaysleepnow"], timeout=5)
        return (rc == 0, "Блокирую экран." if rc == 0 else "Не получилось заблокировать экран.")
    if IS_WINDOWS:  # pragma: no cover - Windows only
        rc, _, _ = _run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=5)
        return (rc == 0, "Блокирую экран." if rc == 0 else "Не получилось заблокировать экран.")
    rc, _, _ = _run(["loginctl", "lock-session"], timeout=5)
    return (rc == 0, "Блокирую экран." if rc == 0 else "Не получилось заблокировать экран.")


def _mac_wifi_interface() -> str:
    rc, out, _ = _run(["networksetup", "-listallhardwareports"], timeout=5)
    if rc == 0:
        block = re.search(r"Hardware Port: (?:Wi-Fi|AirPort).*?Device: (\w+)", out, re.S)
        if block:
            return block.group(1)
    return "en0"


def wifi(enabled: bool) -> tuple[bool, str]:
    if DRY_RUN:
        return True, ("Включила бы Wi-Fi." if enabled else "Выключила бы Wi-Fi.") + " (dry-run)"
    if IS_MAC:
        device = _mac_wifi_interface()
        rc, out, err = _run(["networksetup", "-setairportpower", device, "on" if enabled else "off"], timeout=8)
        if rc != 0:
            return False, f"Не получилось: {err or out or 'нет доступа'}."
        return True, ("Wi-Fi включён." if enabled else "Wi-Fi выключен.")
    if IS_WINDOWS:  # pragma: no cover - Windows only
        state = "enabled" if enabled else "disabled"
        rc, _, err = _run(["netsh", "interface", "set", "interface", "Wi-Fi", f"admin={state}"], timeout=8)
        return rc == 0, ("Wi-Fi включён." if enabled else "Wi-Fi выключен.") if rc == 0 else err
    if shutil.which("nmcli"):
        rc, _, err = _run(["nmcli", "radio", "wifi", "on" if enabled else "off"], timeout=8)
        return rc == 0, ("Wi-Fi включён." if enabled else "Wi-Fi выключен.") if rc == 0 else err
    return False, "Не умею управлять Wi-Fi на этой системе."


def notify(title: str, message: str) -> bool:
    """Best-effort desktop notification."""
    if DRY_RUN:
        print(f"[dry-run] notify {title}: {message}")
        return True
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    try:
        if IS_MAC:
            rc, _, _ = _osascript(
                f'display notification "{safe_message}" with title "{safe_title}"'
            )
            return rc == 0
        if IS_LINUX and shutil.which("notify-send"):
            return _run(["notify-send", safe_title, safe_message])[0] == 0
        if IS_WINDOWS:
            script = (
                "[reflection.assembly]::loadwithpartialname('System.Windows.Forms')"
                " > $null; $n = New-Object System.Windows.Forms.NotifyIcon;"
                " $n.Icon = [System.Drawing.SystemIcons]::Information;"
                f" $n.Visible = $true; $n.ShowBalloonTip(5000, '{safe_title}', '{safe_message}',"
                " 'Info'); Start-Sleep -Seconds 5; $n.Dispose()"
            )
            return _run(["powershell", "-NoProfile", "-Command", script])[0] == 0
    except OSError:
        return False
    return False


# ------------------------------------------------------------------ VS Code
def _file_uri_to_path(uri: str) -> tuple[str, bool]:
    parts = urlsplit(uri)
    path = unquote(parts.path)
    if IS_WINDOWS:
        if path.startswith("/"):
            path = path[1:]
        path = path.replace("/", "\\")
    return path, path.lower().endswith(".code-workspace")


def _ago(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"
    days = int(delta // 86400)
    if days == 1:
        return "вчера"
    return f"{days} дн назад"


def _user_data_dirs() -> list[Path]:
    """Return VS Code-like "User" directories for every flavour found."""
    home = Path.home()
    apps = ("Code", "Cursor", "Code - Insiders", "VSCodium", "Windsurf")
    if IS_WINDOWS:
        roots = [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))]
    elif IS_MAC:
        roots = [home / "Library" / "Application Support"]
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
        roots = [Path(xdg)]
        roots += [home / ".var" / "app" / "com.vscodium.codium" / "config"]
    dirs = []
    for root in roots:
        for app in apps:
            user = root / app / "User"
            if user.exists():
                dirs.append(user)
    return dirs


def _recent_from_state_db(user_dir: Path, limit: int) -> list[dict]:
    db = user_dir / "globalStorage" / "state.vscdb"
    if not db.exists():
        return []
    items: list[dict] = []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        row = con.execute(
            "select value from ItemTable where key='history.recentlyOpenedPathsList'"
        ).fetchone()
        con.close()
        if not row:
            return []
        payload = json.loads(row[0])
        for entry in payload.get("entries", []):
            uri = entry.get("folderUri") or entry.get("workspace") or entry.get("fileUri")
            if not uri:
                continue
            path, is_ws = _file_uri_to_path(uri)
            items.append(
                {
                    "name": Path(path).stem if is_ws else Path(path).name,
                    "path": path,
                    "opened_ts": 0,
                    "exists": Path(path).exists(),
                    "kind": "workspace" if is_ws else "folder",
                }
            )
    except (OSError, ValueError, sqlite3.Error, TypeError):
        return []
    return items[:limit]


def recent_vscode(limit: int = 5) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for user_dir in _user_data_dirs():
        for item in _recent_from_state_db(user_dir, limit * 2):
            key = item["path"].casefold()
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

        storage = user_dir / "workspaceStorage"
        if not storage.exists():
            continue
        try:
            folders = list(storage.iterdir())
        except OSError:
            continue
        for folder in folders:
            workspace_json = folder / "workspace.json"
            if not folder.is_dir() or not workspace_json.exists():
                continue
            try:
                meta = json.loads(workspace_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            uri = meta.get("folder") or meta.get("workspace")
            if not uri:
                continue
            path, is_ws = _file_uri_to_path(uri)
            if path.casefold() in seen:
                continue
            try:
                mtimes = [f.stat().st_mtime for f in folder.iterdir() if f.is_file()]
                ts = max(mtimes) if mtimes else workspace_json.stat().st_mtime
            except OSError:
                ts = workspace_json.stat().st_mtime
            seen.add(path.casefold())
            items.append(
                {
                    "name": Path(path).stem if is_ws else Path(path).name,
                    "path": path,
                    "opened_ts": ts,
                    "opened_ago": _ago(ts),
                    "exists": Path(path).exists(),
                    "kind": "workspace" if is_ws else "folder",
                }
            )

    items.sort(key=lambda x: x.get("opened_ts") or 0, reverse=True)
    for item in items:
        if not item.get("opened_ago") and item.get("opened_ts"):
            item["opened_ago"] = _ago(item["opened_ts"])
    return items[:limit]
