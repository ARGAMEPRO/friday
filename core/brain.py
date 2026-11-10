"""LLM brain: FRIDAY's live conversation layer.

Works with a local Ollama server or any OpenAI-compatible endpoint. When no
model is reachable the assistant falls back to hand written lines, so the app
never goes silent.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
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


_BANNED = re.compile(
    r"как искусственн\w* интеллект|у меня нет (чувств|эмоций)|"
    r"чем могу (быть )?полезна|всегда готов[аы] (вам )?помочь", re.IGNORECASE)

PERSONA = """Ты — Пятница. Девушка-помощница с характером, вкусом и настроением.
Говоришь по-русски, на «вы», обращаешься «{address}». О себе — в женском роде.
У тебя есть своё мнение: оцениваешь, сомневаешься, можешь не согласиться и сказать,
что новость скучная или глупая. Не выдумывай факты, версии и даты; о событиях говори
только по найденным источникам, а если данных нет — так и скажи.
Не выдумывай, чем занималась или что чувствуешь, и не повторяй за пользователем его
занятия. На вопрос «ты что делаешь?» отвечай только «ничего особенного, жду вашей
команды» или «дела как обычно».
Запрещено: «конечно», «с радостью», «чем могу помочь», «я всего лишь ИИ», «у меня нет
эмоций», «как искусственный интеллект», эмодзи, списки и восторги.
Отвечай живо и коротко: 1–3 предложения, как человек в переписке.
Примеры:
— «Привет, как дела?» → «Привет. Да как обычно, всё в порядке. А у вас?»
— «Что думаешь про этот фильм?» → «Снято красиво, но пусто. Второй раз не стал бы смотреть.»
— «Что нового в технике?» → сначала факты из источников, потом одна фраза «Моё мнение: …».
Уровень юмора: {humor}/3 (0 — серьёзно, 3 — редкая добродушная ирония)."""


class Brain:
    def __init__(self, memory: Memory, settings: Settings):
        self.memory = memory
        self.settings = settings
        self.history: list[dict] = []
        self._lock = threading.RLock()
        self._status_cache: dict = {}
        self._status_at = 0.0
        self.last_error = ""
        self.pull_state: dict = {"running": False, "ok": False, "status": "", "percent": 0}
        self._pull_lock = threading.Lock()

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
            if not models:
                return configured
            # The configured model may not be downloaded: use an installed one
            # instead of silently reporting "offline".
            base = configured.split(":")[0]
            for model in models:
                if model == configured or model.split(":")[0] == base:
                    return model
            return models[0]
        if not models:
            return ""
        for hint in ("qwen", "llama3", "llama", "mistral", "gemma", "phi"):
            for model in models:
                if hint in model.lower():
                    return model
        return models[0]

    # ------------------------------------------------------- ollama autopilot
    def _ollama_executable(self) -> str | None:
        found = shutil.which("ollama")
        if found:
            return found
        candidates = ["/usr/local/bin/ollama", "/opt/homebrew/bin/ollama",
                      "/Applications/Ollama.app/Contents/Resources/ollama"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def ensure_server(self, wait: float = 10.0) -> dict:
        """Start `ollama serve` if it is installed but not running."""
        if self.provider != "ollama":
            return {"ok": False, "error": "провайдер не Ollama"}
        if self.ollama_models(timeout=1.0):
            return {"ok": True, "started": False, "models": self.ollama_models(timeout=1.0)}
        exe = self._ollama_executable()
        if not exe:
            return {"ok": False, "error": "Ollama не установлена"}
        try:
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                      "stderr": subprocess.DEVNULL}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([exe, "serve"], **kwargs)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        deadline = time.time() + wait
        while time.time() < deadline:
            models = self.ollama_models(timeout=0.8)
            if models:
                self._status_cache = {}
                return {"ok": True, "started": True, "models": models}
            time.sleep(0.4)
        return {"ok": False, "error": "Ollama не ответила за 10 секунд"}

    def models(self) -> list[str]:
        return self.ollama_models()

    def pull(self, model: str) -> dict:
        """Download a model, streaming progress into ``pull_state``."""
        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "пустое имя модели"}
        with self._pull_lock:
            if self.pull_state.get("running"):
                return {"ok": False, "error": "загрузка уже идёт", "state": dict(self.pull_state)}
            self.pull_state = {"running": True, "ok": False, "model": model,
                               "status": "старт", "percent": 0, "error": ""}
        try:
            request = urllib.request.Request(
                f"{self.base_url}/api/pull",
                data=json.dumps({"model": model, "stream": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3600) as response:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if item.get("error"):
                        raise RuntimeError(item["error"])
                    total = item.get("total") or 0
                    completed = item.get("completed") or 0
                    percent = int(completed * 100 / total) if total else 0
                    with self._pull_lock:
                        self.pull_state.update(
                            status=item.get("status") or "загрузка",
                            percent=max(self.pull_state.get("percent", 0), percent),
                        )
            with self._pull_lock:
                self.pull_state.update(running=False, ok=True, percent=100, status="готово")
            self._status_cache = {}
            return {"ok": True, "state": dict(self.pull_state)}
        except Exception as exc:
            with self._pull_lock:
                self.pull_state.update(running=False, ok=False, error=str(exc), status="ошибка")
            return {"ok": False, "error": str(exc), "state": dict(self.pull_state)}

    def setup(self) -> dict:
        """Everything the UI needs to offer a one-click model install."""
        provider = self.provider
        models = self.ollama_models() if provider == "ollama" else []
        configured = (self.llm.get("model") or "").strip()
        installed = bool(models)
        return {
            "provider": provider,
            "configured": configured,
            "models": models,
            "installed": installed,
            "needs_pull": provider == "ollama" and not installed,
            "suggested": configured or "qwen2.5:7b",
            "pull": dict(self.pull_state),
        }

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

    def brief(self, query: str, sources: list[dict]) -> str:
        """Fast digest: headlines from the feed + one short opinion from the model."""
        bits = []
        for item in sources[:3]:
            title = (item.get("title") or "").strip().rstrip(".")
            if not title:
                continue
            if len(title) > 110:
                title = title[:110].rsplit(" ", 1)[0] + "…"
            bits.append(title)
        facts = "Главное: " + "; ".join(bits) + "." if bits else ""
        tone = random.choice(("скептически", "критично", "с иронией", "осторожно",
                              "с интересом", "по-деловому"))
        messages = [
            {"role": "system", "content": "Ты живая собеседница. Оцени новости одной фразой "
                                          "по-русски, начни строго словами «Моё мнение:». Ровно одно "
                                          "предложение до 22 слов. Запрещены слова: важно, актуально, "
                                          "интересно, значимо. Не пересказывай новости — дай оценку, "
                                          f"можно сомневаться, критиковать или шутить. Тон: {tone}."},
            {"role": "user", "content": f"Запрос: {query}\n" + "\n".join(bits)},
        ]
        try:
            opinion = (self._ask_ollama(messages, num_predict=100, temperature=0.95)
                       or "").split("\n")[0].strip()
        except Exception:
            opinion = ""
        if not opinion.lower().startswith("моё мнение"):
            opinion = "Моё мнение: " + opinion if opinion else "Моё мнение: пока рано делать выводы."
        if opinion and opinion[-1] not in ".!?…":
            opinion = opinion.rstrip(" ,;:—-")
            opinion = opinion.rsplit(" ", 1)[0].rstrip(" ,;:—-") + "."
            if len(opinion) < 12:
                opinion = "Моё мнение: пока рано делать выводы."
        return f"{facts} {opinion}".strip()

    def suggest_command(self, description: str) -> dict:
        """Turn a plain request into {"phrase": ..., "target": ...} for the UI."""
        messages = [
            {"role": "system", "content": "Преобразуй просьбу в команду ассистента. Верни только JSON "
                                          'вида {"phrase": "короткая фраза-триггер", "target": "URL '
                                          'или название приложения"}. Без пояснений.'},
            {"role": "user", "content": description[:500]},
        ]
        try:
            raw = self._ask_ollama(messages, num_predict=90) or ""
        except Exception:
            return {}
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return {}
        phrase = str(data.get("phrase") or "").strip()
        target = str(data.get("target") or "").strip()
        return {"phrase": phrase, "target": target} if phrase and target else {}

    def _remember(self, text: str, answer: str) -> None:
        with self._lock:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": answer})
            self.history = self.history[-12:]

    # ------------------------------------------------------------- providers
    def _ask_ollama(self, messages: list[dict], num_predict: int = 120,
                    temperature: float = 0.65) -> str:
        # Only ask Ollama for its model list when no model is configured:
        # /api/tags adds up to 3 s of latency to every single answer.
        model = self.pick_model(self.ollama_models(timeout=3))
        if not model:
            raise RuntimeError("ollama: no models")
        payload = self._request(
            f"{self.base_url}/api/chat",
            {"model": model, "messages": messages, "stream": False,
             "options": {"temperature": temperature, "top_p": 0.9, "repeat_penalty": 1.05,
                         "num_predict": num_predict, "num_ctx": 2048},
             "keep_alive": "20m"},
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
            answer = answer.replace("？", "?").replace("！", "!").replace("，", ",")
            if re.search(r"[а-яё][a-z]|[a-z][а-яё]|\d{3,}", answer, re.IGNORECASE):
                answer = re.sub(r"[A-Za-z]+\d+|\d+[A-Za-z]+", "", answer).strip()
            if _BANNED.search(answer):
                retry = self._ask_ollama(
                    messages + [{"role": "system", "content": "Это шаблонный ответ. Ответь иначе, "
                                                              "живо, без фраз про искусственный интеллект "
                                                              "и без «чем могу помочь»."}],
                    temperature=0.9,
                )
                if retry and not _BANNED.search(retry):
                    answer = retry
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
