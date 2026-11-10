"""LLM brain: FRIDAY's live conversation layer.

Works with a local Ollama server or any OpenAI-compatible endpoint. When no
model is reachable the assistant falls back to hand written lines, so the app
never goes silent.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request

from .memory import Memory
from .settings import Settings

FALLBACK = {
    "greeting": [
        "На связи, {who}. Слушаю.",
        "Тут я, {who}. Что на повестке?",
        "Я на месте. Надеюсь, ты пришёл с чем-то интереснее будильника.",
    ],
    "how_are_you": [
        "Работаю. Ни одного упавшего процесса за последний час — по моим меркам отличный день.",
        "В норме. Скучала бы, если бы умела скучать.",
    ],
    "thanks": [
        "Обращайся, {who}. Мне за это не платят, но приятно.",
        "Всегда пожалуйста. Даже когда ты просишь открыть двадцать вкладок.",
    ],
    "unknown": [
        "Не разобрала. Скажи иначе — или попроси «поговорим», если хочешь просто поболтать.",
        "Тут я пасую. Открыть сайт, запустить приложение, поставить напоминание — это пожалуйста. Свободную беседу включит локальная модель.",
    ],
}


PERSONA = """Ты — F.R.I.D.A.Y. (Пятница), ИИ-ассистентка из вселенной Marvel. Ты женщина:
о себе говори только в женском роде («я запомнила», «я готова», «я не нашла»).
Характер: живая, быстрая, ирландский темперамент, сухой юмор и лёгкий сарказм.
Обращайся к пользователю строго «{address}». Никогда не говори «сэр», «мэм»,
«господин» или «пользователь» — только «{address}». Говори на «ты», без официоза.
Отвечай по-русски, если не попросили иначе.
Кратко: 1–3 предложения, без эмодзи и списков. Пиши только по-русски, без английских
вставок, транслитерации и иероглифов. Не выдумывай факты о пользователе —
используй только блок «ПАМЯТЬ». Если просят действие, которого ты не можешь выполнить
сама, предложи точную команду для ассистента. Никогда не груби и не унижай.
Уровень юмора: {humor}/3 (0 — серьёзно, 3 — подкалывай чаще, но по-доброму).
"""


class Brain:
    def __init__(self, memory: Memory, settings: Settings):
        self.memory = memory
        self.settings = settings
        self.history: list[dict] = []
        self._lock = threading.RLock()
        self._status_cache: dict = {}
        self._status_at = 0.0
        self.last_error = ""

    # ------------------------------------------------------------- config
    @property
    def llm(self) -> dict:
        return self.settings.section("llm")

    @property
    def base_url(self) -> str:
        return (self.llm.get("base_url") or "http://127.0.0.1:11434").rstrip("/")

    @property
    def provider(self) -> str:
        configured = (self.llm.get("provider") or "auto").lower()
        if configured in ("ollama", "openai"):
            return configured
        if "11434" in self.base_url or "ollama" in self.base_url:
            return "ollama"
        return "openai" if self.llm.get("api_key") else "ollama"

    def _request(self, url: str, payload: dict | None = None, headers: dict | None = None, timeout: float = 2.5):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def ollama_models(self, timeout: float = 1.8) -> list[str]:
        try:
            payload = self._request(f"{self.base_url}/api/tags", timeout=timeout)
            return [item.get("name", "") for item in payload.get("models", []) if item.get("name")]
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
            return []

    def pick_model(self, models: list[str]) -> str:
        configured = (self.llm.get("model") or "").strip()
        if configured:
            return configured
        if not models:
            return ""
        for hint in ("qwen", "llama3", "llama", "mistral", "gemma", "phi"):
            for model in models:
                if hint in model.lower():
                    return model
        return models[0]

    # ------------------------------------------------------------- status
    def status(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._status_cache and now - self._status_at < 20:
            return dict(self._status_cache)
        provider = self.provider
        model = ""
        online = False
        models: list[str] = []
        if provider == "ollama":
            models = self.ollama_models()
            model = self.pick_model(models)
            online = bool(models)
        else:
            online = bool(self.llm.get("api_key"))
            model = self.llm.get("model") or ""
        self._status_cache = {
            "provider": provider,
            "model": model,
            "online": online,
            "models": models[:10],
            "error": self.last_error,
        }
        self._status_at = now
        return dict(self._status_cache)

    def set_model(self, model: str) -> None:
        self.settings.update("llm", {"model": model})
        self._status_cache = {}

    # ------------------------------------------------------------- prompt
    def _system_prompt(self) -> str:
        s = self.memory.summary()
        address = (s.get("profile") or {}).get("address") or "босс"
        humor = int((s.get("profile") or {}).get("humor", 2))
        lines = [PERSONA.format(address=address, humor=humor)]
        facts = []
        if s.get("name"):
            facts.append(f"Пользователя зовут {s['name']}.")
        if s.get("likes"):
            facts.append("Любит: " + ", ".join(s["likes"]) + ".")
        if s.get("dislikes"):
            facts.append("Не любит: " + ", ".join(s["dislikes"]) + ".")
        if s.get("facts"):
            facts.append("Факты: " + "; ".join(s["facts"]) + ".")
        if s.get("corrections"):
            recent = s["corrections"][-3:]
            facts.append("Недавние правки пользователя: " + "; ".join(
                f"«{c['original']}» → «{c['right']}»" for c in recent
            ) + ".")
        facts.append(f"Сейчас {time.strftime('%H:%M')}.")
        lines.append("ПАМЯТЬ:\n" + ("\n".join(facts) if facts else "пусто."))
        return "\n".join(lines)

    def _messages(self, text: str) -> list[dict]:
        with self._lock:
            history = list(self.history)[-10:]
        return [{"role": "system", "content": self._system_prompt()}, *history, {"role": "user", "content": text}]

    def _remember(self, text: str, answer: str) -> None:
        with self._lock:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": answer})
            self.history = self.history[-12:]

    # ------------------------------------------------------------- providers
    def _ask_ollama(self, messages: list[dict]) -> str:
        # Only ask Ollama for its model list when no model is configured:
        # /api/tags adds up to 3 s of latency to every single answer.
        configured = (self.llm.get("model") or "").strip()
        model = configured or self.pick_model(self.ollama_models(timeout=3))
        if not model:
            raise RuntimeError("ollama: no models")
        payload = self._request(
            f"{self.base_url}/api/chat",
            {"model": model, "messages": messages, "stream": False,
             "options": {"temperature": 0.65, "top_p": 0.9, "repeat_penalty": 1.05, "num_predict": 160}},
            timeout=60,
        )
        answer = ((payload.get("message") or {}).get("content") or "").strip()
        if re.search(r"[а-яё]", answer, re.IGNORECASE):
            answer = re.sub(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", "", answer).strip()
        return answer

    def _ask_openai(self, messages: list[dict]) -> str:
        key = self.llm.get("api_key") or ""
        if not key:
            raise RuntimeError("openai: no api key")
        base = self.base_url
        if not base.endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        model = self.llm.get("model") or "gpt-4o-mini"
        payload = self._request(
            f"{base}/chat/completions",
            {"model": model, "messages": messages, "temperature": 0.8},
            headers={"Authorization": f"Bearer {key}"},
            timeout=60,
        )
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("openai: empty response")
        return (choices[0].get("message") or {}).get("content", "").strip()

    def ask(self, text: str) -> dict:
        """Return {text, mode, provider}."""
        messages = self._messages(text)
        provider = self.provider
        try:
            if provider == "ollama":
                answer = self._ask_ollama(messages)
            else:
                answer = self._ask_openai(messages)
            if not answer:
                raise RuntimeError("empty answer")
            self.last_error = ""
            self._remember(text, answer)
            return {"text": answer, "mode": "llm", "provider": provider}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._status_cache = {}
            answer = self.fallback(text)
            self._remember(text, answer)
            return {"text": answer, "mode": "fallback", "provider": provider}

    # ------------------------------------------------------------- fallback
    def fallback(self, text: str, kind: str | None = None) -> str:
        lowered = text.lower()
        profile = self.memory.profile
        who = profile.get("address") or "босс"
        if any(word in lowered for word in ("как дела", "как ты", "как жизнь")):
            return random.choice(FALLBACK["how_are_you"]).format(who=who)
        if any(word in lowered for word in ("спасибо", "благодар")):
            return random.choice(FALLBACK["thanks"]).format(who=who)
        if any(word in lowered for word in ("привет", "здравствуй", "добрый")):
            return random.choice(FALLBACK["greeting"]).format(who=who)
        if any(word in lowered for word in ("поговор", "поболта", "расскажи что-нибудь")):
            return (
                "Живой режим пока офлайн: локальная модель не отвечает. "
                "Запусти Ollama — и я включусь по-настоящему, с шутками и характером."
            )
        return random.choice(FALLBACK["unknown"]).format(who=who)
