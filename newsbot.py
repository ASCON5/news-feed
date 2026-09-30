#!/usr/bin/env python3
"""Собирает новости из RSS и Telegram-превью, убирает дубли, публикует в Telegram-канал.

Запуск:
    python newsbot.py --dry-run   # ничего не публикует и не сохраняет, только печатает
    python newsbot.py             # боевой режим (нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID)
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "data" / "state.json"
CONFIG_PATH = ROOT / "sources.yml"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|yclid$|ref$)", re.I)
NUM = re.compile(r"\d+(?:[.,]\d+)*")
PAUSE = 1.0  # пауза между источниками, чтобы не долбить сайты
TZ_HOURS = 5  # часовой пояс дат в постах (Узбекистан = UTC+5), задаётся в sources.yml

SECRETS: list[str] = []


def mask(text) -> str:
    """Прячет токены из любого текста, который уходит в лог."""
    text = str(text)
    for s in SECRETS:
        if s:
            text = text.replace(s, "***")
    return text


def log(msg) -> None:
    print(mask(msg), flush=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return (dt + timedelta(hours=TZ_HOURS)).strftime("%d.%m.%Y %H:%M")


RU_MONTHS = {"Jan": "янв", "Feb": "фев", "Mar": "мар", "Apr": "апр", "May": "мая", "Jun": "июн",
             "Jul": "июл", "Aug": "авг", "Sep": "сен", "Oct": "окт", "Nov": "ноя", "Dec": "дек"}
DEADLINE = re.compile(r"^Deadline:\s*([A-Za-z]+ \d{1,2}, \d{4})\s*")


def ru_dates(text: str) -> str:
    return re.sub(r"\b(" + "|".join(RU_MONTHS) + r")\b", lambda m: RU_MONTHS[m.group(1)], text)


def ru_left(text: str) -> str:
    m = re.search(r"(about |over |almost )?(\d+)\s+(minute|hour|day|week|month|year)s?", text.lower())
    if not m:
        return text
    unit = {"minute": "мин.", "hour": "ч.", "day": "дн.", "week": "нед.", "month": "мес.", "year": "г."}[m.group(3)]
    return f"Осталось: {'около ' if m.group(1) else ''}{m.group(2)} {unit}"


def split_deadline(summary: str):
    """'Deadline: October 25, 2026 Текст' -> ('Дедлайн: 25.10.2026\\nТекст', datetime)"""
    m = DEADLINE.match(summary)
    if not m:
        return summary, None
    try:
        d = datetime.strptime(m.group(1), "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return summary, None
    return f"Дедлайн: {d.strftime('%d.%m.%Y')}\n" + summary[m.end():], d


def topic_tags(item, topics, limit: int = 2) -> list[str]:
    blob = f"{item.title} {item.summary}".lower()
    found = [name for name, pats in (topics or {}).items() if any(re.search(p, blob) for p in pats)]
    return found[:limit]


def clean_img(url) -> str | None:
    if not url:
        return None
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http") or re.search(r"\.(svg|gif|ico)(\?|$)", url, re.I):
        return None
    return re.sub(r"(?<!:)/{2,}", "/", url)  # «site.org//media/x.jpg» -> «site.org/media/x.jpg»


def set_image(item: Item, src, url) -> None:
    if src.get("images", True) is not False:
        item.image = clean_img(url)


def rss_image(e) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            if m.get("url") and "video" not in str(m.get("type", "")):
                return m["url"]
    for enc in e.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    blobs = [c.get("value", "") for c in e.get("content") or []] + [e.get("summary", "")]
    for blob in blobs:
        for img in BeautifulSoup(blob or "", "html.parser").find_all("img"):
            w = str(img.get("width", "")).strip()
            if w.isdigit() and int(w) < 100:  # пиксели-счётчики и иконки
                continue
            if img.get("src"):
                return img["src"]
    return None


def tg_image(msg) -> str | None:
    for sel in (".tgme_widget_message_photo_wrap", ".link_preview_image", ".link_preview_right_image",
                ".tgme_widget_message_video_thumb"):
        el = msg.select_one(sel)
        if el is not None and el.get("style"):
            m = re.search(r"url\(['\"]?([^'\")]+)", el["style"])
            if m:
                return m.group(1)
    return None


# ---------- текст и ссылки ----------

def normalize_url(url: str) -> str:
    p = urlsplit(url.strip())
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", urlencode(q), ""))


def norm_title(t: str) -> str:
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def clean_text(raw) -> str:
    if not raw:
        return ""
    text = BeautifulSoup(str(raw), "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if m > limit * 0.5:
        return cut[: m + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:-— ") + "…"


@dataclass
class Item:
    source: str
    tag: str
    title: str
    url: str
    summary: str
    published: datetime
    also: list[str] = field(default_factory=list)
    kind: str = "news"  # "news" или "opportunity" (соревнования, гранты: свои лимиты)
    start: datetime | None = None  # для событий: старт или дедлайн (сортировка «ближайшие первыми»)
    tags: list[str] = field(default_factory=list)  # теги по теме (если пусто, берётся tag источника)
    preview: bool = True  # показывать ли карточку-превью ссылки
    image: str | None = None  # адрес картинки (если есть, пост уходит с фото)

    @property
    def key(self) -> str:
        return hashlib.sha1(normalize_url(self.url).encode()).hexdigest()[:16]


def make_item(src, title, url, summary, published) -> Item:
    t = now()
    # даты из будущего или отсутствующие заменяем временем получения
    if published is None or published > t + timedelta(minutes=5):
        published = t
    return Item(src["name"], src.get("tag", "новости"), title, url, summary, published,
                kind=src.get("kind", "news"), preview=src.get("preview", True))


# ---------- получение данных ----------

def http_get(url: str, retries: int = 3, timeout: int = 20) -> bytes:
    last = "?"
    for i in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"},
                             timeout=timeout)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = e.__class__.__name__
        if i < retries - 1:
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def fetch_rss(src) -> list[Item]:
    feed = feedparser.parse(http_get(src["url"]))
    if not feed.entries:
        raise RuntimeError("RSS пустой или не разобрался")
    items = []
    for e in feed.entries:
        link, title = e.get("link"), clean_text(e.get("title", ""))
        if not link or not title:
            continue
        summary = clean_text(e.get("summary") or e.get("description") or "")
        st = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime(*st[:6], tzinfo=timezone.utc) if st else None
        deadline = None
        if src.get("kind") == "opportunity":
            summary, deadline = split_deadline(summary)
            if deadline and deadline.date() < now().date():
                continue  # дедлайн уже прошёл
        it = make_item(src, title, link, summary, pub)
        it.start = deadline
        set_image(it, src, rss_image(e))
        items.append(it)
    return items


def tg_title(line: str) -> str:
    if len(line) <= 140:
        return line
    m = re.search(r"[.!?](\s|$)", line[:220])
    if m and m.end() > 40:
        return line[: m.end()].strip()
    return truncate(line, 140)


def parse_telegram(content: bytes, src) -> list[Item]:
    soup = BeautifulSoup(content, "html.parser")
    msgs = soup.select(".tgme_widget_message")
    if not msgs:
        raise RuntimeError("постов не найдено (канал закрыт для превью или изменилась вёрстка)")
    items = []
    for m in msgs:
        body = m.select_one(".tgme_widget_message_text")
        a = m.select_one("a.tgme_widget_message_date")
        if not body or not a or not a.get("href"):
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        lines = [ln.strip() for ln in body.get_text().split("\n") if ln.strip()]
        if not lines:
            continue
        lines[-1] = re.sub(r"(\s*@\w{4,})+\s*$", "", lines[-1]).strip()  # подпись «@канал»
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        time_tag = m.select_one("time[datetime]")
        pub = parse_iso(time_tag["datetime"]) if time_tag else None
        it = make_item(src, tg_title(lines[0]), a["href"], " ".join(lines), pub)
        set_image(it, src, tg_image(m))
        items.append(it)
    return items


def fetch_telegram(src) -> list[Item]:
    return parse_telegram(http_get(f"https://t.me/s/{src['channel']}"), src)


def fetch_ctftime(src) -> list[Item]:
    t = now()
    finish_ts = int((t + timedelta(days=int(src.get("days_ahead", 30)))).timestamp())
    data = json.loads(http_get(
        f"https://ctftime.org/api/v1/events/?limit=100&start={int(t.timestamp())}&finish={finish_ts}"))
    if not isinstance(data, list):
        raise RuntimeError("неожиданный ответ CTFtime")
    items = []
    for e in data:
        start, end = parse_iso(e.get("start", "")), parse_iso(e.get("finish", ""))
        link, title = e.get("ctftime_url") or e.get("url"), clean_text(e.get("title", ""))
        if not link or not title or not start:
            continue
        if float(e.get("weight") or 0) < float(src.get("min_weight", 0)):
            continue
        if src.get("online_only") and e.get("onsite"):
            continue
        where = f"очно ({e.get('location') or '?'})" if e.get("onsite") else "онлайн"
        weight = float(e.get("weight") or 0)
        raw = e.get("restrictions") or "?"
        restr = {"open": "открытое", "academic": "академическое (студенческие команды)",
                 "prequalified": "по отбору", "invite-only": "по приглашениям"}.get(raw.lower(), raw)
        dates = f"{fmt_dt(start)} — {fmt_dt(end)}" if end else fmt_dt(start)
        lines = [f"Даты: {dates} (UTC+{TZ_HOURS})",
                 f"Формат: {e.get('format') or '?'}, {where}",
                 (f"Рейтинг на CTFtime: {weight:g}" if weight > 0 else "Рейтинг на CTFtime: пока нет")
                 + f" · Участие: {restr}"]
        it = make_item(src, title, link, "\n".join(lines), None)
        it.start = start
        logo = e.get("logo") or ""
        set_image(it, src, logo if logo.startswith("http")
                  else ("https://ctftime.org/" + logo.lstrip("/") if logo else None))
        items.append(it)
    return items


def fetch_codeforces(src) -> list[Item]:
    data = json.loads(http_get("https://codeforces.com/api/contest.list?gym=false"))
    if data.get("status") != "OK":
        raise RuntimeError("Codeforces API вернул ошибку")
    horizon = now() + timedelta(days=int(src.get("days_ahead", 14)))
    items = []
    for c in data.get("result", []):
        if c.get("phase") != "BEFORE" or not c.get("startTimeSeconds"):
            continue
        start = datetime.fromtimestamp(c["startTimeSeconds"], tz=timezone.utc)
        if start > horizon:
            continue
        dur = int(c.get("durationSeconds") or 0)
        lines = [f"Старт: {fmt_dt(start)} (UTC+{TZ_HOURS})",
                 f"Длительность: {dur // 3600} ч {dur % 3600 // 60} мин",
                 "Формат: " + {"CF": "обычный раунд", "ICPC": "ICPC", "IOI": "IOI"}.get(
                     c.get("type"), c.get("type", "?"))]
        it = make_item(src, clean_text(c.get("name", "")), f"https://codeforces.com/contest/{c['id']}",
                       "\n".join(lines), None)
        it.start = start
        items.append(it)
    return items


def fetch_devpost(src) -> list[Item]:
    hacks: list[dict] = []
    for page in (1, 2, 3):  # сортировка по дедлайну: на первой странице те, что вот-вот закончатся
        try:
            data = json.loads(http_get(
                f"https://devpost.com/api/hackathons?status[]=open&status[]=upcoming&order_by=deadline&page={page}"))
        except Exception:
            if page == 1:
                raise
            break
        chunk = data.get("hackathons") if isinstance(data, dict) else None
        if not chunk:
            break
        hacks += chunk
        time.sleep(PAUSE)
    if not hacks:
        raise RuntimeError("пустой ответ Devpost")
    items = []
    seen_urls: set[str] = set()
    for h in hacks:
        title, link = clean_text(h.get("title", "")), h.get("url")
        if not title or not link or link in seen_urls:
            continue
        seen_urls.add(link)
        loc = clean_text((h.get("displayed_location") or {}).get("location", ""))
        if loc and "online" not in loc.lower():  # очные за границей не нужны
            continue
        left = clean_text(h.get("time_left_to_submission", "")).lower()
        if re.search(r"\b(hour|minute)s?\b|\b1 day\b", left):  # до конца сутки или меньше, поздно
            continue
        lines = []
        if h.get("submission_period_dates"):
            lines.append("Даты: " + ru_dates(clean_text(h["submission_period_dates"])))
        if left:
            lines.append(ru_left(left))
        prize = clean_text(h.get("prize_amount", ""))
        if re.sub(r"\D", "", prize).strip("0"):  # «$ 0» не показываем
            lines.append("Призы: " + prize)
        themes = ", ".join(clean_text(x.get("name", "")) for x in h.get("themes") or [] if isinstance(x, dict))
        if themes:
            lines.append("Темы: " + themes)
        it = make_item(src, title, link, "\n".join(lines), None)
        set_image(it, src, h.get("thumbnail_url"))
        items.append(it)
    return items


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram, "ctftime": fetch_ctftime,
            "codeforces": fetch_codeforces, "devpost": fetch_devpost}


def collect(cfg):
    items, report = [], {}
    for src in cfg["sources"]:
        if src.get("enabled", True) is False:
            continue
        name = src["name"]
        try:
            got = FETCHERS[src["type"]](src)
            items += got
            report[name] = len(got)
        except Exception as e:  # один сбойный источник не должен ронять остальные
            report[name] = f"ОШИБКА: {mask(e)}"
            log(f"::warning::{name}: {mask(e)}")
        time.sleep(PAUSE)
    return items, report


# ---------- фильтры, дубли, отбор ----------

def is_ad(item: Item, words) -> bool:
    blob = f"{item.title} {item.summary}".lower()
    return any(w in blob for w in words)


def similar(a: str, b: str, thr: float) -> bool:
    if not a or not b:
        return False
    na, nb = set(NUM.findall(a)), set(NUM.findall(b))
    if na and nb and na != nb:  # «Python 3.12» и «Python 3.13» — разные новости
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= thr


def budget(state, key, per_run, per_day, t):
    day_ago = t - timedelta(hours=24)
    sent = sum(1 for x in state.get(key, []) if (parse_iso(x) or t) >= day_ago)
    return max(0, min(per_run, per_day - sent))


def sort_key(i: Item):
    return i.start or i.published


def round_robin(items, limit, key=lambda i: i.published, reverse=True):
    groups: dict[str, list[Item]] = {}
    for it in sorted(items, key=key, reverse=reverse):
        groups.setdefault(it.source, []).append(it)
    out: list[Item] = []
    while len(out) < limit and any(groups.values()):
        for g in groups.values():
            if g and len(out) < limit:
                out.append(g.pop(0))
    return out


def select(items, cfg, state):
    s = cfg["settings"]
    t = now()
    cutoff = t - timedelta(hours=s["max_age_hours"])
    ads = [w.lower() for w in cfg.get("ad_words", [])]
    fresh = [i for i in items
             if i.published >= cutoff
             and (i.kind == "opportunity" or len(i.title) >= s["min_title_len"])
             and not is_ad(i, ads) and i.key not in state["seen"]]
    fresh.sort(key=lambda i: i.published)

    thr = s["title_similarity"]
    known = [x["t"] for x in state["titles"]]
    kept: list[Item] = []
    for it in fresh:
        nt = norm_title(it.title)
        if any(k.key == it.key for k in kept):
            continue
        if any(similar(nt, k, thr) for k in known):
            continue
        twin = next((k for k in kept if similar(nt, norm_title(k.title), thr)), None)
        if twin:
            if it.source != twin.source and it.source not in twin.also:
                twin.also.append(it.source)
            continue
        kept.append(it)

    news = [i for i in kept if i.kind != "opportunity"]
    opps = [i for i in kept if i.kind == "opportunity"]
    n_lim = budget(state, "sent_log", s["max_posts_per_run"], s["max_posts_per_day"], t)
    o_lim = budget(state, "sent_log_opp", s.get("max_opportunities_per_run", 5),
                   s.get("max_opportunities_per_day", 12), t)
    picks = sorted(round_robin(news, n_lim), key=lambda i: i.published)
    picks += sorted(round_robin(opps, o_lim, key=sort_key, reverse=False), key=sort_key)
    return picks


# ---------- формат и отправка ----------

def format_post(item: Item, max_summary: int = 350) -> str:
    summary = item.summary
    base = item.title.rstrip("…")
    if summary.startswith(base):
        summary = summary[len(base):].lstrip(" .:-—")
    summary = truncate(summary, max_summary)
    tag_line = " ".join("#" + re.sub(r"\W", "_", t) for t in (item.tags or [item.tag]))
    icon = "🎯 " if item.kind == "opportunity" else ""
    parts = [f"<b>{icon}{html.escape(item.title)}</b>"]
    if summary:
        parts.append(html.escape(summary))
    foot = f'{tag_line} · <a href="{html.escape(item.url, quote=True)}">🔗 {html.escape(item.source)}</a>'
    if item.also:
        foot += "\nТакже: " + ", ".join(html.escape(x) for x in item.also)
    parts.append(foot)
    return "\n\n".join(parts)


class TelegramError(Exception):
    pass


def _call(token: str, method: str, payload: dict, text_key: str) -> None:
    url = f"https://api.telegram.org/bot{token}/{method}"
    for _ in range(4):
        try:
            r = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            raise TelegramError(f"сеть: {e.__class__.__name__}")
        if r.status_code == 200:
            return
        try:
            data = r.json()
        except ValueError:
            data = {}
        desc = data.get("description", "")
        if r.status_code == 429:
            wait = int(data.get("parameters", {}).get("retry_after", 5))
            if wait > 120:
                raise TelegramError(f"лимит Telegram, ждать {wait} с")
            time.sleep(wait + 1)
            continue
        if r.status_code == 400 and "parse entities" in desc and "parse_mode" in payload:
            payload.pop("parse_mode")
            payload[text_key] = BeautifulSoup(payload[text_key], "html.parser").get_text()[:4096]
            continue
        raise TelegramError(f"{r.status_code} {desc}")
    raise TelegramError("слишком много повторов")


def send(token: str, chat_id: str, text: str, link_preview: bool = True, image: str | None = None) -> None:
    if image:
        try:
            _call(token, "sendPhoto", {"chat_id": chat_id, "photo": image, "caption": text[:1024],
                                       "parse_mode": "HTML"}, "caption")
            return
        except TelegramError as e:
            if not str(e).startswith("400"):  # сеть, лимиты и прочее не глотаем
                raise
            log(f"Фото не прошло ({mask(e)}), отправляю без фото")
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
                                 "link_preview_options": {"is_disabled": not link_preview}}, "text")


def render(item: Item, s: dict, with_image: bool) -> str:
    """Подпись к фото ограничена 1024 символами, поэтому при необходимости сокращаем резюме."""
    limit = 1000 if with_image else 4000
    text = ""
    for m in (s["max_summary_chars"], 200, 100, 0):
        text = format_post(item, m)
        if len(text) <= limit:
            return text
    return text[:limit]


# ---------- состояние ----------

def load_state() -> dict:
    base = {"initialized": False, "seen": {}, "titles": [], "sent_log": [], "sent_log_opp": [], "last_run": ""}
    if STATE_PATH.exists():
        base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    return base


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


def mark_seen(state: dict, item: Item) -> None:
    ts = iso(now())
    state["seen"][item.key] = ts
    state["titles"].append({"t": norm_title(item.title), "ts": ts})


def finish(state: dict, s: dict) -> None:
    t = now()
    old = t - timedelta(days=s["state_days"])
    old_titles = t - timedelta(days=min(s["state_days"], s.get("title_memory_days", 30)))
    state["seen"] = {k: v for k, v in state["seen"].items() if (parse_iso(v) or t) >= old}
    state["titles"] = [x for x in state["titles"] if (parse_iso(x["ts"]) or t) >= old_titles]
    for k in ("sent_log", "sent_log_opp"):
        state[k] = [x for x in state.get(k, []) if (parse_iso(x) or t) >= t - timedelta(days=2)]
    state["last_run"] = t.strftime("%Y-%m-%d")  # раз в сутки: поддерживает активность репозитория
    save_state(state)


# ---------- запуск ----------

def run(cfg, state, dry, sender) -> int:
    global TZ_HOURS
    s = cfg["settings"]
    TZ_HOURS = s.get("timezone_offset_hours", 5)
    items, report = collect(cfg)
    log("Источники:")
    for name, r in report.items():
        log(f"  {name}: {r}")
    if not items:
        log("::error::Ни один источник не отдал данные")
        return 1

    if not dry and not state["initialized"]:
        for it in items:
            mark_seen(state, it)
        state["initialized"] = True
        finish(state, s)
        log(f"Инициализация: {len(items)} записей помечено как виденные, ничего не опубликовано.")
        return 0

    picks = select(items, cfg, state)
    log(f"К публикации: {len(picks)}")
    for it in picks:
        if it.kind != "opportunity":
            it.tags = topic_tags(it, cfg.get("topics"))
        with_image = bool(it.image) and s.get("images", True)
        text = render(it, s, with_image)
        if dry:
            log("-----\n" + BeautifulSoup(text, "html.parser").get_text()
                + (f"\n[фото] {it.image}" if with_image else ""))
            continue
        try:
            sender(text, it.preview, it.image if with_image else None)
        except TelegramError as e:
            log(f"::error::Telegram: {e}")
            finish(state, s)
            return 1
        mark_seen(state, it)  # сразу после отправки, чтобы сбой дальше не вызвал дубль
        state["sent_log_opp" if it.kind == "opportunity" else "sent_log"].append(iso(now()))
        save_state(state)
        time.sleep(s["delay_seconds"])
    if not dry:
        finish(state, s)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args(argv)
    dry = args.dry_run or os.getenv("DRY_RUN", "").lower() == "true"
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    SECRETS.append(token)
    if not dry and not (token and chat):
        log("::error::Нет TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID (Settings → Secrets → Actions)")
        return 2
    sender = None if dry else (lambda text, preview=True, image=None: send(
        token, chat, text, preview and cfg["settings"].get("link_preview", True), image))
    return run(cfg, load_state(), dry, sender)


if __name__ == "__main__":
    sys.exit(main())
