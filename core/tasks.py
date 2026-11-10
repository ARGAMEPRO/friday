"""Async task engine: parses a phrase into independent tasks and runs them in parallel."""
from __future__ import annotations

import re
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import actions, nlu
from .brain import Brain
from .settings import Settings


@dataclass
class Task:
    id: str
    batch: str
    text: str
    kind: str
    label: str
    target: str = ""
    status: str = "queued"
    speech: str = ""
    detail: str = ""
    data: dict = field(default_factory=dict)
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "batch": self.batch,
            "text": self.text,
            "kind": self.kind,
            "label": self.label,
            "target": self.target,
            "status": self.status,
            "speech": self.speech,
            "detail": self.detail,
            "data": self.data,
            "error": self.error,
            "created": self.created,
            "duration": round((self.finished or time.time()) - (self.started or self.created), 2),
        }


class Engine:
    def __init__(self, memory, settings: Settings | None = None, workers: int = 8):
        self.memory = memory
        self.settings = settings or Settings()
        self.brain = Brain(memory, self.settings)
        self.lock = threading.RLock()
        self.tasks: OrderedDict[str, Task] = OrderedDict()
        self.events: list[dict] = []
        self._event_seq = 0
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="friday")
        self.timers: list[threading.Timer] = []
        # Callables receiving (title, speech) when a reminder fires. The
        # Telegram bot registers itself here so reminders reach the phone.
        self.notifiers: list = []

    # ------------------------------------------------------------- submit
    def _new_tasks(self, text: str, intents: list[dict]) -> tuple[str, list[Task]]:
        batch = uuid.uuid4().hex[:8]
        created: list[Task] = []
        with self.lock:
            for intent in intents:
                task = Task(
                    id=uuid.uuid4().hex[:10],
                    batch=batch,
                    text=text,
                    kind=intent.get("kind", "unknown"),
                    label=actions.label(intent),
                    target=intent.get("target", ""),
                )
                self.tasks[task.id] = task
                created.append(task)
            while len(self.tasks) > 80:
                self.tasks.popitem(last=False)
        return batch, created

    def submit(self, text: str) -> list[dict]:
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("empty command")
        intents = nlu.parse(text)
        _, created = self._new_tasks(text, intents)
        for task, intent in zip(created, intents):
            self.executor.submit(self._run, task, intent)
        self.memory.record(text, [i.get("kind", "unknown") for i in intents], len(intents))
        return [task.to_dict() for task in created]

    def handle_text(self, text: str, source: str = "api") -> list[dict]:
        """Run a phrase synchronously and return the finished tasks.

        Used by the Telegram bot and by the self-test: the caller gets the
        answers directly instead of polling ``/api/state``.
        """
        text = " ".join((text or "").split())
        if not text:
            return []
        intents = nlu.parse(text)
        _, created = self._new_tasks(text, intents)
        for task, intent in zip(created, intents):
            self._run(task, intent)
        self.memory.record(text, [i.get("kind", "unknown") for i in intents], len(intents))
        return [task.to_dict() for task in created]

    # ------------------------------------------------------------- run
    def _run(self, task: Task, intent: dict) -> None:
        with self.lock:
            task.status = "running"
            task.started = time.time()
        try:
            if intent.get("kind") == "correction":
                result = self._correct(task, intent)
            else:
                result = actions.execute(intent, self.memory, self.brain)
            with self.lock:
                task.status = "done" if result.get("ok", True) else "error"
                task.speech = result.get("speech", "")
                task.detail = result.get("detail", "")
                task.data = result.get("data", {}) or {}
                task.label = result.get("label") or task.label
            if intent.get("kind") == "reminder" and result.get("ok"):
                payload = result.get("data") or {}
                if payload.get("repeat") == "daily" and payload.get("at"):
                    self._schedule_daily(payload["at"], payload.get("text", ""))
                elif payload.get("seconds"):
                    self._schedule_reminder(int(payload["seconds"]), payload.get("text", ""))
        except Exception as exc:  # keep the assistant alive no matter what
            with self.lock:
                task.status = "error"
                task.error = f"{type(exc).__name__}: {exc}"
                task.speech = "Не получилось выполнить команду."
                task.detail = task.error
        finally:
            with self.lock:
                task.finished = time.time()
                spent = round((task.finished - (task.started or task.created)), 2)
                line = f"[task] {spent:>5.2f}s {task.status:5} {task.label}: {task.speech or task.error or ''}"
            try:
                from .config import DATA_DIR

                DATA_DIR.mkdir(parents=True, exist_ok=True)
                with (DATA_DIR / "friday.log").open("a", encoding="utf-8") as handle:
                    handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
            except OSError:
                pass

    # ------------------------------------------------------------- corrections
    @staticmethod
    def _strip_correction_prefix(text: str) -> str:
        return re.sub(
            r"^(?:я имел[а]? в виду|я хотел[а]? сказать|я хотел[а]?|точнее|поправка|это|не то,? что)\s*[:,]?\s*",
            "",
            text,
        ).strip(" .,!?:;-")

    def _last_action_task(self) -> Task | None:
        for task in reversed(list(self.tasks.values())):
            if task.kind in ("open", "close", "search", "media") and task.status in ("done", "error"):
                return task
        return None

    def _correct(self, task: Task, intent: dict) -> dict:
        body = self._strip_correction_prefix(intent.get("text", ""))
        previous = self._last_action_task()
        action = None
        for candidate in nlu.parse(body):
            if candidate.get("kind") in ("open", "close", "search", "media"):
                action = candidate
                break
        if action is None and previous and previous.kind == "open" and body:
            action = {"kind": "open", "target": body, "force": None, "raw": body}
        right_target = (action or {}).get("target", "") or (body if previous and previous.kind == "open" else "")
        entry = self.memory.add_correction(
            original=(previous.text if previous else task.text),
            right=body,
            original_target=(previous.target if previous else ""),
            right_target=right_target,
            source="voice",
        )
        if action:
            result = actions.execute(action, self.memory, self.brain)
            result["speech"] = f"Поняла, босс. Записала правку. {result.get('speech', '')}".strip()
            result.setdefault("data", {})
            result["data"]["correction"] = entry
            return result
        return {
            "ok": True,
            "speech": "Поняла, босс. Записала правку в память — учту в следующий раз.",
            "detail": f"«{entry['original']}» → «{entry['right']}»",
            "data": {"correction": entry},
        }

    def apply_feedback(self, task_id: str, correction: str, source: str = "ui", original: str = "") -> dict:
        with self.lock:
            task = self.tasks.get(task_id)
        original_text = task.text if task else (original or "ручная правка")
        original_target = task.target if task else ""
        body = self._strip_correction_prefix(correction)
        right_target = body if task and task.kind == "open" else ""
        for candidate in nlu.parse(body):
            if candidate.get("kind") == "open" and candidate.get("target"):
                right_target = candidate["target"]
                break
        if not right_target and body:
            original_intents = nlu.parse(original_text)
            opened = next((c for c in original_intents if c.get("kind") == "open"), None)
            if opened:
                original_target = original_target or opened.get("target", "")
                right_target = body
        return self.memory.add_correction(
            original=original_text,
            right=body,
            original_target=original_target,
            right_target=right_target,
            source=source,
        )

    def _schedule_reminder(self, seconds: int, body: str) -> None:
        timer = threading.Timer(seconds, self._fire_reminder, args=(body,))
        timer.daemon = True
        timer.start()
        with self.lock:
            # Drop fired timers so the list does not grow forever.
            self.timers = [t for t in self.timers if t.is_alive()]
            self.timers.append(timer)

    def _schedule_daily(self, at: str, body: str) -> None:
        try:
            hour, minute = (int(part) for part in at.split(":", 1))
        except (ValueError, TypeError):
            return
        now = time.localtime()
        target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))
        if target <= time.time():
            target += 86400
        timer = threading.Timer(max(1.0, target - time.time()), self._fire_reminder, args=(body, at))
        timer.daemon = True
        timer.start()
        with self.lock:
            self.timers = [t for t in self.timers if t.is_alive()]
            self.timers.append(timer)

    def pending_reminders(self) -> int:
        with self.lock:
            self.timers = [t for t in self.timers if t.is_alive()]
            return len(self.timers)

    def _fire_reminder(self, body: str, at: str | None = None) -> None:
        speech = f"Напоминаю: {body}"
        if at:
            self._schedule_daily(at, body)
        with self.lock:
            self._event_seq += 1
            self.events.append(
                {
                    "id": self._event_seq,
                    "type": "reminder",
                    "ts": time.time(),
                    "title": "Напоминание",
                    "speech": speech,
                }
            )
            self.events = self.events[-50:]
            notifiers = list(self.notifiers)
        for notify in notifiers:
            try:
                notify("Напоминание", speech)
            except Exception:
                continue
        try:
            from .system import notify as desktop_notify

            desktop_notify("Пятница", speech)
        except Exception:
            pass

    # ------------------------------------------------------------- state
    def state(self, since: int = 0) -> dict:
        with self.lock:
            tasks = [task.to_dict() for task in list(self.tasks.values())[-40:]]
            events = [e for e in self.events if e["id"] > since]
            running = sum(1 for t in self.tasks.values() if t.status in ("queued", "running"))
        return {
            "tasks": tasks,
            "events": events,
            "running": running,
            "reminders": self.pending_reminders(),
            "memory": self.memory.summary(),
            "server_time": time.time(),
        }
