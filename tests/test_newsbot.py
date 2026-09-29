import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import newsbot as nb  # noqa: E402

nb.PAUSE = 0


def rfc(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>{d}</description>"
        f"<pubDate>{rfc(p)}</pubDate></item>" for t, l, d, p in items)
    return (f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
            f"<title>x</title>{body}</channel></rss>").encode()


def tg(posts):
    out = ""
    for i, (text, p) in enumerate(posts, 1):
        out += (f'<div class="tgme_widget_message" data-post="campcode/{i}">'
                f'<div class="tgme_widget_message_text">{text}</div>'
                f'<a class="tgme_widget_message_date" href="https://t.me/campcode/{i}">'
                f'<time datetime="{p.isoformat()}"></time></a></div>')
    return out.encode()


class Units(unittest.TestCase):
    def test_normalize_url(self):
        a = nb.normalize_url("http://www.Site.ru/news/1/?utm_source=x&id=5#top")
        b = nb.normalize_url("https://site.ru/news/1?id=5")
        self.assertEqual(a, b)

    def test_similar(self):
        n = nb.norm_title
        self.assertTrue(nb.similar(n("Вышла новая версия Claude"), n("Вышла новая версия Claude от Anthropic"), 0.75))
        self.assertFalse(nb.similar(n("Вышел Python 3.12"), n("Вышел Python 3.13"), 0.75))
        self.assertFalse(nb.similar(n("Вышла новая версия Claude"), n("Землетрясение в Японии"), 0.75))

    def test_ad_filter(self):
        it = nb.Item("s", "t", "Заголовок", "https://a.ru/1", "Купи по промокоду ABC", nb.now())
        self.assertTrue(nb.is_ad(it, ["промокод"]))

    def test_format_escapes_and_no_title_repeat(self):
        it = nb.Item("Src", "разработка", "A <b> & B новость", "https://a.ru/?a=1&b=2",
                     "A <b> & B новость. Подробности тут.", nb.now(), also=["Другой"])
        text = nb.format_post(it)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("&amp;b=2", text)
        self.assertEqual(text.count("новость"), 1 + 0)  # заголовок не повторяется в резюме
        self.assertIn("Также: Другой", text)
        self.assertLess(len(text), 4096)

    def test_parse_telegram(self):
        t = nb.now() - timedelta(hours=1)
        items = nb.parse_telegram(tg([("Заголовок поста<br/>Вторая строка", t)]), {"name": "CC", "tag": "x"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Заголовок поста")
        self.assertEqual(items[0].url, "https://t.me/campcode/1")

    def test_parse_telegram_broken_layout_is_loud(self):
        with self.assertRaises(RuntimeError):
            nb.parse_telegram(b"<html>nothing</html>", {"name": "CC"})

    def test_future_date_replaced(self):
        it = nb.make_item({"name": "s"}, "t", "u", "", nb.now() + timedelta(days=5))
        self.assertLess(it.published, nb.now() + timedelta(minutes=1))

    def test_mask(self):
        nb.SECRETS.append("SECRET123")
        self.assertNotIn("SECRET123", nb.mask("url https://api.telegram.org/botSECRET123/x"))
        nb.SECRETS.remove("SECRET123")


class FakeResp:
    def __init__(self, code, data=None):
        self.status_code, self._d = code, data or {}

    def json(self):
        return self._d


class SendTests(unittest.TestCase):
    def test_429_then_ok(self):
        seq = [FakeResp(429, {"parameters": {"retry_after": 1}}), FakeResp(200)]
        with mock.patch.object(nb.requests, "post", side_effect=seq), mock.patch.object(nb.time, "sleep"):
            nb.send("tok", "@c", "hi")

    def test_parse_error_falls_back_to_plain(self):
        calls = []

        def post(url, json, timeout):
            calls.append(dict(json))
            return FakeResp(400, {"description": "can't parse entities"}) if len(calls) == 1 else FakeResp(200)

        with mock.patch.object(nb.requests, "post", side_effect=post):
            nb.send("tok", "@c", "<b>Заголовок</b>")
        self.assertNotIn("parse_mode", calls[1])
        self.assertEqual(calls[1]["text"], "Заголовок")

    def test_other_error_raises(self):
        with mock.patch.object(nb.requests, "post", return_value=FakeResp(403, {"description": "not enough rights"})):
            with self.assertRaises(nb.TelegramError):
                nb.send("tok", "@c", "hi")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(nb, "STATE_PATH", Path(self.tmp.name) / "state.json")]
        self.pages = {}
        self.patches.append(mock.patch.object(nb, "http_get", lambda url, **k: self.pages[url]))
        for p in self.patches:
            p.start()
        self.cfg = {
            "settings": dict(max_age_hours=24, min_title_len=15, title_similarity=0.75,
                             max_posts_per_run=5, max_posts_per_day=20, delay_seconds=0,
                             state_days=30, max_summary_chars=300),
            "ad_words": ["промокод"],
            "sources": [
                {"name": "A", "type": "rss", "url": "rssA", "tag": "разработка"},
                {"name": "B", "type": "rss", "url": "rssB", "tag": "наука"},
                {"name": "CC", "type": "telegram", "channel": "campcode", "tag": "разработка"},
                {"name": "Dead", "type": "rss", "url": "dead", "tag": "x"},
            ],
        }
        self.sent = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def set_pages(self, a, b, c):
        self.pages["rssA"], self.pages["rssB"] = rss(a), rss(b)
        self.pages["https://t.me/s/campcode"] = tg(c)

        def dead(*_):
            raise RuntimeError("dead: HTTP 500")
        self.pages["dead"] = None

    def go(self, dry=False):
        # «мёртвый» источник: http_get падает
        orig = self.pages

        def getter(url, **k):
            if url == "dead":
                raise RuntimeError("dead: HTTP 500")
            return orig[url]

        with mock.patch.object(nb, "http_get", getter):
            return nb.run(self.cfg, nb.load_state(), dry, self.sent.append)

    def test_full_flow(self):
        t = nb.now()
        old = [("Старая новость про Rust и компиляторы", "https://a.ru/old", "d", t - timedelta(hours=2))]
        self.set_pages(old, [], [("Пост из телеграм канала про Go", t - timedelta(hours=1))])

        # 1) первый боевой запуск = инициализация, ничего не публикуется, мёртвый источник не мешает
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.sent, [])
        self.assertTrue(nb.load_state()["initialized"])

        # 2) новая новость + её дубль из другого источника + реклама + повтор старой
        new = old + [
            ("Вышла новая версия Claude 5", "https://a.ru/new?utm_source=x", "Подробности релиза.", t),
            ("Реклама: купи по промокоду ХХХ", "https://a.ru/ad", "", t),
        ]
        dup = [("Вышла новая версия Claude 5 от Anthropic", "https://b.ru/other", "", t)]
        self.set_pages(new, dup, [("Пост из телеграм канала про Go", t - timedelta(hours=1))])
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Claude 5", self.sent[0])
        self.assertIn("Также: B", self.sent[0])

        # 3) повторный запуск с теми же данными ничего не шлёт
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_writes_nothing(self):
        t = nb.now()
        self.set_pages([("Свежая новость для проверки", "https://a.ru/1", "", t)], [], [("Пост про что-то важное", t)])
        self.assertEqual(self.go(dry=True), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(nb.STATE_PATH.exists())

    def test_daily_cap(self):
        t = nb.now()
        self.cfg["settings"]["max_posts_per_run"] = 2
        many = [(f"Уникальная тема номер {i} про {w}", f"https://a.ru/{i}", "", t)
                for i, w in enumerate(["кошек", "ракеты", "вино", "футбол", "шахматы"])]
        self.set_pages([], [], [("Пост чтобы инициализироваться", t - timedelta(hours=3))])
        self.go()  # инициализация
        self.set_pages(many, [], [("Пост чтобы инициализироваться", t - timedelta(hours=3))])
        self.go()
        self.assertEqual(len(self.sent), 2)

    def test_telegram_failure_does_not_mark_seen(self):
        t = nb.now()
        self.set_pages([], [], [("Пост чтобы инициализироваться", t - timedelta(hours=3))])
        self.go()
        self.set_pages([("Новость которая не отправится", "https://a.ru/x", "", t)], [], [("Пост чтобы инициализироваться", t - timedelta(hours=3))])

        def boom(_):
            raise nb.TelegramError("500")
        state = nb.load_state()

        def getter(url, **k):
            if url == "dead":
                raise RuntimeError("x")
            return self.pages[url]
        with mock.patch.object(nb, "http_get", getter):
            self.assertEqual(nb.run(self.cfg, state, False, boom), 1)
        self.assertEqual(len(nb.load_state()["seen"]), 1)  # только инициализационный пост


if __name__ == "__main__":
    unittest.main(verbosity=2)
