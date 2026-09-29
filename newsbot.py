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

    @property
    def key(self) -> str:
        return hashlib.sha1(normalize_url(self.url).encode()).hexdigest()[:16]


def make_item(src, title, url, summary, published) -> Item:
    t = now()
    # даты из будущего или отсутствующие заменяем временем получения
    if published is None or published > t + timedelta(minutes=5):
        published = t
    return Item(src["name"], src.get("tag", "новости"), title, url, summary, published)


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
        items.append(make_item(src, title, link, summary, pub))
    return items


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
        time_tag = m.select_one("time[datetime]")
        pub = parse_iso(time_tag["datetime"]) if time_tag else None
        items.append(make_item(src, truncate(lines[0], 140), a["href"], " ".join(lines), pub))
    return items


def fetch_telegram(src) -> list[Item]:
    return parse_telegram(http_get(f"https://t.me/s/{src['channel']}"), src)


FETCHERS = {"rss": fetch_rss, "telegram": fetch_telegram}


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


def round_robin(items, limit):
    groups: dict[str, list[Item]] = {}
    for it in sorted(items, key=lambda i: i.published, reverse=True):
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
             if i.published >= cutoff and len(i.title) >= s["min_title_len"]
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

    day_ago = t - timedelta(hours=24)
    sent24 = sum(1 for x in state["sent_log"] if (parse_iso(x) or t) >= day_ago)
    limit = max(0, min(s["max_posts_per_run"], s["max_posts_per_day"] - sent24))
    picks = round_robin(kept, limit)
    picks.sort(key=lambda i: i.published)
    return picks


# ---------- формат и отправка ----------

def format_post(item: Item, max_summary: int = 350) -> str:
    summary = item.summary
    base = item.title.rstrip("…")
    if summary.startswith(base):
        summary = summary[len(base):].lstrip(" .:-—")
    summary = truncate(summary, max_summary)
    tag = re.sub(r"\W", "_", item.tag)
    parts = [f"<b>{html.escape(item.title)}</b>"]
    if summary:
        parts.append(html.escape(summary))
    foot = f'#{tag} · <a href="{html.escape(item.url, quote=True)}">{html.escape(item.source)}</a>'
    if item.also:
        foot += "\nТакже: " + ", ".join(html.escape(x) for x in item.also)
    parts.append(foot)
    return "\n\n".join(parts)


class TelegramError(Exception):
    pass


def send(token: str, chat_id: str, text: str, link_preview: bool = True) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
               "link_preview_options": {"is_disabled": not link_preview}}
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
            payload["text"] = BeautifulSoup(text, "html.parser").get_text()[:4096]
            continue
        raise TelegramError(f"{r.status_code} {desc}")
    raise TelegramError("слишком много повторов")


# ---------- состояние ----------

def load_state() -> dict:
    base = {"initialized": False, "seen": {}, "titles": [], "sent_log": [], "last_run": ""}
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


def finish(state: dict, days: int) -> None:
    t = now()
    old = t - timedelta(days=days)
    state["seen"] = {k: v for k, v in state["seen"].items() if (parse_iso(v) or t) >= old}
    state["titles"] = [x for x in state["titles"] if (parse_iso(x["ts"]) or t) >= old]
    state["sent_log"] = [x for x in state["sent_log"] if (parse_iso(x) or t) >= t - timedelta(days=2)]
    state["last_run"] = t.strftime("%Y-%m-%d")  # раз в сутки: поддерживает активность репозитория
    save_state(state)


# ---------- запуск ----------

def run(cfg, state, dry, sender) -> int:
    s = cfg["settings"]
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
        finish(state, s["state_days"])
        log(f"Инициализация: {len(items)} записей помечено как виденные, ничего не опубликовано.")
        return 0

    picks = select(items, cfg, state)
    log(f"К публикации: {len(picks)}")
    for it in picks:
        text = format_post(it, s["max_summary_chars"])
        if dry:
            log("-----\n" + BeautifulSoup(text, "html.parser").get_text())
            continue
        try:
            sender(text)
        except TelegramError as e:
            log(f"::error::Telegram: {e}")
            finish(state, s["state_days"])
            return 1
        mark_seen(state, it)  # сразу после отправки, чтобы сбой дальше не вызвал дубль
        state["sent_log"].append(iso(now()))
        save_state(state)
        time.sleep(s["delay_seconds"])
    if not dry:
        finish(state, s["state_days"])
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
    sender = None if dry else (lambda text: send(token, chat, text, cfg["settings"].get("link_preview", True)))
    return run(cfg, load_state(), dry, sender)


if __name__ == "__main__":
    sys.exit(main())
