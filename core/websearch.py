"""Minimal web search via DuckDuckGo HTML, no API key, stdlib only."""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
QUALITY = {
    "wikipedia.org": 6, "ru.wikipedia.org": 6, "government.ru": 4, "ria.ru": 4,
    "tass.ru": 4, "rbc.ru": 4, "kommersant.ru": 4, "forbes.ru": 3, "vedomosti.ru": 3,
    "habr.com": 3, "stackoverflow.com": 3, "mos.ru": 3, "lenta.ru": 2, "russian.rt.com": 2,
}


def _text(raw: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def _target(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    if "/l/?" in href:
        query = urllib.parse.urlparse(href).query
        href = urllib.parse.parse_qs(query).get("uddg", [href])[0]
    return href


def domain(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def search(query: str, limit: int = 6, timeout: float = 8.0) -> list[dict]:
    if not (query or "").strip():
        return []
    page = ""
    for base in ("https://html.duckduckgo.com/html/?q=", "https://lite.duckduckgo.com/lite/?q="):
        request = urllib.request.Request(base + urllib.parse.quote(query), headers={"User-Agent": UA})
        try:
            page = urllib.request.urlopen(request, timeout=timeout).read().decode("utf-8", "replace")
        except (OSError, urllib.error.URLError):
            continue
        if 'class="result' in page or 'result-link' in page:
            break
    results: list[dict] = []
    for block in re.split(r'class="result results_links', page)[1:]:
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        target = _target(html.unescape(link.group(1)))
        host = domain(target)
        if not host or host.endswith("duckduckgo.com"):
            continue
        results.append({
            "title": _text(link.group(2))[:160],
            "url": target,
            "domain": host,
            "snippet": _text(snippet.group(1))[:320] if snippet else "",
        })
    results.sort(key=lambda item: -QUALITY.get(item["domain"], 0))
    unique, seen = [], set()
    for item in results:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        unique.append(item)
    return unique[:limit]


def _rss_items(url: str, timeout: float) -> list[dict]:
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout
    ).read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        host = domain(link)
        items.append({"title": title, "url": link, "domain": host or "news", "snippet": title})
    return items


TECH_FEEDS = (
    "https://kod.ru/rss",
    "https://www.ixbt.com/export/news.rss",
    "https://3dnews.ru/news/rss/",
    "https://habr.com/ru/rss/news/",
)
GENERAL_FEEDS = (
    "https://lenta.ru/rss/news",
    "https://www.ixbt.com/export/news.rss",
)
_TECH_WORDS = ("техник", "технолог", "гаджет", "компьютер", "смартфон", "нейросет",
               "искусственн", "it-", "айти", "процессор", "видеокарт")


def _feed_items(url: str, timeout: float) -> list[dict]:
    raw = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout
    ).read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        description = _text(item.findtext("description") or "")[:300]
        items.append({"title": title, "url": link, "domain": domain(link) or "news",
                      "snippet": description or title})
    return items


def news(query: str, limit: int = 6, timeout: float = 8.0,
         feeds: tuple[str, ...] | list[str] | None = None) -> list[dict]:
    """Headlines straight from news RSS feeds — no search engine in between."""
    low = query.lower()
    feeds = tuple(feeds) if feeds else (
        TECH_FEEDS if any(word in low for word in _TECH_WORDS) else GENERAL_FEEDS
    )
    results: list[dict] = []
    for url in feeds:
        try:
            results.extend(_feed_items(url, timeout))
        except (OSError, urllib.error.URLError, ET.ParseError):
            continue
        if len(results) >= limit:
            break
    if not results:
        for url in (
            "https://www.bing.com/news/search?q=" + urllib.parse.quote(query) + "&format=RSS&setlang=ru",
            "https://news.google.com/rss/search?q=" + urllib.parse.quote(query) + "&hl=ru&gl=RU&ceid=RU:ru",
        ):
            try:
                results.extend(_feed_items(url, timeout))
            except (OSError, urllib.error.URLError, ET.ParseError):
                continue
            if results:
                break
    return results[:limit]
