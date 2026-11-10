"""Executable actions for every intent kind."""
from __future__ import annotations

import datetime as dt
import random

from . import catalog, skills, system

MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    number = abs(int(number))
    if 10 <= number % 100 <= 20:
        return forms[2]
    if number % 10 == 1:
        return forms[0]
    if 2 <= number % 10 <= 4:
        return forms[1]
    return forms[2]


def _human_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} {_plural(seconds, ('секунду', 'секунды', 'секунд'))}"
    if seconds < 3600:
        minutes = max(1, round(seconds / 60))
        return f"{minutes} {_plural(minutes, ('минуту', 'минуты', 'минут'))}"
    hours = max(1, round(seconds / 3600))
    return f"{hours} {_plural(hours, ('час', 'часа', 'часов'))}"


def label(intent: dict) -> str:
    kind = intent.get("kind")
    if kind == "open":
        return f"Открыть: {intent.get('target', '')}"
    if kind == "search":
        return f"Поиск: {intent.get('query', '')}"
    if kind == "close":
        return f"Закрыть: {intent.get('target', '')}"
    return {
        "vscode_recent": "История VS Code",
        "system": "Статус системы",
        "time": "Текущее время",
        "date": "Сегодняшняя дата",
        "media": "Управление музыкой",
        "recall": "Что я о тебе помню",
        "help": "Список возможностей",
        "reminder": "Напоминание",
        "remember_like": "Запомнить предпочтение",
        "remember_dislike": "Запомнить предпочтение",
        "remember_fact": "Запомнить факт",
        "remember_name": "Запомнить имя",
        "remember_alias": "Запомнить ссылку",
        "smalltalk": "Разговор",
        "chat": "Живой разговор",
        "profile_humor": "Настройка юмора",
        "set_address": "Как обращаться",
        "correction": "Правка",
        "skill_screenshot": "Скриншот",
        "skill_find": "Поиск файла",
        "skill_password": "Пароль",
        "skill_calc": "Калькулятор",
        "skill_weather": "Погода",
        "skill_currency": "Курс валют",
        "skill_lock": "Блокировка экрана",
        "skill_wifi": "Wi-Fi",
        "skill_todo_add": "Новая задача",
        "skill_todo_list": "Мои задачи",
        "skill_todo_done": "Задача выполнена",
        "skill_todo_remove": "Задача удалена",
        "skill_history": "История команд",
    }.get(kind, "Команда")


def _open(intent: dict, memory) -> dict:
    target = (intent.get("target") or "").strip(" .,!?")
    force = intent.get("force")
    if not target:
        return _help({}, memory) | {"speech": "Уточни, что именно открыть."}

    corrected = memory.correction_for(target)
    if corrected:
        target = corrected

    if catalog.looks_like_url(target):
        url = target if "://" in target else "https://" + target
        ok = system.open_url(url)
        return {
            "ok": ok,
            "speech": f"Открываю {url}." if ok else f"Не смог открыть браузер. Ссылка: {url}",
            "detail": url,
            "data": {"url": url},
        }

    site = catalog.find_site(target, memory.aliases)
    app = catalog.find_app(target) if force != "web" else None

    if app and force != "web":
        launched = system.launch_app(target)
        if launched:
            name = catalog.pretty(launched[0])
            return {
                "ok": True,
                "speech": f"Запускаю «{name}».",
                "detail": f"Приложение: {name}\n{launched[1]}",
                "data": {"app": name, "path": launched[1]},
            }
        if not site:
            return {"ok": False, "speech": f"Нашёл «{target}», но запустить не удалось.", "detail": ""}

    if site:
        _, url, pretty = site
        ok = system.open_url(url)
        return {
            "ok": ok,
            "speech": f"Открываю «{pretty}» в браузере." if ok else f"Браузер не отвечает, но вот ссылка: {url}",
            "detail": url,
            "data": {"url": url},
        }

    # unknown target -> web search, never fail silently
    url = catalog.search_url(target)
    ok = system.open_url(url)
    return {
        "ok": ok,
        "speech": f"Готового адреса для «{target}» нет — открыла поиск." if ok else f"Не нашла «{target}».",
        "detail": url,
        "data": {"url": url},
    }


def _search(intent: dict, memory) -> dict:
    query = (intent.get("query") or "").strip()
    url = catalog.search_url(query)
    ok = system.open_url(url)
    return {
        "ok": ok,
        "speech": f"Ищу «{query}» в Яндекс." if ok else "Не удалось открыть браузер.",
        "detail": url,
        "data": {"url": url, "query": query},
    }


def _vscode(intent: dict, memory) -> dict:
    limit = int(intent.get("limit") or 5)
    items = system.recent_vscode(limit)
    if not items:
        return {
            "ok": True,
            "speech": "Не нашла истории папок VS Code. Похоже, ты ещё не открывал проекты.",
            "detail": "",
            "data": {"items": []},
        }
    for i, item in enumerate(items, 1):
        item["index"] = i
    spoken = "; ".join(
        f"{i}) {item['name']} — {item.get('opened_ago', 'недавно')}" for i, item in enumerate(items, 1)
    )
    return {
        "ok": True,
        "speech": f"Последние папки в VS Code: {spoken}.",
        "detail": "\n".join(f"{item['index']}. {item['path']}" for item in items),
        "data": {"items": items},
    }


def _system(intent: dict, memory) -> dict:
    s = system.system_status()
    parts = []
    if s.get("ram_percent") is not None:
        parts.append(f"память занята на {s['ram_percent']}%")
    if s.get("disk_free_gb") is not None:
        parts.append(f"на диске свободно {s['disk_free_gb']} ГБ")
    if s.get("battery") is not None and s["battery"] <= 100:
        state = "на зарядке" if s.get("charging") else "от батареи"
        parts.append(f"заряд {s['battery']}%, {state}")
    if s.get("uptime_h") is not None:
        parts.append(f"система работает {s['uptime_h']} часов")
    speech = "Система в норме: " + ", ".join(parts) + "." if parts else "Система в норме."
    return {
        "ok": True,
        "speech": speech,
        "detail": "\n".join(
            [
                f"Платформа: {s.get('platform', '—')}",
                f"RAM: {s.get('ram_free_gb', '?')} GB свободно из {s.get('ram_total_gb', '?')} GB",
                f"Диск: {s.get('disk_free_gb', '?')} GB свободно",
                f"Батарея: {s.get('battery', 'н/д')}%",
                f"CPU: {s.get('cpu_count', '?')} потоков",
                f"Uptime: {s.get('uptime_h', '?')} ч",
            ]
        ),
        "data": s,
    }


def _time(intent: dict, memory) -> dict:
    now = dt.datetime.now()
    return {"ok": True, "speech": f"Сейчас {now:%H:%M}.", "detail": now.strftime("%d.%m.%Y %H:%M:%S"), "data": {}}


def _date(intent: dict, memory) -> dict:
    now = dt.datetime.now()
    text = f"{now.day} {MONTHS[now.month - 1]} {now.year} года, {WEEKDAYS[now.weekday()]}"
    return {"ok": True, "speech": f"Сегодня {text}.", "detail": now.strftime("%d.%m.%Y"), "data": {}}


def _media(intent: dict, memory) -> dict:
    action = intent.get("action", "")
    result = system.media(action)
    return {
        "ok": bool(result.get("ok")),
        "speech": result.get("speech", ""),
        "detail": action,
        "data": {"action": action},
    }


def _close(intent: dict, memory) -> dict:
    target = intent.get("target", "")
    ok, out = system.close_app(target)
    if ok:
        return {"ok": True, "speech": f"Закрываю «{target}».", "detail": out, "data": {}}
    return {"ok": False, "speech": f"Не получилось закрыть «{target}».", "detail": out, "data": {}}


def _remember(intent: dict, memory) -> dict:
    kind = intent["kind"]
    value = intent.get("value", "")
    if kind == "remember_name":
        memory.set_name(value)
        return {"ok": True, "speech": f"Запомнила, тебя зовут {memory.name}.", "detail": value, "data": {}}
    if kind == "remember_alias":
        memory.add_alias(intent.get("alias", ""), intent.get("url", ""))
        return {
            "ok": True,
            "speech": f"Теперь «{intent.get('alias')}» открывается по адресу {intent.get('url')}.",
            "detail": intent.get("url", ""),
            "data": {},
        }
    field = "likes" if kind == "remember_like" else "dislikes" if kind == "remember_dislike" else "facts"
    added = memory.add(field, value)
    if kind == "remember_like":
        speech = f"Запомнила: тебе нравится {value}." if added else f"Это уже было в памяти: {value}."
    elif kind == "remember_dislike":
        speech = f"Запомнила: ты не любишь {value}." if added else f"Это уже было в памяти: {value}."
    else:
        speech = f"Запомнила: {value}." if added else "Это я уже помню."
    return {"ok": True, "speech": speech, "detail": value, "data": {"added": added}}


def _recall(intent: dict, memory) -> dict:
    if intent.get("field") == "likes" and memory.data["likes"]:
        speech = "Ты любишь: " + ", ".join(memory.data["likes"]) + "."
    else:
        speech = memory.describe()
    return {"ok": True, "speech": speech, "detail": memory.describe(), "data": memory.summary()}


def _reminder(intent: dict, memory) -> dict:
    if intent.get("repeat") == "daily":
        at = intent.get("at") or "09:00"
        body = intent.get("text") or ""
        tail = f": {body}." if body else "."
        return {
            "ok": True,
            "speech": f"Хорошо, буду напоминать каждый день в {at}{tail}",
            "detail": f"Ежедневно в {at}" + (f" — {body}" if body else ""),
            "data": {"repeat": "daily", "at": at, "text": body},
        }
    seconds = intent.get("seconds")
    body = intent.get("text") or ""
    if not seconds:
        return {
            "ok": False,
            "speech": "Скажи, через сколько напомнить — например: «напомни через 10 минут позвонить».",
            "detail": "",
            "data": {},
        }
    tail = f": {body}." if body else "."
    return {
        "ok": True,
        "speech": f"Хорошо, напомню через {_human_seconds(seconds)}{tail}",
        "detail": f"Через {_human_seconds(seconds)} — {body}".rstrip(" —"),
        "data": {"seconds": seconds, "text": body, "at": intent.get("at")},
    }


def _help(intent: dict, memory) -> dict:
    speech = (
        "Я Пятница — твой локальный ассистент. Открываю сайты и приложения, ищу в интернете, "
        "управляю музыкой, показываю папки VS Code и статус системы, ставлю напоминания, "
        "запоминаю предпочтения и правки. Скажи «поговорим» — включу живой режим с шутками. "
        "Ещё умею: скриншот, найти файл, посчитать, сгенерировать пароль, показать погоду и курс, "
        "включить или выключить Wi-Fi, заблокировать экран, вести список задач и напоминать каждый день. "
        "Если что-то сделаю не так, скажи «не так, я имела в виду...» — я запишу правку в память."
    )
    return {"ok": True, "speech": speech, "detail": speech, "data": {}}


def _smalltalk(intent: dict, memory, brain=None) -> dict:
    if brain is not None:
        answer = brain.ask(intent.get("raw", ""))
        return {
            "ok": True,
            "speech": answer["text"],
            "detail": answer["text"],
            "data": {"mode": answer["mode"]},
        }
    sub = intent.get("sub")
    who = (memory.profile or {}).get("address") or memory.name or "босс"
    lines = {
        "greeting": [
            f"На связи, {who}.",
            f"Тут я, {who}. Что на повестке?",
        ],
        "how_are_you": ["Работаю. Все системы в норме — по моим меркам отличный день."],
        "who_are_you": [
            "Я F.R.I.D.A.Y. — Пятница. Локальный ассистент с ирландским характером и твоей памятью в придачу.",
        ],
        "thanks": ["Обращайся, босс."],
        "bye": ["До связи. Буду здесь."],
    }
    speech = random.choice(lines.get(sub, ["Слушаю."]))
    return {"ok": True, "speech": speech, "detail": speech, "data": {}}


def _chat(intent: dict, memory, brain=None) -> dict:
    text = intent.get("raw", "")
    if brain is not None:
        answer = brain.ask(text)
        return {
            "ok": True,
            "speech": answer["text"],
            "detail": answer["text"],
            "data": {"mode": answer["mode"], "provider": answer["provider"]},
        }
    speech = "Живой режим пока офлайн: запусти Ollama, и я начну болтать по-настоящему."
    return {"ok": True, "speech": speech, "detail": speech, "data": {"mode": "offline"}}


def _unknown(intent: dict, memory, brain=None) -> dict:
    if brain is not None:
        answer = brain.ask(intent.get("raw", ""))
        return {
            "ok": True,
            "speech": answer["text"],
            "detail": answer["text"],
            "data": {"mode": answer["mode"]},
        }
    speech = (
        "Не разобрала команду. Скажи «что ты умеешь» — покажу примеры, "
        "или переформулируй: например, «открой ютуб»."
    )
    return {"ok": False, "speech": speech, "detail": intent.get("raw", ""), "data": {}}


def _profile_humor(intent: dict, memory) -> dict:
    value = int(intent.get("value", 2))
    memory.set_profile("humor", value)
    speech = "Окей, больше никаких шуток. Почти." if value == 0 else "Отлично, включаю чувство юмора на полную."
    return {"ok": True, "speech": speech, "detail": f"humor={value}", "data": {"humor": value}}


def _set_address(intent: dict, memory) -> dict:
    value = (intent.get("value") or "").strip()
    if not value:
        return {"ok": False, "speech": "Не расслышала, как к тебе обращаться.", "detail": "", "data": {}}
    memory.set_profile("address", value)
    return {"ok": True, "speech": f"Договорились, {value}.", "detail": value, "data": {"address": value}}


def execute(intent: dict, memory, brain=None) -> dict:
    kind = intent.get("kind")
    if kind in skills.HANDLERS:
        result = skills.HANDLERS[kind](intent, memory)
    elif kind in ("chat", "smalltalk", "unknown"):
        handler = {"chat": _chat, "smalltalk": _smalltalk, "unknown": _unknown}[kind]
        result = handler(intent, memory, brain)
    else:
        handler = {
            "open": _open,
            "search": _search,
            "vscode_recent": _vscode,
            "system": _system,
            "time": _time,
            "date": _date,
            "media": _media,
            "close": _close,
            "remember_like": _remember,
            "remember_dislike": _remember,
            "remember_fact": _remember,
            "remember_name": _remember,
            "remember_alias": _remember,
            "recall": _recall,
            "reminder": _reminder,
            "help": _help,
            "profile_humor": _profile_humor,
            "set_address": _set_address,
        }.get(kind, _unknown)
        result = handler(intent, memory)
    result.setdefault("label", label(intent))
    return result
