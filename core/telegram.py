"""Telegram bot for FRIDAY.

Long polling over the Bot API with no third-party dependencies. Besides plain
commands it uses the richer parts of the API: HTML formatting, inline
keyboards, callback buttons, typing status, reactions, photos, voice replies
and the new message widgets (Bot API 10.4, ``sendWidget``) with a graceful
fallback to a normal message on servers that do not support them yet.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from shutil import which

from . import system

API_ROOT = "https://api.telegram.org"

MAIN_KEYBOARD = [
    [{"text": "↻ Ещё раз", "callback_data": "again"},
     {"text": "📋 Задачи", "callback_data": "tasks"}],
    [{"text": "📸 Скриншот", "callback_data": "shot"},
     {"text": "🖥 Статус", "callback_data": "status"}],
]

COMMANDS = [
    {"command": "status", "description": "состояние ядра и модели"},
    {"command": "memory", "description": "что я о тебе помню"},
    {"command": "tasks", "description": "последние задачи"},
    {"command": "card", "description": "карточка-виджет со статусом"},
    {"command": "shot", "description": "скриншот экрана"},
    {"command": "help", "description": "что я умею"},
]


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _chunks(text: str, size: int = 3800) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, current = [], ""
    for line in text.splitlines(True):
        if len(current) + len(line) > size:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts


class TelegramBot:
    def __init__(self, engine, settings):
        self.engine = engine
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._running = False
        self._error = ""
        self._username = ""
        self._last_chat: int | None = None
        self._seen: dict[int, float] = {}
        self._last_text: dict[int, str] = {}
        self._pending_media: dict[int, str] = {}
        self._message_ids: dict[int, int] = {}
        self._offset = 0
        self._widget_state = ""

    # ------------------------------------------------------------- config
    @property
    def config(self) -> dict:
        return self.settings.section("telegram")

    @property
    def token(self) -> str:
        return (os.environ.get("FRIDAY_TELEGRAM_TOKEN") or self.config.get("token") or "").strip()

    @property
    def allow_ids(self) -> set[int]:
        raw = self.config.get("allow_ids") or []
        return {int(item) for item in raw if str(item).strip().lstrip("-").isdigit()}

    @property
    def running(self) -> bool:
        return self._running

    def _note(self, message: str) -> None:
        self._error = message

    def status(self) -> dict:
        return {
            "configured": bool(self.token),
            "running": self._running,
            "username": self._username,
            "last_chat": self._last_chat,
            "error": self._error,
            "allow_ids": sorted(self.allow_ids),
            "open_access": not self.allow_ids,
            "widgets": self._widget_state,
        }

    # ---------------------------------------------------------- transport
    def _api(self, method: str, payload: dict | None = None, timeout: float = 35.0,
             files: list[tuple[str, str, bytes, str]] | None = None) -> dict:
        token = self.token
        if not token:
            return {"ok": False, "description": "no token"}
        url = f"{API_ROOT}/bot{token}/{method}"
        try:
            if files:
                boundary = "----friday" + uuid.uuid4().hex
                body = bytearray()
                for key, value in (payload or {}).items():
                    if value is None:
                        continue
                    body += f"--{boundary}\r\n".encode()
                    body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
                    body += (json.dumps(value) if isinstance(value, (dict, list)) else str(value)).encode()
                    body += b"\r\n"
                for field, filename, data, mime in files:
                    body += f"--{boundary}\r\n".encode()
                    body += (
                        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                        f"Content-Type: {mime}\r\n\r\n"
                    ).encode()
                    body += data
                    body += b"\r\n"
                body += f"--{boundary}--\r\n".encode()
                request = urllib.request.Request(
                    url, data=bytes(body),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                    method="POST",
                )
            else:
                data = json.dumps(payload or {}).encode("utf-8")
                request = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"}, method="POST"
                )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                return {"ok": False, "description": f"HTTP {exc.code}"}
        except (OSError, urllib.error.URLError) as exc:
            return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------ sending
    def send(self, chat_id: int, text: str, keyboard: list | None = None) -> bool:
        ok = True
        for chunk in _chunks(text or "…"):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if keyboard:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            result = self._api("sendMessage", payload)
            if not result.get("ok"):
                # Unbalanced HTML must not lose the message: retry as plain text.
                result = self._api("sendMessage", {"chat_id": chat_id,
                                                   "text": re.sub(r"<[^>]+>", "", chunk)})
            ok = ok and bool(result.get("ok"))
            if not result.get("ok"):
                self._note(result.get("description", "send failed"))
        return ok

    def send_photo(self, chat_id: int, path: str, caption: str = "",
                   keyboard: list | None = None) -> bool:
        try:
            data = Path(path).read_bytes()
        except OSError:
            return False
        payload = {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = self._api("sendPhoto", payload, timeout=90,
                           files=[("photo", Path(path).name, data, "image/png")])
        return bool(result.get("ok"))

    def send_document(self, chat_id: int, path: str, caption: str = "") -> bool:
        try:
            data = Path(path).read_bytes()
        except OSError:
            return False
        result = self._api(
            "sendDocument",
            {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"},
            timeout=90,
            files=[("document", Path(path).name, data, "application/octet-stream")],
        )
        return bool(result.get("ok"))

    def send_voice(self, chat_id: int, text: str) -> bool:
        """Speak a reply: local TTS, then ogg/opus when ffmpeg is available."""
        try:
            from . import local_tts

            audio, _mime = local_tts.synthesize(text[:600])
        except Exception:
            return False
        if not audio:
            return False
        workdir = self.engine.settings.path.parent / "tts_cache"
        workdir.mkdir(parents=True, exist_ok=True)
        source = workdir / f"tg-{uuid.uuid4().hex[:8]}.wav"
        source.write_bytes(audio)
        ffmpeg = which("ffmpeg")
        if not ffmpeg:
            result = self._api("sendAudio", {"chat_id": chat_id, "title": "FRIDAY"},
                               timeout=90, files=[("audio", source.name, audio, "audio/wav")])
            return bool(result.get("ok"))
        target = source.with_suffix(".ogg")
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                 "-c:a", "libopus", "-b:a", "48k", str(target)],
                capture_output=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if not target.exists():
            return False
        result = self._api("sendVoice", {"chat_id": chat_id}, timeout=90,
                           files=[("voice", "voice.ogg", target.read_bytes(), "audio/ogg")])
        return bool(result.get("ok"))

    def send_widget(self, chat_id: int, widget_html: str, caption: str = "",
                    keyboard: list | None = None) -> bool:
        """Bot API 10.4 message widget; falls back to a formatted message."""
        payload = {"chat_id": chat_id, "html": widget_html, "width": 0, "height": 0}
        if caption:
            payload["caption"] = re.sub(r"<[^>]+>", "", caption)
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        result = self._api("sendWidget", payload, timeout=40)
        if result.get("ok"):
            self._widget_state = "ok"
            return True
        self._widget_state = (result.get("description") or "unsupported")[:90]
        return self.send(chat_id, caption or "Карточка", keyboard)

    def broadcast(self, title: str, message: str | None = None) -> int:
        if message is None:
            message, title = title, ""
        text = f"🔔 <b>{_esc(title)}</b>\n{_esc(message)}" if title else _esc(message)
        chats = sorted(self.allow_ids) or ([self._last_chat] if self._last_chat else [])
        sent = 0
        for chat_id in chats:
            if self.send(chat_id, text):
                sent += 1
        return sent

    # ------------------------------------------------------------- control
    def start(self, token: str | None = None) -> dict:
        if token:
            self.settings.update("telegram", {"token": token.strip()})
        if not self.token:
            return {"ok": False, "error": "нет токена", "bot": self.status()}
        probe = self._api("getMe", timeout=15)
        if not probe.get("ok"):
            self._note(probe.get("description", "getMe failed"))
            return {"ok": False, "error": self._error, "bot": self.status()}
        self._username = (probe.get("result") or {}).get("username", "")
        self._api("setMyCommands", {"commands": COMMANDS})
        self._error = ""
        self._running = True
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="friday-telegram", daemon=True)
            self._thread.start()
        return {"ok": True, "bot": self.status()}

    def stop(self) -> dict:
        self._running = False
        return {"ok": True, "bot": self.status()}

    # --------------------------------------------------------------- loop
    def _loop(self) -> None:
        while self._running:
            try:
                result = self._api(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25,
                     "allowed_updates": ["message", "callback_query"]},
                    timeout=40,
                )
            except Exception as exc:
                self._note(f"{type(exc).__name__}: {exc}")
                time.sleep(3)
                continue
            if not result.get("ok"):
                self._note(result.get("description", "getUpdates failed"))
                time.sleep(3)
                continue
            for update in result.get("result") or []:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                try:
                    self._handle_update(update)
                except Exception as exc:
                    self._note(f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ updates
    def _authorized(self, chat_id: int) -> bool:
        allowed = self.allow_ids
        return not allowed or int(chat_id) in allowed

    def _handle_update(self, update: dict) -> None:
        if update.get("callback_query"):
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        if (message.get("from") or {}).get("is_bot"):
            return
        self._last_chat = int(chat_id)
        if not self._authorized(chat_id):
            self.send(chat_id, f"Доступ закрыт. Мой chat id: {chat_id} — добавь его в allow_ids.")
            return
        now = time.time()
        if now - self._seen.get(int(chat_id), 0) < 0.6:
            return
        self._seen[int(chat_id)] = now
        self._message_ids[int(chat_id)] = int(message.get("message_id") or 0)
        self._api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        self._react(chat_id, message.get("message_id"), "👀")
        # Answer in a worker thread: a slow LLM call must not freeze polling.
        threading.Thread(target=self._answer, args=(int(chat_id), text),
                         name="friday-tg-reply", daemon=True).start()

    def _handle_callback(self, callback: dict) -> None:
        data = (callback.get("data") or "").strip()
        message = callback.get("message") or {}
        chat_id = ((message.get("chat") or {}).get("id")) or self._last_chat
        if not chat_id or not self._authorized(chat_id):
            return
        self._api("answerCallbackQuery", {"callback_query_id": callback.get("id"), "text": "Готово"})
        if data == "again":
            text = self._last_text.get(int(chat_id))
            if text:
                self._answer(int(chat_id), text)
        elif data == "tasks":
            self._answer(int(chat_id), "/tasks")
        elif data == "shot":
            self._answer(int(chat_id), "сделай скриншот")
        elif data == "status":
            self._answer(int(chat_id), "/status")

    def _react(self, chat_id: int, message_id: int | None, emoji: str) -> None:
        if not message_id:
            return
        self._api("setMessageReaction", {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        })

    def _answer(self, chat_id: int, text: str) -> None:
        try:
            if text.strip().lower() in ("/card", "карточка", "виджет"):
                self._send_card(chat_id)
                return
            reply = self._respond(chat_id, text)
        except Exception as exc:
            self._note(f"{type(exc).__name__}: {exc}")
            self.send(chat_id, "Не получилось обработать команду. Попробуй ещё раз.", MAIN_KEYBOARD)
            return
        photo = self._pending_media.pop(chat_id, None)
        if photo and Path(photo).exists():
            if self.send_photo(chat_id, photo,
                               f"<b>Скриншот</b>\n<code>{_esc(Path(photo).name)}</code>", MAIN_KEYBOARD):
                reply = ""
        if reply:
            self.send(chat_id, reply, MAIN_KEYBOARD)
        if self.config.get("voice"):
            self.send_voice(chat_id, re.sub(r"<[^>]+>", "", reply or "Готово")[:400])
        self._react(chat_id, self._message_ids.get(chat_id), "✅")

    # ------------------------------------------------------------- replies
    def _respond(self, chat_id: int, text: str) -> str:
        """Pure text answer: used by tests and by ``_answer``."""
        text = (text or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if command in ("/start", "/help"):
            return (
                "<b>F.R.I.D.A.Y. на связи.</b>\n\n"
                "Пиши обычной фразой — выполню и пришлю результат:\n"
                "<code>открой ютуб</code>, <code>статус системы</code>, "
                "<code>напомни через 10 минут позвонить</code>, "
                "<code>сделай скриншот</code>, <code>курс доллара</code>, "
                "<code>добавь задачу купить хлеб</code>.\n\n"
                f"Твой chat id: {chat_id}\n"
                "Команды: /status, /memory, /tasks, /card, /shot, /id.\n"
                "Кнопки под сообщением тоже работают."
            )
        if command == "/id":
            return f"chat id: <code>{chat_id}</code>"
        if command == "/status":
            state = self.engine.state(0)
            llm = self.engine.brain.status()
            memory = state["memory"]
            return (
                "<b>Статус</b>\n"
                f"Ядро: онлайн · {_esc(system.LABEL)}\n"
                f"Активных задач: {state['running']}\n"
                f"LLM: {'онлайн · ' + _esc(llm.get('model') or llm.get('provider', '')) if llm.get('online') else 'офлайн'}\n"
                f"Память: {len(memory.get('likes', []))} предпочтений, "
                f"{len(memory.get('facts', []))} фактов, {len(memory.get('corrections', []))} правок\n"
                f"Напоминаний: {self.engine.pending_reminders()}"
            )
        if command == "/card":
            state = self.engine.state(0)
            llm = self.engine.brain.status()
            return (
                "<b>F.R.I.D.A.Y. · пульт</b>\n"
                f"Задач в работе: {state['running']} · напоминаний: {state['reminders']}\n"
                f"LLM: {'онлайн' if llm.get('online') else 'офлайн'}"
            )
        if command == "/memory":
            return _esc(self.engine.memory.describe())
        if command == "/tasks":
            tasks = self.engine.state(0)["tasks"][-8:]
            if not tasks:
                return "Задач пока нет."
            lines = []
            for task in tasks:
                mark = "✅" if task["status"] == "done" else "❌" if task["status"] == "error" else "…"
                detail = task.get("speech") or task.get("error") or task["status"]
                lines.append(f"{mark} <b>{_esc(task['label'])}</b>\n{_esc(detail)}")
            return "\n\n".join(lines)
        if command:
            return "Не знаю такую команду. Список: /status, /memory, /tasks, /card, /shot."

        self._last_text[chat_id] = text
        tasks = self.engine.handle_text(text, source="telegram")
        if not tasks:
            return "Пустая команда."
        blocks = []
        for task in tasks:
            if task["status"] == "error":
                blocks.append(f"❌ <b>{_esc(task['label'])}</b>\n"
                              f"{_esc(task.get('error') or task.get('speech'))}")
            else:
                blocks.append(f"<b>{_esc(task['label'])}</b>\n{_esc(task.get('speech'))}")
            data = task.get("data") or {}
            if data.get("photo") and data.get("file"):
                self._pending_media[chat_id] = data["file"]
        return "\n\n".join(blocks)

    # --------------------------------------------------------------- card
    def _send_card(self, chat_id: int) -> None:
        state = self.engine.state(0)
        memory = state["memory"]
        llm = self.engine.brain.status()
        widget = f"""<div style="font-family:-apple-system,system-ui,sans-serif;padding:14px;border-radius:14px;background:linear-gradient(160deg,#0b1220,#16233a);color:#e6edf3">
<div style="font-size:11px;letter-spacing:2px;opacity:.6">F.R.I.D.A.Y.</div>
<div style="font-size:19px;font-weight:700;margin:4px 0 10px">Пульт</div>
<div style="display:flex;gap:8px;margin-bottom:8px">
<div style="flex:1;background:#ffffff12;border-radius:10px;padding:8px"><div style="font-size:10px;opacity:.6">ЗАДАЧИ</div><div style="font-size:17px">{state['running']}</div></div>
<div style="flex:1;background:#ffffff12;border-radius:10px;padding:8px"><div style="font-size:10px;opacity:.6">ПАМЯТЬ</div><div style="font-size:17px">{len(memory.get('likes', [])) + len(memory.get('facts', []))}</div></div>
<div style="flex:1;background:#ffffff12;border-radius:10px;padding:8px"><div style="font-size:10px;opacity:.6">LLM</div><div style="font-size:12px">{'ON' if llm.get('online') else 'OFF'}</div></div>
</div>
<div style="font-size:11px;opacity:.65">напоминаний: {state['reminders']}</div>
</div>"""
        caption = (
            f"<b>F.R.I.D.A.Y.</b>\nЗадач в работе: {state['running']} · "
            f"напоминаний: {state['reminders']}\n"
            f"LLM: {'онлайн' if llm.get('online') else 'офлайн'}"
        )
        if not self.send_widget(chat_id, widget, caption, MAIN_KEYBOARD):
            self.send(chat_id, caption, MAIN_KEYBOARD)
