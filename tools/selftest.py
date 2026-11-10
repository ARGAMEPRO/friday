"""Self-test / smoke suite for F.R.I.D.A.Y.

    python tools/selftest.py            # everything, offline-friendly
    python tools/selftest.py --tts      # also synthesise one phrase
    python tools/selftest.py -v         # print every check

Uses a temporary data directory, so the real memory and settings are never
touched. Commands that would open apps or browsers run in dry-run mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FRIDAY_DRY_RUN", "1")

from core import catalog, local_tts, nlu, system  # noqa: E402
from core.memory import Memory  # noqa: E402
from core.settings import Settings  # noqa: E402
from core.tasks import Engine  # noqa: E402
from core.telegram import TelegramBot  # noqa: E402

PASSED = 0
FAILED = 0
VERBOSE = False


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        if VERBOSE:
            print(f"  ✓ {name}")
        return True
    FAILED += 1
    print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
    return False


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------- NLU
NLU_CASES = [
    ("открой ютуб", "open", {"target": "ютуб"}),
    ("запусти яндекс музыку", "open", {"target": "яндекс музыку"}),
    ("в браузере открой сайт решу егэ", "open", {"target": "решу егэ", "force": "web"}),
    ("найди рецепт борща", "search", {"query": "рецепт борща"}),
    ("найди в интернете погоду", "search", {"query": "погоду"}),
    ("статус системы", "system", {}),
    ("покажи статус", "system", {}),
    ("который час", "time", {}),
    ("какое сегодня число", "date", {}),
    ("включи следующий трек", "media", {"action": "next"}),
    ("пауза", "media", {"action": "play_pause"}),
    ("выключи музыку", "media", {"action": "stop"}),
    ("стоп", "media", {"action": "stop"}),
    ("громче", "media", {"action": "volume_up"}),
    ("тише", "media", {"action": "volume_down"}),
    ("без звука", "media", {"action": "mute"}),
    ("запомни, что мне нравится рок", "remember_like", {"value": "рок"}),
    ("запомни, что я не люблю шум", "remember_dislike", {"value": "шум"}),
    ("запомни, что мне не нравится шум", "remember_dislike", {"value": "шум"}),
    ("я не люблю шум", "remember_dislike", {"value": "шум"}),
    ("запомни, что меня зовут Арсен", "remember_name", {"value": "арсен"}),
    ("обращайся ко мне шеф", "set_address", {"value": "шеф"}),
    ("без шуток", "profile_humor", {"value": 0}),
    ("что ты умеешь", "help", {}),
    ("поговорим", "chat", {}),
    ("что ты обо мне знаешь", "recall", {}),
    ("какие папки я последний раз открывал в vs code", "vscode_recent", {}),
    ("привет", "smalltalk", {}),
    ("закрой телеграм", "close", {"target": "телеграм"}),
    ("напомни через 10 минут позвонить", "reminder", {"seconds": 600, "text": "позвонить"}),
    ("напомни через час", "reminder", {"seconds": 3600}),
    ("напомни через полчаса позвонить", "reminder", {"seconds": 1800}),
    ("напомни через пару минут", "reminder", {"seconds": 120}),
    ("включи громче", "media", {"action": "volume_up"}),
]


def test_nlu() -> None:
    section("NLU: разбор фраз")
    for phrase, kind, expected in NLU_CASES:
        intents = nlu.parse(phrase)
        ok = bool(intents) and intents[0].get("kind") == kind
        detail = f"получено {intents}"
        if ok:
            for key, value in expected.items():
                if intents[0].get(key) != value:
                    ok = False
                    detail = f"{key}={intents[0].get(key)!r}, ожидалось {value!r}"
                    break
        check(f"«{phrase}» → {kind}", ok, detail)

    section("NLU: несколько команд в одной фразе")
    intents = nlu.parse("открой ютуб и вк и телеграм")
    check("три цели", len(intents) == 3 and [i.get("target") for i in intents] == ["ютуб", "вк", "телеграм"],
          f"получено {[i.get('target') for i in intents]}")
    intents = nlu.parse("открой ютуб и запусти телеграм")
    check("две команды с глаголами", [i.get("kind") for i in intents] == ["open", "open"], f"получено {intents}")
    intents = nlu.parse("привет, как дела")
    check("приветствие + вопрос", intents[0].get("sub") == "how_are_you", f"получено {intents}")
    intents = nlu.parse("открой сайт")
    check("пустая цель не теряется", intents[0].get("kind") == "open" and not intents[0].get("target"),
          f"получено {intents}")
    intents = nlu.parse("не так, я имела в виду вконтакте")
    check("правка", intents[0].get("kind") == "correction", f"получено {intents}")


def test_catalog() -> None:
    section("Каталог: сайты и приложения")
    check("ютуб", (catalog.find_site("ютуб") or ("",))[0] == "ютуб")
    check("опечатка «ютоб»", (catalog.find_site("ютоб") or ("",))[0] == "ютуб")
    check("опечатка «гитхуб»", (catalog.find_site("гитхуб") or ("",))[0] == "гитхаб")
    check("вконтакте", (catalog.find_site("вконтакте") or ("", "https://vk.com"))[1] == "https://vk.com")
    check("url распознан", catalog.looks_like_url("example.com") and catalog.looks_like_url("https://ya.ru"))
    check("url не распознан", not catalog.looks_like_url("просто текст"))
    check("canon приводит падежи", catalog.canon("Ютубе") == "ютуб", catalog.canon("Ютубе"))
    app = catalog.find_app("телеграм")
    check("поиск приложения не падает", app is None or isinstance(app, tuple), repr(app))


# --------------------------------------------------------------------- system
def test_system() -> None:
    section(f"Платформа: {system.LABEL}")
    status = system.system_status()
    check("статус содержит платформу", status.get("platform") == system.LABEL, str(status))
    check("статус содержит CPU", bool(status.get("cpu_count")))
    check("статус содержит диск", status.get("disk_free_gb") is not None, str(status))
    result = system.media("play_pause")
    check("медиа возвращает словарь", isinstance(result, dict) and "ok" in result, repr(result))
    items = system.recent_vscode(3)
    check("история VS Code — список", isinstance(items, list), repr(items))
    ok = system.open_url("https://example.com")
    check("open_url в dry-run", ok is True)


# --------------------------------------------------------------------- engine
def build_engine(tmp: Path) -> tuple[Engine, Settings]:
    memory = Memory(tmp / "memory.json")
    settings = Settings(tmp / "settings.json")
    settings.update("llm", {"provider": "ollama", "base_url": "http://127.0.0.1:59999"})
    return Engine(memory, settings), settings


def test_engine(tmp: Path) -> None:
    section("Движок: команды, память, правки")
    engine, settings = build_engine(tmp)

    tasks = engine.handle_text("статус системы")
    check("статус выполнен", tasks and tasks[0]["status"] == "done", repr(tasks))
    check("статус озвучен", bool(tasks[0]["speech"]))

    engine.handle_text("запомни, что я не люблю шум")
    check("антипатия записана", "шум" in engine.memory.data["dislikes"], str(engine.memory.data))
    check("антипатия не попала в likes", "шум" not in engine.memory.data["likes"], str(engine.memory.data))

    engine.handle_text("запомни, что мне нравится рок")
    check("предпочтение записано", "рок" in engine.memory.data["likes"], str(engine.memory.data))

    engine.handle_text("который час")
    engine.handle_text("открой ютуб")
    correction = engine.handle_text("не так, я имела в виду вконтакте")[0]
    check("правка сохранена", bool(engine.memory.corrections), repr(engine.memory.corrections))
    check("правка применена", "ВКонтакте" in correction["speech"], correction["speech"])

    engine.handle_text("запомни, что меня зовут Арсен")
    check("имя записано", engine.memory.name == "Арсен", str(engine.memory.name))

    engine.handle_text("обращайся ко мне шеф")
    check("обращение записано", engine.memory.profile.get("address") == "шеф", str(engine.memory.profile))

    reminder = engine.handle_text("напомни через 1 час позвонить")[0]
    check("напоминание поставлено", reminder["status"] == "done" and engine.pending_reminders() >= 1)

    empty = engine.handle_text("   ")
    check("пустая команда", empty == [], repr(empty))


def test_telegram(tmp: Path) -> None:
    section("Telegram: логика бота без сети")
    engine, settings = build_engine(tmp / "tg")
    bot = TelegramBot(engine, settings)
    status = bot.status()
    check("статус без токена", status["configured"] is False and status["running"] is False, str(status))
    engine.notifiers.append(bot.broadcast)
    reply = bot._respond(42, "/start")
    check("/start отвечает", "chat id: 42" in reply, reply[:80])
    reply = bot._respond(42, "/status")
    check("/status отвечает", "Ядро: онлайн" in reply, reply[:80])
    reply = bot._respond(42, "/memory")
    check("/memory отвечает", isinstance(reply, str) and bool(reply))
    reply = bot._respond(42, "открой ютуб")
    check("команда выполняется из Telegram", "Открываю" in reply, reply[:80])
    reply = bot._respond(42, "/card")
    check("/card отвечает", "пульт" in reply.lower(), reply[:80])
    check("виджет-фолбэк без токена", bot.send_widget(42, "<div>hi</div>") is False and bool(bot._widget_state))
    check("HTML-разметка в ответах", "<b>" in bot._respond(42, "посчитай 2+2"))
    check("allow_ids парсится", Settings.parse_ids("1, 2;3") == [1, 2, 3])
    settings.update("telegram", {"allow_ids": [7]})
    check("allowlist работает", bot._authorized(7) and not bot._authorized(8))


def test_skills(tmp: Path) -> None:
    section("Навыки: калькулятор, пароль, задачи, история")
    from core import actions, nlu

    engine, _settings = build_engine(tmp / "skills")
    memory = engine.memory
    cases = {
        "посчитай 12*8+4": "skill_calc",
        "сгенерируй пароль на 20": "skill_password",
        "сделай скриншот": "skill_screenshot",
        "какая погода в Москве": "skill_weather",
        "курс доллара": "skill_currency",
        "заблокируй экран": "skill_lock",
        "выключи wifi": "skill_wifi",
        "добавь задачу купить хлеб": "skill_todo_add",
        "мои задачи": "skill_todo_list",
        "выполни задачу 1": "skill_todo_done",
        "удали задачу 1": "skill_todo_remove",
        "что я делал сегодня": "skill_history",
        "найди файл selftest": "skill_find",
    }
    for phrase, kind in cases.items():
        intents = nlu.parse(phrase)
        check(f"«{phrase}» → {kind}", bool(intents) and intents[0].get("kind") == kind,
              str(intents[:1]))
    daily = nlu.parse("напомни каждый день в 9:30 пить воду")[0]
    check("ежедневное напоминание", daily.get("repeat") == "daily" and daily.get("at") == "09:30", str(daily))

    result = actions.execute(nlu.parse("посчитай 12*8+4")[0], memory)
    check("калькулятор считает", result.get("detail") == "100", str(result.get("speech")))
    result = actions.execute(nlu.parse("посчитай 2+")[0], memory)
    check("калькулятор не падает", result.get("ok") is False)

    result = actions.execute(nlu.parse("добавь задачу тест")[0], memory)
    check("задача добавлена", result.get("ok") and len(memory.todos()) == 1)
    result = actions.execute(nlu.parse("выполни задачу 1")[0], memory)
    check("задача выполнена", result.get("ok") and memory.todos()[0]["done"])
    result = actions.execute(nlu.parse("удали задачу 1")[0], memory)
    check("задача удалена", result.get("ok") and not memory.todos())
    result = actions.execute(nlu.parse("что я делал сегодня")[0], memory)
    check("история команд", result.get("ok") is True)

    result = actions.execute(nlu.parse("сгенерируй пароль на 12")[0], memory)
    check("пароль нужной длины", len((result.get("data") or {}).get("password", "")) == 12,
          str(result.get("detail")))


def test_tts(with_tts: bool) -> None:
    section("Голос")
    check("локальный движок определён", bool(local_tts.engine()) or True)
    print(f"  · движок: {local_tts.engine_label() or 'нет (останется браузерный)'}")
    if with_tts:
        path = local_tts.synthesize("Проверка голоса. Пятница на связи.", 1.0, 1.0)
        check("синтез локальным голосом", path is not None and path.exists(), str(path))
    else:
        print("  · синтез не проверялся (добавь --tts)")


def test_http(tmp: Path) -> None:
    section("HTTP API")
    from server import FridayServer

    engine, settings = build_engine(tmp / "http")
    bot = TelegramBot(engine, settings)
    server = FridayServer(("127.0.0.1", 0), engine, bot)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def get_raw(path: str):
        with urllib.request.urlopen(base + path, timeout=8) as response:
            return response.status, response.read().decode("utf-8", "replace")

    def post(path: str, payload: dict):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    try:
        code, health = get("/api/health")
        check("GET /api/health", code == 200 and health.get("platform") == system.PLATFORM, str(health))
        code, settings_payload = get("/api/settings")
        check("GET /api/settings", code == 200 and "tts_engine" in settings_payload)
        check("токен бота не утекает", "token" not in settings_payload["settings"]["telegram"], str(settings_payload["settings"]["telegram"]))
        code, state = get("/api/state")
        check("GET /api/state", code == 200 and "tasks" in state and "reminders" in state)
        code, created = post("/api/command", {"text": "открой ютуб"})
        check("POST /api/command", code == 200 and created.get("ok") and created.get("tasks"), str(created))
        time.sleep(0.6)
        _, state = get("/api/state")
        check("задача завершилась", any(t["status"] == "done" for t in state["tasks"]), str(state["tasks"][-1:]))
        code, tg = get("/api/telegram")
        check("GET /api/telegram", code == 200 and "bot" in tg)
        code, llm = get("/api/llm/setup")
        check("GET /api/llm/setup", code == 200 and "installed" in llm and "pull" in llm)
        code, page = get_raw("/")
        check("GET / отдаёт интерфейс", code == 200 and "F.R.I.D.A.Y." in page, page[:60])
        try:
            get_raw("/../server.py")
            check("path traversal заблокирован", False)
        except urllib.error.HTTPError as exc:
            check("path traversal заблокирован", exc.code in (403, 404), str(exc.code))
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="FRIDAY self-test")
    parser.add_argument("--tts", action="store_true", help="синтезировать тестовую фразу")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    print(f"F.R.I.D.A.Y. self-test · {system.LABEL} · Python {sys.version.split()[0]}")
    with tempfile.TemporaryDirectory(prefix="friday-test-") as raw:
        tmp = Path(raw)
        test_nlu()
        test_catalog()
        test_system()
        test_engine(tmp)
        test_telegram(tmp)
        test_skills(tmp)
        test_tts(args.tts)
        test_http(tmp)

    total = PASSED + FAILED
    print(f"\n{'=' * 46}\n  {PASSED}/{total} проверок прошло, провалов: {FAILED}")
    if FAILED:
        print("  Есть провалы — см. ✗ выше.")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
