"""HTTP server + JSON API for the F.R.I.D.A.Y. UI."""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import local_tts, system, tts
from core.config import DATA_DIR, HOST, PORT, WEB_DIR
from core.memory import Memory
from core.settings import Settings
from core.tasks import Engine
from core.telegram import TelegramBot

CALIBRATION = [
    {"id": "music", "title": "Запуск музыки", "command": "запусти яндекс музыку"},
    {"id": "ege", "title": "Сайт в браузере", "command": "в браузере открой сайт решу егэ"},
    {"id": "vscode", "title": "История VS Code", "command": "какие папки я последний раз открывал в vs code"},
    {"id": "track", "title": "Музыка: следующий трек", "command": "включи следующий трек"},
    {"id": "status", "title": "Статус системы", "command": "статус системы"},
    {"id": "time", "title": "Время", "command": "который час"},
    {"id": "memory", "title": "Память", "command": "запомни, что мне нравится рок"},
    {"id": "chat", "title": "Живой разговор", "command": "поговорим"},
]


class FridayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine: Engine, telegram: TelegramBot | None = None):
        super().__init__(address, Handler)
        self.engine = engine
        self.telegram = telegram


class Handler(BaseHTTPRequestHandler):
    server_version = "Friday/2.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if not cache else "public, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _static(self, url_path: str) -> None:
        rel = "index.html" if url_path in ("", "/") else url_path.lstrip("/")
        root = WEB_DIR.resolve()
        try:
            target = (root / rel).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            ctype = "text/javascript; charset=utf-8"
        if target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        self._send(200, target.read_bytes(), ctype or "application/octet-stream")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _stream_tts(self, text: str, voice: str, rate: str, pitch: str, target) -> None:
        """Start playing as soon as Edge sends the first audio chunk."""
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "none")
        self.end_headers()

        tts.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        part = tts.CACHE_DIR / f"{uuid.uuid4().hex}.part.mp3"

        async def pump() -> None:
            communicate = tts.communicate(text, voice, rate, pitch)
            if communicate is None:
                raise RuntimeError("edge-tts unavailable")
            with open(part, "wb") as handle:
                async for chunk in communicate.stream():
                    if chunk.get("type") != "audio":
                        continue
                    data = chunk.get("data") or b""
                    if not data:
                        continue
                    handle.write(data)
                    self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                    self.wfile.flush()

        try:
            asyncio.run(pump())
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            if part.exists() and part.stat().st_size > 512:
                if not tts.tighten_into(part, target):
                    part.replace(target)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
        finally:
            part.unlink(missing_ok=True)

    # ------------------------------------------------------------- routes
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/state":
            since = int((query.get("since") or ["0"])[0])
            self._json(self.server.engine.state(since))
            return

        if parsed.path == "/api/health":
            telegram = getattr(self.server, "telegram", None)
            self._json(
                {
                    "ok": True,
                    "time": time.time(),
                    "name": "FRIDAY",
                    "platform": system.PLATFORM,
                    "platform_label": system.LABEL,
                    "python": sys.version.split()[0],
                    "telegram": bool(telegram and telegram.running),
                }
            )
            return

        if parsed.path == "/api/vscode/recent":
            try:
                n = int((query.get("n") or ["5"])[0])
            except ValueError:
                n = 5
            self._json({"items": system.recent_vscode(max(1, min(n, 20)))})
            return

        if parsed.path == "/api/telegram":
            bot = getattr(self.server, "telegram", None)
            self._json(
                {
                    "ok": True,
                    "platform": system.PLATFORM,
                    "bot": bot.status() if bot else {"configured": False, "running": False},
                    "settings": self.server.engine.settings.public().get("telegram", {}),
                }
            )
            return

        if parsed.path == "/api/settings":
            engine = self.server.engine
            memory = engine.memory
            local_kind = local_tts.engine()
            if local_kind == "say":
                label = "say+edge-tts" if tts.available() else "say"
            elif local_kind == "piper":
                label = "piper+edge-tts" if tts.available() else "piper"
            else:
                label = "edge-tts" if tts.available() else "browser"
            self._json(
                {
                    "settings": engine.settings.public(),
                    "profile": memory.profile,
                    "corrections": memory.corrections[-8:],
                    "correction_count": len(memory.corrections),
                    "tts_available": tts.available() or local_tts.available(),
                    "tts_engine": label,
                    "piper_available": local_kind == "piper",
                    "local_tts": local_kind,
                    "local_tts_label": local_tts.engine_label(),
                    "platform": system.PLATFORM,
                    "platform_label": system.LABEL,
                }
            )
            return

        if parsed.path == "/api/llm/status":
            force = (query.get("force") or ["0"])[0] == "1"
            self._json(self.server.engine.brain.status(force))
            return

        if parsed.path == "/api/llm/setup":
            self._json(self.server.engine.brain.setup())
            return

        if parsed.path == "/api/tts":
            text = ((query.get("text") or [""])[0] or "").strip()[:700]
            voice = (query.get("voice") or [""])[0]
            if not text:
                self._json({"error": "empty text"}, 400)
                return
            config = self.server.engine.settings.section("tts")
            engine_preference = config.get("engine", "auto")
            is_russian = any("а" <= ch.lower() <= "я" for ch in text)

            rate_value = 1.0
            pitch_value = 1.0
            try:
                raw_rate = (query.get("rate") or [""])[0]
                if raw_rate:
                    rate_value = max(0.6, min(1.4, float(raw_rate)))
            except ValueError:
                pass
            try:
                raw_pitch = (query.get("pitch") or [""])[0]
                if raw_pitch:
                    pitch_value = max(0.5, min(1.6, float(raw_pitch)))
            except ValueError:
                pass

            local = local_tts.engine()
            # edge-tts sounds much better than the built-in say voices, so in
            # "auto" mode Russian goes neural too; `say` stays as the offline
            # fallback and as the explicit "piper"/"local" choice.
            use_local = (
                is_russian
                and bool(local)
                and (engine_preference in ("piper", "local") or not tts.available())
            )
            if use_local:
                path = local_tts.synthesize(text, rate_value, pitch_value)
                if path:
                    self._send(200, path.read_bytes(), "audio/wav", cache=True)
                    return

            if not tts.available():
                self._json({"error": "no neural voice available"}, 503)
                return
            speech_text = tts.normalize_text(text)
            if not speech_text:
                self._json({"error": "empty text"}, 400)
                return
            voice = voice or tts.voice_for(speech_text, config.get("voice_ru", ""), config.get("voice_en", ""))
            rate = f"{(rate_value - 1) * 100:+.0f}%"
            pitch = f"{(pitch_value - 1) * 100:+.0f}Hz"

            target = tts.cache_path(speech_text, voice, rate, pitch)
            if target.exists() and target.stat().st_size > 512:
                self._send(200, target.read_bytes(), "audio/mpeg", cache=True)
                return
            self._stream_tts(speech_text, voice, rate, pitch, target)
            return

        if parsed.path == "/api/calibration":
            self._json(
                {
                    "scenarios": CALIBRATION,
                    "profile": self.server.engine.memory.profile,
                    "corrections": self.server.engine.memory.corrections[-10:],
                }
            )
            return

        self._static(parsed.path)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._body()
        engine = self.server.engine

        if parsed.path == "/api/command":
            text = (payload.get("text") or "").strip()
            if not text:
                self._json({"error": "empty text"}, 400)
                return
            try:
                tasks = engine.submit(text)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"ok": True, "tasks": tasks})
            return

        if parsed.path == "/api/memory":
            memory = engine.memory
            action = payload.get("action")
            kind = payload.get("kind", "facts")
            value = (payload.get("value") or "").strip()
            if action == "add":
                ok = memory.add(kind, value)
                self._json({"ok": ok, "memory": memory.summary()})
            elif action == "remove":
                ok = memory.remove(kind, value)
                self._json({"ok": ok, "memory": memory.summary()})
            elif action == "name":
                memory.set_name(value)
                self._json({"ok": True, "memory": memory.summary()})
            else:
                self._json({"error": "unknown action"}, 400)
            return

        if parsed.path == "/api/feedback":
            correction = (payload.get("correction") or "").strip()
            if not correction:
                self._json({"error": "empty correction"}, 400)
                return
            entry = engine.apply_feedback(
                payload.get("task_id") or "",
                correction,
                source=payload.get("source") or "ui",
                original=payload.get("original") or "",
            )
            self._json(
                {
                    "ok": True,
                    "entry": entry,
                    "memory": engine.memory.summary(),
                    "message": "Правка сохранена в память.",
                }
            )
            return

        if parsed.path == "/api/settings":
            section = payload.get("section")
            values = payload.get("values") or {}
            if section == "profile":
                for key in ("humor", "address"):
                    if key in values and values[key] not in (None, ""):
                        engine.memory.set_profile(key, values[key])
                self._json({"ok": True, "profile": engine.memory.profile})
                return
            if section not in ("llm", "tts", "telegram"):
                self._json({"error": "unknown section"}, 400)
                return
            if section == "telegram":
                values = dict(values)
                if "allow_ids" in values:
                    values["allow_ids"] = Settings.parse_ids(values.get("allow_ids"))
                if "token" in values:
                    values["token"] = (values["token"] or "").strip()
            engine.settings.update(section, values)
            if section == "llm":
                engine.brain._status_cache = {}
            self._json({"ok": True, "settings": engine.settings.public()})
            return

        if parsed.path == "/api/telegram":
            bot = getattr(self.server, "telegram", None)
            if bot is None:
                self._json({"ok": False, "error": "telegram is not initialised"}, 503)
                return
            action = payload.get("action") or "status"
            if action == "start":
                values = {}
                if payload.get("token"):
                    values["token"] = str(payload["token"]).strip()
                if "allow_ids" in payload:
                    values["allow_ids"] = Settings.parse_ids(payload.get("allow_ids"))
                values["enabled"] = True
                engine.settings.update("telegram", values)
                result = bot.start(values.get("token"))
                self._json(result)
                return
            if action == "stop":
                engine.settings.update("telegram", {"enabled": False})
                self._json(bot.stop())
                return
            if action == "save":
                values = {"enabled": bool(payload.get("enabled", True))}
                if payload.get("token"):
                    values["token"] = str(payload["token"]).strip()
                if "allow_ids" in payload:
                    values["allow_ids"] = Settings.parse_ids(payload.get("allow_ids"))
                engine.settings.update("telegram", values)
                self._json({"ok": True, "bot": bot.status(),
                            "settings": engine.settings.public().get("telegram", {})})
                return
            self._json({"ok": True, "bot": bot.status()})
            return

        if parsed.path == "/api/llm/setup":
            action = (payload.get("action") or "").strip()
            brain = engine.brain
            if action == "ensure":
                self._json(brain.ensure_server())
            elif action == "pull":
                model = (payload.get("model") or "").strip() or brain.setup().get("suggested") or "qwen2.5:7b"
                threading.Thread(target=brain.pull, args=(model,), daemon=True).start()
                self._json({"ok": True, "model": model, "started": True})
            else:
                self._json({"ok": False, "error": "action: ensure | pull"}, 400)
            return

        if parsed.path == "/api/llm/start":
            executable = shutil.which("ollama")
            if not executable:
                self._json({"ok": False, "error": "ollama not found in PATH"}, 404)
                return
            try:
                kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if system.IS_WINDOWS:
                    kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
                else:
                    kwargs["start_new_session"] = True
                subprocess.Popen([executable, "serve"], **kwargs)
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
                return
            time.sleep(1.6)
            self._json({"ok": True, "status": engine.brain.status(force=True)})
            return

        self._json({"error": "not found"}, 404)


def build(port: int | None = None):
    memory = Memory()
    settings = Settings()
    engine = Engine(memory, settings)
    local_tts.ensure_async()
    telegram = TelegramBot(engine, settings)
    engine.notifiers.append(telegram.broadcast)
    start = port or PORT
    last_error = None
    for candidate in range(start, start + 20):
        try:
            server = FridayServer((HOST, candidate), engine, telegram)
            return server, engine
        except OSError as exc:
            last_error = exc
    raise SystemExit(f"Cannot bind a port: {last_error}")


def _maybe_start_telegram(server: FridayServer) -> None:
    """Auto-start the bot when it is enabled and a token is configured."""
    bot = getattr(server, "telegram", None)
    if bot is None:
        return
    config = server.engine.settings.section("telegram")
    if not (config.get("enabled") and bot.token):
        return
    result = bot.start()
    if result.get("ok"):
        _log(f"Telegram bot @{(result.get('status') or {}).get('username', '?')} online")
    else:
        _log(f"Telegram bot failed: {result.get('error')}")


LOG_PATH = Path(__file__).resolve().parent / "data" / "friday.log"


def _acquire_instance_lock() -> bool:
    """Two servers would poll the same bot token and answer twice."""
    lock = DATA_DIR / "friday.pid"
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid:
        try:
            os.kill(pid, 0)
        except OSError:
            pid = 0
        if pid and pid != os.getpid():
            print(f"F.R.I.D.A.Y. уже запущена (pid {pid}). "
                  f"Закрой её: kill {pid}", flush=True)
            return False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
    except OSError:
        return True
    atexit.register(lambda: lock.unlink(missing_ok=True))
    return True


def _log(message: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def doctor() -> int:
    """Print an environment health report. Exit code 0 when everything is fine."""
    from core.config import DATA_DIR, DRY_RUN
    from core import catalog

    problems = 0
    print(f"F.R.I.D.A.Y. doctor · {system.LABEL} · Python {sys.version.split()[0]}")

    def line(ok: bool, title: str, detail: str = "", optional: bool = False) -> None:
        nonlocal problems
        if ok:
            mark = "OK  "
        elif optional:
            mark = "WARN"
        else:
            mark = "FAIL"
            problems += 1
        print(f"  [{mark}] {title}" + (f" — {detail}" if detail else ""))

    line(sys.version_info >= (3, 10), "Python 3.10+", sys.version.split()[0])
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        line(True, "каталог data", str(DATA_DIR))
    except OSError as exc:
        line(False, "каталог data", f"{type(exc).__name__}: {exc}")
    try:
        memory = Memory()
        line(True, "память", f"{memory.data['stats'].get('commands', 0)} команд, "
                             f"{len(memory.corrections)} правок")
    except Exception as exc:
        line(False, "память", f"{type(exc).__name__}: {exc}")
    try:
        settings = Settings()
        line(True, "настройки", settings.path.name)
    except Exception as exc:
        settings = None
        line(False, "настройки", f"{type(exc).__name__}: {exc}")

    kind = local_tts.engine()
    line(bool(kind), "локальный голос", local_tts.engine_label() or "нет")
    line(tts.available(), "edge-tts (сеть)", "установлен" if tts.available() else "не установлен (опционально)",
         optional=True)
    if system.IS_MAC:
        line(bool(local_tts.say_voice()), "голос macOS say", local_tts.say_voice() or "русский голос не найден",
             optional=True)
    line(bool(catalog.start_menu_links()) or system.IS_WINDOWS, "приложения",
         f"{len(catalog.start_menu_links())} найдено")

    if settings is not None:
        engine = Engine(Memory(), settings)
        status = engine.brain.status(force=True)
        line(bool(status.get("online")), "LLM", f"{status.get('provider')} · "
             f"{status.get('model') or 'нет модели'}" + (f" · {status.get('error')}" if status.get("error") else ""),
             optional=True)
        bot_token = (settings.section("telegram").get("token") or "").strip()
        has_token = bool(bot_token or os.environ.get("FRIDAY_TELEGRAM_TOKEN"))
        line(has_token, "Telegram-токен", "задан" if has_token else "не задан (опционально)", optional=True)

    try:
        items = system.recent_vscode(1)
        line(True, "история VS Code", f"{len(items)} записей")
    except Exception as exc:
        line(False, "история VS Code", f"{type(exc).__name__}: {exc}")

    try:
        probe = FridayServer((HOST, 0), Engine(Memory(), Settings()))
        probe.server_close()
        line(True, "сокет", "можно занять порт")
    except Exception as exc:
        line(False, "сокет", f"{type(exc).__name__}: {exc}")

    if DRY_RUN:
        print("  [INFO] FRIDAY_DRY_RUN=1 — команды не выполняются по-настоящему")
    print(f"\nИтог: {'всё в порядке' if problems == 0 else f'{problems} проблем(ы)'}")
    return 0 if problems == 0 else 1


def main() -> None:
    if not _acquire_instance_lock():
        return
    parser = argparse.ArgumentParser(description="FRIDAY local assistant")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--doctor", action="store_true", help="проверить окружение и выйти")
    args = parser.parse_args()
    if args.doctor:
        raise SystemExit(doctor())
    server, _ = build(args.port)
    # Ollama may be installed but not running: fix that in the background so
    # the first "поговорим" does not fail for a trivial reason.
    threading.Thread(target=server.engine.brain.ensure_server, daemon=True).start()
    port = server.server_address[1]
    url = f"http://{HOST}:{port}"
    _log(f"F.R.I.D.A.Y. online: {url}  [{system.LABEL}]")
    # getMe is a network call: never block the UI on it.
    threading.Thread(target=_maybe_start_telegram, args=(server,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("\nOffline.")
    finally:
        bot = getattr(server, "telegram", None)
        if bot and bot.running:
            bot.stop()
        server.server_close()


if __name__ == "__main__":
    main()
