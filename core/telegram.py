"""Telegram bot for F.R.I.D.A.Y. — stdlib only, no dependencies.

The bot long-polls the Telegram Bot API and feeds every message into the same
``Engine`` the web UI uses, so memory, corrections, reminders and the LLM are
shared between the browser and the phone.

Setup
-----
1. Talk to @BotFather, create a bot, copy the token.
2. Put it into ``data/settings.json`` → ``telegram.token`` (or export
   ``FRIDAY_TELEGRAM_TOKEN``), or paste it into Настройки → Telegram.
3. Send ``/start`` to the bot. The answer contains the chat id; put it into
   ``telegram.allow_ids`` if you want to lock the bot to your own account.

Commands: /start, /help, /status, /memory, /id, /tasks. Anything else is
executed as a normal FRIDAY command.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_TEXT = 3800
POLL_TIMEOUT = 25


class TelegramBot:
    def __init__(self, engine, settings):
        self.engine = engine
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._offset = 0
        self._me: dict = {}
        self._error = ""
        self._log: list[str] = []
        self._started_at: float | None = None
        self._last_chat: int | None = None
        self._seen: dict[int, float] = {}

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
        if isinstance(raw, str):
            raw = raw.replace(";", ",").split(",")
        allowed = set()
        for item in raw:
            try:
                allowed.add(int(str(item).strip()))
            except (TypeError, ValueError):
                continue
        return allowed

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _note(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._log.append(f"{stamp} {message}")
            self._log = self._log[-40:]

    def status(self) -> dict:
        with self._lock:
            return {
                "configured": bool(self.token),
                "running": self.running,
                "username": (self._me or {}).get("username", ""),
                "name": (self._me or {}).get("first_name", ""),
                "error": self._error,
                "started_at": self._started_at,
                "allow_ids": sorted(self.allow_ids),
                "open_access": not self.allow_ids,
                "log": list(self._log[-12:]),
            }

    # ------------------------------------------------------------- transport
    def _api(self, method: str, payload: dict | None = None, timeout: float = 35.0) -> dict:
        token = self.token
        if not token:
            raise RuntimeError("telegram token is not set")
        url = f"https://api.telegram.org/bot{token}/{method}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
                raise RuntimeError(body.get("description") or f"HTTP {exc.code}") from exc
            except (ValueError, OSError):
                raise RuntimeError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"network: {exc.reason}") from exc

    def send(self, chat_id: int, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        ok = True
        for start in range(0, len(text), MAX_TEXT):
            chunk = text[start:start + MAX_TEXT]
            try:
                self._api("sendMessage", {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                })
            except Exception as exc:  # keep the bot alive on a single failure
                self._error = f"{type(exc).__name__}: {exc}"
                self._note(f"send failed: {self._error}")
                ok = False
            time.sleep(0.15)
        return ok

    def broadcast(self, text: str) -> int:
        """Push a message to every allowed chat (used for reminders)."""
        targets = sorted(self.allow_ids) or ([self._last_chat] if self._last_chat else [])
        sent = 0
        for chat_id in targets:
            if self.send(chat_id, text):
                sent += 1
        return sent

    # ------------------------------------------------------------- lifecycle
    def start(self, token: str | None = None) -> dict:
        if token:
            self.settings.update("telegram", {"token": token, "enabled": True})
        if not self.token:
            self._error = "не задан токен бота"
            return {"ok": False, "error": self._error, "status": self.status()}
        if self.running:
            return {"ok": True, "status": self.status()}
        self._stop.clear()
        self._error = ""
        try:
            me = self._api("getMe", timeout=15)
            if not me.get("ok"):
                raise RuntimeError(me.get("description") or "getMe failed")
            self._me = me.get("result") or {}
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._note(f"getMe failed: {self._error}")
            return {"ok": False, "error": self._error, "status": self.status()}

        self._thread = threading.Thread(target=self._loop, name="friday-telegram", daemon=True)
        self._thread.start()
        self._started_at = time.time()
        self._note(f"запущен как @{self._me.get('username', '?')}")
        return {"ok": True, "status": self.status()}

    def stop(self) -> dict:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=POLL_TIMEOUT + 5)
        self._thread = None
        self._started_at = None
        self._note("остановлен")
        return {"ok": True, "status": self.status()}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                response = self._api(
                    "getUpdates",
                    {"offset": self._offset, "timeout": POLL_TIMEOUT,
                     "allowed_updates": ["message"]},
                    timeout=POLL_TIMEOUT + 10,
                )
                if not response.get("ok"):
                    raise RuntimeError(response.get("description") or "getUpdates failed")
                for update in response.get("result") or []:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    try:
                        self._handle_update(update)
                    except Exception as exc:  # one bad message must not kill the loop
                        self._error = f"{type(exc).__name__}: {exc}"
                        self._note(f"update failed: {self._error}")
                self._error = ""
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                self._note(f"poll failed: {self._error}")
                if self._stop.wait(6.0):
                    break

    # ------------------------------------------------------------- messages
    def _authorized(self, chat_id: int) -> bool:
        allowed = self.allow_ids
        return not allowed or chat_id in allowed

    def _handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text:
            return
        if message.get("from", {}).get("is_bot"):
            return
        self._last_chat = int(chat_id)

        if not self._authorized(chat_id):
            self.send(chat_id, f"Доступ закрыт. Мой chat id: {chat_id} — добавь его в allow_ids.")
            return

        # Simple flood guard: one command per chat per 600 ms.
        now = time.time()
        if now - self._seen.get(int(chat_id), 0) < 0.6:
            return
        self._seen[int(chat_id)] = now

        # Answer in a worker thread: a slow LLM call must not freeze polling,
        # otherwise every following Telegram message waits in the queue.
        threading.Thread(
            target=self._answer, args=(int(chat_id), text), name="friday-tg-reply", daemon=True
        ).start()

    def _answer(self, chat_id: int, text: str) -> None:
        try:
            reply = self._respond(chat_id, text)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._note(f"handle failed: {self._error}")
            reply = "Не получилось обработать команду. Попробуй ещё раз."
        if reply:
            self.send(chat_id, reply)

    def _respond(self, chat_id: int, text: str) -> str:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            return (
                "F.R.I.D.A.Y. на связи.\n\n"
                "Пиши обычной фразой — я выполню команду и пришлю результат:\n"
                "«открой ютуб», «статус системы», «напомни через 10 минут позвонить», "
                "«запомни, что мне нравится рок», «что ты умеешь».\n\n"
                f"Твой chat id: {chat_id}\n"
                "Команды: /status, /memory, /tasks, /id."
            )
        if command == "/id":
            return f"chat id: {chat_id}"
        if command == "/status":
            state = self.engine.state(0)
            llm = self.engine.brain.status()
            memory = state["memory"]
            return (
                f"Ядро: онлайн\n"
                f"Активных задач: {state['running']}\n"
                f"LLM: {'онлайн · ' + (llm.get('model') or llm.get('provider', '')) if llm.get('online') else 'офлайн'}\n"
                f"Память: {len(memory.get('likes', []))} предпочтений, "
                f"{len(memory.get('facts', []))} фактов, {len(memory.get('corrections', []))} правок\n"
                f"Напоминаний в очереди: {self.engine.pending_reminders()}"
            )
        if command == "/memory":
            return self.engine.memory.describe()
        if command == "/tasks":
            tasks = self.engine.state(0)["tasks"][-8:]
            if not tasks:
                return "Задач пока нет."
            lines = []
            for task in tasks:
                mark = "✓" if task["status"] == "done" else "✗" if task["status"] == "error" else "…"
                lines.append(f"{mark} {task['label']}: {task.get('speech') or task.get('error') or task['status']}")
            return "\n".join(lines)
        if command:
            return "Не знаю такой команды. /help — что я умею."

        results = self.engine.handle_text(text, source="telegram")
        if not results:
            return "Пустая команда."
        lines = []
        for task in results:
            if task["status"] == "error" and task.get("error"):
                lines.append(f"Не получилось: {task['error']}")
            else:
                lines.append(task.get("speech") or task.get("detail") or "Готово.")
        return "\n\n".join(lines)
