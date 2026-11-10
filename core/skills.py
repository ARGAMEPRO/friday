"""Extra skills: screenshot, file search, passwords, math, weather, todos.

Each handler has the same signature as actions handlers: (intent, memory) and
returns a dict with speech/detail/data. Kept free of actions imports to avoid
a cycle (actions imports this module).
"""
from __future__ import annotations

import ast
import json
import operator
import re
import secrets
import string
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import system
from .config import DRY_RUN


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(number))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


# ------------------------------------------------------------------ screenshot
def screenshot(intent: dict, memory) -> dict:
    if DRY_RUN:
        return {"ok": True, "speech": "Скриншот (dry-run: не сохраняю).", "data": {"photo": False}}
    try:
        path = system.screenshot()
    except Exception as exc:
        return {"ok": False, "speech": f"Не смогла сделать скриншот: {exc}."}
    if not path:
        return {"ok": False, "speech": "Скриншот не получился: система не дала доступ к экрану."}
    try:
        system.open_path(str(path))
    except Exception:
        pass
    return {
        "ok": True,
        "speech": f"Скриншот готов: {path.name}. Открыла его.",
        "detail": str(path),
        "data": {"file": str(path), "photo": True},
    }


# ------------------------------------------------------------------ file search
def find_file(intent: dict, memory) -> dict:
    query = (intent.get("query") or "").strip()
    if not query:
        return {"ok": False, "speech": "Скажи, какой файл искать — например «найди файл договор»."}
    if DRY_RUN:
        return {"ok": True, "speech": f"Нашла бы файлы «{query}» (dry-run).", "data": {"files": []}}
    files = [f for f in system.find_files(query, limit=12)
             if "__pycache__" not in f and not f.endswith((".pyc", ".pyo"))][:8]
    if not files:
        return {"ok": False, "speech": f"Файлов с именем «{query}» не нашла.", "data": {"files": []}}
    names = [Path(item).name for item in files]
    speech = f"Нашла {len(files)} {_plural(len(files), ('файл', 'файла', 'файлов'))}: " + ", ".join(names[:4])
    if len(names) > 4:
        speech += " и ещё " + str(len(names) - 4)
    speech += "."
    return {
        "ok": True,
        "speech": speech,
        "detail": "\n".join(files),
        "data": {"files": files, "first": files[0]},
    }


# ------------------------------------------------------------------ password
def password(intent: dict, memory) -> dict:
    length = int(intent.get("length") or 16)
    length = max(6, min(length, 64))
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+?"
    value = "".join(secrets.choice(alphabet) for _ in range(length))
    # Guarantee at least one digit and one symbol, so weak sites accept it.
    if not any(ch.isdigit() for ch in value):
        value = value[:-1] + secrets.choice(string.digits)
    if not any(ch in "!@#$%^&*-_=+?" for ch in value):
        value = value[:-1] + secrets.choice("!@#$%^&*-_=+?")
    return {"ok": True, "speech": f"Пароль на {length} символов: {value}", "detail": value,
            "data": {"password": value}}


# ------------------------------------------------------------------ calculator
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 20:
            raise ValueError("слишком большая степень")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("не понимаю выражение")


def calc(intent: dict, memory) -> dict:
    expression = (intent.get("expression") or "").strip().replace(" ", "")
    if not expression:
        return {"ok": False, "speech": "Что посчитать? Например: «посчитай 12*8+4»."}
    expression = expression.replace("х", "*").replace("×", "*").replace("÷", "/").replace(",", ".")
    if not re.fullmatch(r"[0-9+\-*/().%]+", expression):
        return {"ok": False, "speech": "Могу считать только числа и знаки + − × ÷ ( )."}
    try:
        value = _eval(ast.parse(expression, mode="eval"))
    except ZeroDivisionError:
        return {"ok": False, "speech": "На ноль делить нельзя, даже мне."}
    except (ValueError, SyntaxError) as exc:
        return {"ok": False, "speech": f"Не посчитала: {exc}."}
    if isinstance(value, float):
        shown = f"{value:.6f}".rstrip("0").rstrip(".")
    else:
        shown = str(value)
    return {"ok": True, "speech": f"{expression} = {shown}", "detail": shown,
            "data": {"expression": expression, "result": shown}}


# ------------------------------------------------------------------ weather
def _get_json(url: str, timeout: float = 6.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Friday/2.2"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def weather(intent: dict, memory) -> dict:
    city = (intent.get("city") or "").strip()
    url = "https://wttr.in/" + (urllib.parse.quote(city) if city else "") + "?format=j1&lang=ru"
    try:
        data = _get_json(url)
        current = (data.get("current_condition") or [{}])[0]
        temp = current.get("temp_C")
        feels = current.get("FeelsLikeC")
        desc = ((current.get("lang_ru") or current.get("weatherDesc") or [{}])[0].get("value") or "").strip()
    except Exception:
        return {"ok": False, "speech": "Погодный сервис не отвечает. Проверь интернет?"}
    if temp is None:
        return {"ok": False, "speech": "Не нашла такой город."}
    place = f" в {city.title()}" if city else ""
    desc_ru = _WEATHER_RU.get(desc.lower(), desc.lower()) or "без осадков"
    speech = f"Сейчас{place} {temp}°, {desc_ru}"
    if feels and feels != temp:
        speech += f", ощущается как {feels}°"
    return {"ok": True, "speech": speech + ".", "detail": f"{temp}°, {desc}"}


# ------------------------------------------------------------------ currency
_WEATHER_RU = {
    "clear": "ясно", "sunny": "солнечно", "partly cloudy": "переменная облачность",
    "cloudy": "облачно", "overcast": "пасмурно", "mist": "дымка", "fog": "туман",
    "smog": "смог", "rain": "дождь", "light rain": "небольшой дождь",
    "moderate rain": "дождь", "heavy rain": "сильный дождь", "snow": "снег",
    "light snow": "небольшой снег", "thunderstorm": "гроза", "drizzle": "морось",
    "patchy rain possible": "местами дождь", "light drizzle": "морось",
}

_CURRENCIES = {
    "доллар": "USD", "бакс": "USD", "usd": "USD",
    "евро": "EUR", "eur": "EUR",
    "юань": "CNY", "юаня": "CNY", "cny": "CNY",
    "фунт": "GBP", "gbp": "GBP",
    "тенге": "KZT", "kzt": "KZT",
    "лира": "TRY", "try": "TRY",
}


def currency(intent: dict, memory) -> dict:
    code = (intent.get("code") or "USD").upper()
    try:
        data = _get_json("https://www.cbr-xml-daily.ru/daily_json.js")
        item = (data.get("Valute") or {}).get(code)
    except Exception:
        return {"ok": False, "speech": "Курсы не загрузились — похоже, нет интернета."}
    if not item:
        return {"ok": False, "speech": f"Курс {code} не нашла."}
    nominal = item.get("Nominal") or 1
    value = (item.get("Value") or 0) / nominal
    name = item.get("Name") or code
    speech = f"{name}: {value:.2f} ₽"
    if nominal > 1:
        speech += f" за {nominal} шт."
    speech += "."
    return {"ok": True, "speech": speech, "detail": f"{value:.2f} RUB"}


# ------------------------------------------------------------------ system toggles
def lock(intent: dict, memory) -> dict:
    ok, speech = system.lock_screen()
    return {"ok": ok, "speech": speech}


def wifi(intent: dict, memory) -> dict:
    enabled = bool(intent.get("enabled"))
    ok, speech = system.wifi(enabled)
    return {"ok": ok, "speech": speech}


# ------------------------------------------------------------------ todos
def todo_add(intent: dict, memory) -> dict:
    text = (intent.get("text") or "").strip()
    if not text:
        return {"ok": False, "speech": "Что записать в задачи?"}
    item = memory.add_todo(text)
    items = memory.todos()
    return {
        "ok": True,
        "speech": f"Записала: {item['text']}. Всего {len(items)} "
                  f"{_plural(len(items), ('задача', 'задачи', 'задач'))}.",
        "detail": item["text"],
        "data": {"todo": item},
    }


def todo_list(intent: dict, memory) -> dict:
    items = [t for t in memory.todos() if not t.get("done")]
    done = memory.todos()
    if not items:
        speech = "Активных задач нет. Скажи «добавь задачу …», и я запишу."
    else:
        lines = [f"{i + 1}. {t['text']}" for i, t in enumerate(items)]
        speech = f"Задачи ({len(items)}): " + "; ".join(lines[:6])
        if len(lines) > 6:
            speech += f" и ещё {len(lines) - 6}"
        speech += "."
    return {"ok": True, "speech": speech, "detail": "\n".join(t["text"] for t in done)}


def todo_done(intent: dict, memory) -> dict:
    index = int(intent.get("index") or 0)
    items = [t for t in memory.todos() if not t.get("done")]
    if not items:
        return {"ok": False, "speech": "Все задачи уже выполнены. Отдыхай."}
    if not 1 <= index <= len(items):
        return {"ok": False, "speech": f"Нет задачи №{index}. Сейчас их {len(items)}."}
    item = items[index - 1]
    memory.complete_todo(item)
    left = len([t for t in memory.todos() if not t.get("done")])
    return {"ok": True, "speech": f"Готово: {item['text']}. Осталось {left}.", "detail": item["text"]}


def todo_remove(intent: dict, memory) -> dict:
    index = int(intent.get("index") or 0)
    items = memory.todos()
    if not 1 <= index <= len(items):
        return {"ok": False, "speech": f"Нет задачи №{index}."}
    item = items.pop(index - 1)
    memory.set_todos(items)
    return {"ok": True, "speech": f"Удалила: {item['text']}.", "detail": item["text"]}


# ------------------------------------------------------------------ history
def history(intent: dict, memory) -> dict:
    period = intent.get("period") or "today"
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        start -= timedelta(days=1)
        end = start + timedelta(days=1)
    else:
        end = now
    start_ts, end_ts = start.timestamp(), end.timestamp()
    items = [h for h in memory.data.get("history", []) if start_ts <= h.get("ts", 0) < end_ts]
    if not items:
        return {"ok": True, "speech": "За этот период команд не было."}
    from collections import Counter
    counter = Counter(item.get("text", "") for item in items)
    top = ", ".join(f"«{text}»" for text, _ in counter.most_common(4))
    label = "вчера" if period == "yesterday" else "сегодня"
    speech = f"{label.capitalize()} {len(items)} {_plural(len(items), ('команда', 'команды', 'команд'))}. Чаще всего: {top}."
    return {"ok": True, "speech": speech, "detail": "\n".join(items[-10:]) if False else ""}


HANDLERS = {
    "skill_screenshot": screenshot,
    "skill_find": find_file,
    "skill_password": password,
    "skill_calc": calc,
    "skill_weather": weather,
    "skill_currency": currency,
    "skill_lock": lock,
    "skill_wifi": wifi,
    "skill_todo_add": todo_add,
    "skill_todo_list": todo_list,
    "skill_todo_done": todo_done,
    "skill_todo_remove": todo_remove,
    "skill_history": history,
}
