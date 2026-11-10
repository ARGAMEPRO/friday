"""Rule based intent parser for Russian / English voice commands."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import catalog

MEDIA_PATTERNS = [
    ("prev", r"\b(предыдущ\w*|назад)\b.{0,12}\b(трек|песн|композиц)"),
    ("next", r"\b(следующ\w*|дальше)\b.{0,12}\b(трек|песн|композиц)|\bпереключи\b.{0,10}\bтрек"),
    ("play_pause", r"^(пауза|поставь на паузу|приостанови|pause)\b"),
    ("play_pause", r"^(играй|play|resume)$"),
    ("play_pause", r"\b(продолжи|продолжить|сними с паузы|играй|play|resume)\b"),
    ("stop", r"\b(останови|выключи|стоп|stop)\b.{0,18}\b(музык|трек|воспроизвед|плеер)"),
    ("stop", r"^(стоп|stop)$"),
    ("mute", r"\b(без звука|выключи звук|включи звук|мут|mute)\b"),
    ("volume_up", r"\b(громче|прибавь звук|сделай громче|louder)\b"),
    ("volume_down", r"\b(тише|убавь звук|сделай тише|quieter)\b"),
]

OPEN_RE = re.compile(
    r"\b(открой|откройте|открыть|запусти|запустите|запустить|включи|включить|"
    r"перейди|перейти|зайди|зайти|open|launch|start)\b",
    re.I,
)
SEARCH_RE = re.compile(r"\b(найди|найти|поищи|поискать|загугли|загуглить|search|google)\b", re.I)
CLOSE_RE = re.compile(r"\b(закрой|закройте|закрыть|выключи|выключить|убей|останови|close|kill)\b", re.I)

CLEAN_TARGET_RE = re.compile(
    r"\b(в браузере|браузере|браузер|сайт|сайте|сайтик|страницу|страница|"
    r"в интернете|в сети|во всемирной паутине|"
    r"приложение|приложении|программу|программа|прогу|пожалуйста|плиз|пж|"
    r"мне|меня|ссылка|ссылку|быстро|сейчас|уже|этот|эту)\b",
    re.I,
)

SMALLTALK = [
    ("greeting", r"^(привет|приветствую|здравствуй\w*|доброе утро|добрый день|добрый вечер|хай|hello|hi)\b"),
    ("how_are_you", r"как (у тебя )?дела|как ты|how are you"),
    ("who_are_you", r"кто ты|что ты такое|расскажи о себе|who are you"),
    ("thanks", r"^(спасибо|благодарю|thanks|thank you)\b"),
    ("bye", r"^(пока|до свидания|до встречи|bye|goodbye)\b"),
]


def normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[«»\"“”]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,!?;\n\t")


def split_commands(text: str) -> list[str]:
    parts = re.split(r"\s+(?:и|а также|потом|затем|после этого|далее)\s+|[;]|,\s*", text)
    cleaned = []
    for part in parts:
        part = part.strip(" .,!?")
        # Two-letter targets are real: «вк», «тг». Drop only filler words.
        if len(part) >= 3 or (len(part) >= 2 and catalog.find_site(part)):
            cleaned.append(part)
    return cleaned or [text]


def _clean_target(value: str) -> str:
    value = CLEAN_TARGET_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip(" .,!?:;-")
    return value


def _media(text: str):
    for action, pattern in MEDIA_PATTERNS:
        if re.search(pattern, text):
            return {"kind": "media", "action": action, "raw": text}
    return None


def _vscode(text: str):
    code_ctx = re.search(r"\b(vs ?code|вс ?код|вскод|visual studio(?: code)?|вс\s?код)\b", text)
    history_ctx = re.search(r"(папк|проект|воркспейс|workspace)", text) and re.search(
        r"(последн|недавн|открыва|истори|запуска|работал|открыт)", text
    )
    if code_ctx and re.search(r"(папк|проект|истори|открыва|последн|недавн|расскаж|покаж)", text):
        return {"kind": "vscode_recent", "limit": 5, "raw": text}
    if history_ctx and re.search(r"(папк|проект)", text):
        return {"kind": "vscode_recent", "limit": 5, "raw": text}
    return None


def _system(text: str):
    if re.search(
        r"(статус системы|система в порядке|загрузк\w* (систем|процессор)|"
        r"сколько (памяти|озу|ram|свободно)|батаре|аккумулятор|температур|"
        r"процессор загружен|диск\w* (свободн|занят)|покажи статус|"
        r"как (там )?система|system status|system check)",
        text,
    ):
        return {"kind": "system", "raw": text}
    return None


def _time_date(text: str):
    if re.search(r"(который час|сколько времени|текущее время|what time)", text):
        return {"kind": "time", "raw": text}
    if re.search(r"(какое (сегодня )?число|какая (сегодня )?дата|какой (сегодня )?день|what date)", text):
        return {"kind": "date", "raw": text}
    return None


def _memory_whole(text: str, raw: str):
    """Memory commands are parsed on the full phrase so 'и' inside values is safe."""
    flat = re.sub(r"[,]+", " ", text).strip()
    if re.search(r"(меня зовут|my name is)", text):
        m = re.search(r"(?:меня зовут|my name is)\s+([a-zа-я0-9\- ]+)", text)
        if m:
            return {"kind": "remember_name", "value": m.group(1).strip(), "raw": raw}

    alias = re.search(
        r"(?:запомни|запиши|remember)\s+(?:сайт|ссылку|адрес|site)\s+(.+?)\s+(?:это|как|—|--|-|:)\s*(\S+)",
        text,
    )
    if alias:
        return {
            "kind": "remember_alias",
            "alias": alias.group(1).strip(),
            "url": alias.group(2).strip(),
            "raw": raw,
        }

    forget = re.match(
        r"^(?:забудь|удали|сотри|убери)\s+(?:из памяти\s+)?(?:что\s+|про\s+|информацию о\s+)?"
        r"(?:я\s+)?(?:больше\s+)?(?:не\s+)?(?:люблю|нравится|обожаю)?\s*(.{2,80})$",
        flat,
    )
    if forget and not re.match(
        r"^(?:удали|убери|сотри)\s+(?:задачу|напоминание|файл|todo|task|последн)", flat
    ):
        return {"kind": "forget", "value": forget.group(1).strip(" .,!?;:-"), "raw": raw}

    # Dislikes must be checked before likes: "я не люблю шум" contains the
    # word "люблю" and used to be stored as a preference.
    m = re.search(r"(?:запомни|запиши|remember)?[,\s]*(?:что\s+)?мне не\s+нрав(?:ится|ятся)\s+(.+)", text)
    if m:
        return {"kind": "remember_dislike", "value": m.group(1), "raw": raw}
    m = re.search(
        r"(?:запомни|запиши|remember)?[,\s]*(?:что\s+)?(?:я\s+)?"
        r"(?:не\s+(?:очень\s+)?люблю|ненавижу|недолюбливаю|терпеть не могу)\s+(.+)",
        text,
    )
    if m:
        remove_like = bool(re.match(r"^(?:я\s+)?(?:больше\s+)?не\s+люблю", text.strip()))
        return {"kind": "remember_dislike", "value": m.group(1), "remove_like": remove_like, "raw": raw}
    m = re.search(r"(?:запомни|запиши|remember)?[,\s]*(?:что\s+)?мне нрав(?:ится|ятся)\s+(.+)", text)
    if m:
        return {"kind": "remember_like", "value": m.group(1), "raw": raw}
    m = re.search(r"(?:запомни|запиши|remember)?[,\s]*(?:что\s+)?(?:я\s+)?(?:обожаю|люблю)\s+(.+)", text)
    if m:
        return {"kind": "remember_like", "value": m.group(1), "raw": raw}
    m = re.search(r"(?:запомни|запиши|remember)\s+(?:что\s+)?(.+)", flat)
    if m:
        return {"kind": "remember_fact", "value": m.group(1), "raw": raw}

    recall = _recall_whole(text)
    if recall:
        recall["raw"] = raw
        return recall
    return None


RECALL_RE = re.compile(
    r"(что\s+(ты\s+)?(обо\s+мне\s+)?(знаешь|помнишь)|что\s+(ты\s+)?(знаешь|помнишь)\s+обо\s+мне|"
    r"что\s+я\s+(люблю|не\s+люблю)|что\s+мне\s+нравится|мои\s+предпочтения|"
    r"что\s+тебе\s+известно|что\s+ты\s+запомнил[а]?|remember about me)"
)


def _recall_whole(text: str):
    if RECALL_RE.search(text):
        field = "likes" if re.search(r"что (я|мне) (люблю|нравится)", text) else "all"
        return {"kind": "recall", "field": field}


def _reminder(text: str):
    if not re.search(r"\bнапомни|\bremind\b", text):
        return None
    seconds = None
    at_time = None
    m = re.search(r"через\s+(\d+)\s*(секунд\w*|сек|минут\w*|мин|час\w*|ч)\b", text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("сек"):
            seconds = amount
        elif unit.startswith("час") or unit == "ч":
            seconds = amount * 3600
        else:
            seconds = amount * 60
    else:
        # "через час", "через полчаса", "через пару минут" — speech without digits.
        m = re.search(
            r"через\s+(полтора\s+часа|полчаса|пол\s+часа|час|минуту|минутку|"
            r"пару\s+минут|пару\s+секунд|полминуты)\b",
            text,
        )
        if m:
            phrase = re.sub(r"\s+", " ", m.group(1))
            seconds = {
                "полтора часа": 5400,
                "полчаса": 1800,
                "пол часа": 1800,
                "час": 3600,
                "минуту": 60,
                "минутку": 60,
                "пару минут": 120,
                "пару секунд": 2,
                "полминуты": 30,
            }.get(phrase, 60)
        else:
            m = re.search(r"\bв\s+(\d{1,2})[:.](\d{2})", text)
            if m:
                hour, minute = int(m.group(1)), int(m.group(2))
                now = datetime.now()
                target = now.replace(hour=min(hour, 23), minute=min(minute, 59), second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                seconds = int((target - now).total_seconds())
                at_time = target.strftime("%H:%M")
    body = re.sub(r"^.*?\bнапомни(?:ть)?\s*(?:мне)?\s*", "", text)
    body = re.sub(r"через\s+\d+\s*(секунд\w*|сек|минут\w*|мин|час\w*|ч)\b", " ", body)
    body = re.sub(
        r"через\s+(полтора\s+часа|полчаса|пол\s+часа|час|минуту|минутку|"
        r"пару\s+минут|пару\s+секунд|полминуты)\b",
        " ",
        body,
    )
    body = re.sub(r"\bв\s+\d{1,2}[:.]\d{2}\b", " ", body)
    body = re.sub(r"^(что|о том, что|про)\s+", "", body.strip())
    body = body.strip(" .,!?:;-")
    if seconds is None:
        return {"kind": "reminder", "seconds": None, "text": body, "raw": text}
    return {"kind": "reminder", "seconds": max(1, seconds), "at": at_time, "text": body, "raw": text}


CORRECTION_RE = re.compile(
    r"^(?:нет[,\s]+)?(?:не так|не то|это не то|неправильно|поправка|точнее|"
    r"я имел[а]? в виду|я хотел[а]? сказать|я хотел[а]?)[,:\s]+(.+)"
)
HUMOR_RE = re.compile(r"(не подкалывай|без шуток|будь серь[её]зн\w*|хватит шутить|не шути)")
HUMOR_ON_RE = re.compile(r"(подкалывай|можно с юмором|будь смешнее|включи юмор)")
CHAT_RE = re.compile(
    r"(поговор\w*|поболта\w*|расскажи (?:что-нибудь|историю)|"
    r"что думаешь|тво[её] мнение|скучно|как жизнь|что нового)"
)


def _correction(text: str, raw: str):
    match = CORRECTION_RE.match(text)
    if not match:
        return None
    body = match.group(1).strip(" .,!?:;-")
    if len(body) < 2:
        return None
    return {"kind": "correction", "text": body, "raw": raw}


def _parse_chunk(text: str):
    media = _media(text)
    if media:
        return media

    result = _time_date(text)
    if result:
        return result

    result = _system(text)
    if result:
        return result

    result = _vscode(text)
    if result:
        return result

    if re.search(r"\bпомощь\b|\bпомоги\b|\bпомочь\b|что (ты )?умеешь|список команд|какие команды|help\b|возможности", text):
        return {"kind": "help", "raw": text}

    if HUMOR_RE.search(text):
        return {"kind": "profile_humor", "value": 0, "raw": text}
    if HUMOR_ON_RE.search(text):
        return {"kind": "profile_humor", "value": 2, "raw": text}
    if re.search(r"обращайся ко мне", text):
        match = re.search(r"обращайся ко мне\s+(.+)", text)
        if match:
            return {"kind": "set_address", "value": match.group(1).strip(" .,!?;"), "raw": text}
    if CHAT_RE.search(text):
        return {"kind": "chat", "raw": text}

    if re.search(r"\b(закрой|закрыть|выключи|выключить|убей|close|kill)\b", text):
        m = CLOSE_RE.search(text)
        target = _clean_target(text[m.end():])
        if target:
            return {"kind": "close", "target": target, "raw": text}

    if OPEN_RE.search(text):
        m = OPEN_RE.search(text)
        target = _clean_target(text[m.end():])
        force = None
        if re.search(r"(в браузере|браузер\w*|сайт\w*|страниц\w*|\bweb\b|в интернете)", text):
            force = "web"
        if re.search(r"(приложени\w*|программ\w*|\bapp\b)", text):
            force = "app"
        # An empty target still parses: the action layer asks a follow-up
        # question instead of dropping the request into "unknown".
        return {"kind": "open", "target": target, "force": force, "raw": text}

    if re.search(r"\bновост\w*|\bчто\s+(?:сейчас\s+)?(?:происходит|случилось)|последние\s+события", text):
        topic = re.search(r"(?:про|о|об|по)\s+(.+)$", text)
        query = "последние новости сегодня"
        if topic:
            value = topic.group(1).strip(" ?!.")
            query = "последние новости сегодня" if "новост" in value else f"{value} новости"
        return {"kind": "search", "query": query, "raw": text}

    m = SEARCH_RE.search(text)
    if m:
        query = _clean_target(text[m.end():])
        query = re.sub(r"^(?:информаци\w*|инфу|сведения|данные|новости)\s+(?:про|о|об|по)\s+", "", query)
        query = re.sub(r"^(?:в интернете|в сети|мне|пожалуйста)\s+", "", query).strip()
        if query:
            return {"kind": "search", "query": query, "raw": text}

    if re.search(r"\bкто ты\b|\bпредставься\b|как тебя зовут|what are you\b", text):
        return {"kind": "identity", "raw": text}
    if re.match(r"^кто я\b", text):
        return {"kind": "whoami", "raw": text}

    for kind, pattern in SMALLTALK:
        if re.search(pattern, text):
            return {"kind": "smalltalk", "sub": kind, "raw": text}

    if catalog.looks_like_url(text):
        return {"kind": "open", "target": text, "force": "web", "raw": text}

    return None


def _is_known_target(chunk: str) -> bool:
    """True when a verbless chunk names a site/app, e.g. «вк» in «открой вк и вконтакте»."""
    chunk = chunk.strip()
    if not chunk or len(chunk) > 60:
        return False
    if catalog.looks_like_url(chunk):
        return True
    if catalog.find_site(chunk):
        return True
    try:
        return catalog.find_app(chunk) is not None
    except Exception:
        return False


_DAILY_RE = re.compile(
    r"^(?:напомни(?:ть)?(?: мне)? )?каждый день в (\d{1,2})(?::(\d{2}))?\s*(.*)$", re.I
)


def _skill_intent(text: str) -> dict | None:
    """Quick, deterministic skills that are cheaper than the LLM."""
    low = text.lower().strip()

    daily = _DAILY_RE.match(low)
    if daily:
        hour = max(0, min(23, int(daily.group(1))))
        minute = max(0, min(59, int(daily.group(2) or 0)))
        body = (daily.group(3) or "").strip(" .,!?:;-")
        return {"kind": "reminder", "repeat": "daily", "at": f"{hour:02d}:{minute:02d}",
                "text": body or "напоминание", "seconds": None, "raw": text}

    if re.search(r"\b(скриншот|скрин|снимок экрана|screenshot)\b|что на экране", low):
        return {"kind": "skill_screenshot", "raw": text}

    found = re.match(r"^(?:найди(?:те)?|найти|search) файл(?:ы)?\s+(.{2,60})$", low)
    if found:
        return {"kind": "skill_find", "query": found.group(1).strip(" .,!?\"'"), "raw": text}

    pwd = re.search(r"\b(?:сгенерируй|сделай|придумай|создай) (?:мне )?(?:пароль|паролик)(?: на (\d{1,3}))?", low)
    if pwd:
        return {"kind": "skill_password", "length": int(pwd.group(1) or 16), "raw": text}

    calc = re.match(r"^(?:посчитай|вычисли|сколько будет|calc)\s+(.{1,80})$", low)
    if calc:
        return {"kind": "skill_calc", "expression": calc.group(1), "raw": text}

    if re.search(r"\bпогод", low) and not re.match(r"^(найди|поищи|загугли|search|google)", low):
        city = ""
        match = re.search(r"погод\w*(?:\s+в\s+([а-яёa-z\- ]{2,30}))?", low.strip(" ?!"))
        if match and match.group(1):
            city = match.group(1).strip()
        return {"kind": "skill_weather", "city": city, "raw": text}

    cur = re.search(r"\bкурс\s+(доллара|евро|юаня|фунта|тенге|лиры|usd|eur|cny|gbp|try|kzt)\b", low)
    if not cur:
        cur = re.search(r"\b(?:сколько стоит|сколько стоят) (доллар|евро|юань|фунт|тенге|лира)\b", low)
    if cur:
        code = _CURRENCY_CODES.get(cur.group(1), "USD")
        return {"kind": "skill_currency", "code": code, "codes": [code], "raw": text}
    if re.search(r"\bкурс", low) and re.search(r"валют|рубл", low):
        codes = [v for k, v in _CURRENCY_CODES.items() if k in low]
        return {"kind": "skill_currency", "codes": list(dict.fromkeys(codes)) or ["USD", "EUR", "CNY", "GBP"],
                "raw": text}

    if re.search(r"\b(заблокируй|заблокировать) (?:экран|компьютер|мак|систему|ноут)|\block (?:screen|pc)\b", low):
        return {"kind": "skill_lock", "raw": text}

    wifi = re.search(r"\b(включи|вруби|выключи|отключи|выруби)\s+(?:wi-?fi|вай-?фай)\b", low)
    if wifi:
        return {"kind": "skill_wifi", "enabled": wifi.group(1) in ("включи", "вруби"), "raw": text}

    add = re.match(r"^(?:добавь|запиши|внеси)\s+(?:мне\s+)?(?:новую\s+)?(?:задачу|в список дел|в задачи)[:\s]+(.{2,200})$", low)
    if add:
        return {"kind": "skill_todo_add", "text": add.group(1).strip(" .,!?"), "raw": text}
    if re.match(r"^(?:мои |какие )?задачи$|^список задач$|^что по задачам$", low.strip(" ?!")):
        return {"kind": "skill_todo_list", "raw": text}
    done = re.match(r"^(?:выполни|сделай|закрой|готово)\s+задачу\s+(\d{1,3})$", low.strip(" .!"))
    if done:
        return {"kind": "skill_todo_done", "index": int(done.group(1)), "raw": text}
    remove = re.match(r"^(?:удали|убери|сотри)\s+задачу\s+(\d{1,3})$", low.strip(" .!"))
    if remove:
        return {"kind": "skill_todo_remove", "index": int(remove.group(1)), "raw": text}

    study = re.search(
        r"\b(?:порешать|решать|позаниматься|поучиться|поделать|разобрать|готовиться\s+к)\s+"
        r"(?:егэ\s+)?(информатик\w*|математик\w*|русск\w*|физик\w*|хими\w*|биолог\w*|"
        r"истори\w*|обществ\w*|английск\w*|литератур\w*|географ\w*)", low)
    if not study:
        study = re.search(r"\bрешу\s+егэ\s+(информатик\w*|математик\w*|русск\w*|физик\w*|"
                          r"хими\w*|биолог\w*|истори\w*|обществ\w*|английск\w*|литератур\w*|"
                          r"географ\w*)", low)
    if study:
        stem = study.group(1)
        for key in ("информатик", "математик", "русск", "физик", "хими", "биолог",
                    "истори", "обществ", "английск", "литератур", "географ"):
            if stem.startswith(key):
                return {"kind": "skill_study", "subject": key, "raw": text}

    hist = re.search(r"что я делал(?:а)?\s+(сегодня|вчера)|история (?:команд|задач)", low)
    if hist:
        return {"kind": "skill_history", "period": "yesterday" if "вчера" in hist.group(0) else "today",
                "raw": text}
    return None


_CURRENCY_CODES = {
    "доллар": "USD", "доллара": "USD", "usd": "USD",
    "евро": "EUR", "eur": "EUR",
    "юань": "CNY", "юаня": "CNY", "cny": "CNY",
    "фунт": "GBP", "фунта": "GBP", "gbp": "GBP",
    "тенге": "KZT", "kzt": "KZT",
    "лира": "TRY", "лиры": "TRY", "try": "TRY",
}


_MULTI = re.compile(r"\s+(?:а\s+также|и\s+ещё|и\s+еще|а\s+потом|потом|плюс|и)\s+", re.I)


def _multi_parts(value: str) -> list[str]:
    if not _MULTI.search(value):
        return [value]
    parts = [part.strip(" ,.!?") for part in _MULTI.split(value)]
    return [part for part in parts if part]


_STT_FIX = (
    ("визуал студио", "visual studio"), ("вижуал студио", "visual studio"),
    ("визуал студия", "visual studio"), ("вс студио", "visual studio"),
    ("курс долара", "курс доллара"), ("курс евро", "курс евро"),
    ("решу еге", "решу егэ"), ("решу егэ", "решу егэ"),
    ("телеграмм", "telegram"), ("ютуп", "ютуб"),
)


def _fix_speech(text: str) -> str:
    """Small dictionary for typical Russian speech-recognition slips."""
    value = text
    for wrong, right in _STT_FIX:
        value = value.replace(wrong, right)
    return value


def parse(text: str) -> list[dict]:
    raw = " ".join((_fix_speech(text or "")).split())
    if not raw:
        return []
    text = normalize(raw)

    correction = _correction(text, raw)
    if correction:
        return [correction]

    memory = _memory_whole(text, raw)
    if memory:
        return [memory]

    parts = _multi_parts(text)
    if len(parts) > 1:
        merged: list[dict] = []
        for part in parts:
            if len(part.split()) < 2:
                merged = []
                break
            got = parse(part)
            if not got or got[0].get("kind") == "unknown":
                merged = []
                break
            merged.extend(got)
        if len(merged) > 1:
            return merged

    skill = _skill_intent(text)
    if skill:
        return [skill]

    reminder = _reminder(text)
    if reminder:
        return [reminder]

    chunks = split_commands(text)
    intents: list[dict] = []
    smalltalk: list[dict] = []
    last_open: dict | None = None
    for chunk in chunks:
        intent = _parse_chunk(chunk)
        if intent:
            if intent["kind"] == "smalltalk":
                smalltalk.append(intent)
                continue
            if intent["kind"] == "open" and intent.get("target"):
                last_open = intent
            intents.append(intent)
            continue
        # "открой вк и яндекс музыку": the second chunk has no verb, but it
        # names a known target, so it becomes another open command.
        if last_open and _is_known_target(chunk):
            extra = dict(last_open)
            extra["target"] = chunk
            extra["raw"] = chunk
            intents.append(extra)

    if not intents:
        recall = _recall_whole(text)
        if recall:
            return [recall]
        if smalltalk:
            # "привет, как дела" used to lose both halves. Prefer the question.
            chosen = next((s for s in smalltalk if s.get("sub") == "how_are_you"), smalltalk[0])
            return [dict(chosen, raw=raw)]
        intents.append({"kind": "unknown", "raw": raw})
    return intents
