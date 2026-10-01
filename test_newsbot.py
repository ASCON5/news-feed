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

    def test_telegram_long_first_sentence_not_cut_midword(self):
        first = ("Приоритизацию задач теперь можно доверить мухе: команда взяла схему нервной системы "
                 "дрозофилы и поручила ей самое сложное, решать, с чего начать день.")
        body = first + " Сервис работает проще пареной репы."
        items = nb.parse_telegram(tg([(body, nb.now())]), {"name": "CC", "tag": "x"})
        self.assertEqual(items[0].title, first)
        text = nb.format_post(items[0])
        self.assertNotIn("…", text.split("\n")[0])
        self.assertIn("Сервис работает", text)

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
            return nb.run(self.cfg, nb.load_state(), dry, lambda text, p=True, img=None: self.sent.append(text))

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

        def boom(*_):
            raise nb.TelegramError("500")
        state = nb.load_state()

        def getter(url, **k):
            if url == "dead":
                raise RuntimeError("x")
            return self.pages[url]
        with mock.patch.object(nb, "http_get", getter):
            self.assertEqual(nb.run(self.cfg, state, False, boom), 1)
        self.assertEqual(len(nb.load_state()["seen"]), 1)  # только инициализационный пост


import json  # noqa: E402


class Opportunities(unittest.TestCase):
    def fake(self, payload):
        return mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode())

    def test_ctftime(self):
        t = nb.now()
        payload = [{"title": "DiceCTF 2026", "ctftime_url": "https://ctftime.org/event/1", "url": "https://dice.ctf",
                    "start": (t + timedelta(days=3)).isoformat(), "finish": (t + timedelta(days=5)).isoformat(),
                    "format": "Jeopardy", "onsite": False, "location": "", "restrictions": "Open", "weight": 24.5}]
        with self.fake(payload):
            items = nb.fetch_ctftime({"name": "CTFtime", "tag": "ctf", "kind": "opportunity"})
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "opportunity")
        self.assertIsNotNone(items[0].start)
        self.assertIn("Jeopardy", items[0].summary)
        self.assertIn("онлайн", items[0].summary)

    def test_ctftime_bad_response_is_loud(self):
        with self.fake({"error": "x"}):
            with self.assertRaises(RuntimeError):
                nb.fetch_ctftime({"name": "CTFtime"})

    def test_codeforces_only_upcoming(self):
        t = nb.now()
        payload = {"status": "OK", "result": [
            {"id": 2001, "name": "Codeforces Round 1051 (Div. 2)", "type": "CF", "phase": "BEFORE",
             "durationSeconds": 7200, "startTimeSeconds": int((t + timedelta(days=2)).timestamp())},
            {"id": 2002, "name": "Far away round", "type": "CF", "phase": "BEFORE",
             "durationSeconds": 7200, "startTimeSeconds": int((t + timedelta(days=90)).timestamp())},
            {"id": 1, "name": "Old", "type": "CF", "phase": "FINISHED", "startTimeSeconds": 1000}]}
        with self.fake(payload):
            items = nb.fetch_codeforces({"name": "Codeforces", "kind": "opportunity", "days_ahead": 14})
        self.assertEqual([i.url for i in items], ["https://codeforces.com/contest/2001"])
        self.assertIn("2 ч 0 мин", items[0].summary)

    def test_devpost_online_only(self):
        payload = {"hackathons": [
            {"title": "AI Hack", "url": "https://ai.devpost.com/", "displayed_location": {"location": "Online"},
             "submission_period_dates": "Oct 01 - 29, 2026", "prize_amount": "$<span>25,000</span>",
             "themes": [{"name": "AI"}]},
            {"title": "Paris Hack", "url": "https://p.devpost.com/", "displayed_location": {"location": "Paris, France"}},
            {"title": "Almost Over", "url": "https://o.devpost.com/", "displayed_location": {"location": "Online"},
             "time_left_to_submission": "about 3 hours left"}]}
        with self.fake(payload):
            items = nb.fetch_devpost({"name": "Devpost", "kind": "opportunity"})
        self.assertEqual([i.title for i in items], ["AI Hack"])
        self.assertIn("25,000", items[0].summary)

    def test_separate_caps_short_titles_soonest_first(self):
        t = nb.now()
        cfg = {"settings": dict(max_age_hours=24, min_title_len=15, title_similarity=0.75,
                                max_posts_per_run=5, max_posts_per_day=20,
                                max_opportunities_per_run=2, max_opportunities_per_day=12), "ad_words": []}
        state = {"seen": {}, "titles": [], "sent_log": [], "sent_log_opp": []}
        items = [nb.Item("N", "x", f"Длинная новость номер {i} про {w}", f"https://n.ru/{i}", "", t)
                 for i, w in enumerate(["кошек", "ракеты", "вино"])]
        items += [nb.Item("CTF", "ctf", name, f"https://c.ru/{i}", "", t, kind="opportunity",
                          start=t + timedelta(days=d))
                  for i, (name, d) in enumerate([("Alpha Cup", 9), ("DiceCTF 2026", 3), ("Gamma Quest", 5)])]
        picks = nb.select(items, cfg, state)
        opps = [p for p in picks if p.kind == "opportunity"]
        self.assertEqual([p.title for p in opps], ["DiceCTF 2026", "Gamma Quest"])  # ближайшие, короткое имя прошло
        self.assertEqual(len([p for p in picks if p.kind == "news"]), 3)  # новости не вытеснены

    def test_opportunity_format_has_icon(self):
        it = nb.Item("CTFtime", "ctf", "DiceCTF 2026", "https://ctftime.org/event/1", "Даты: 1\nФормат: Jeopardy",
                     nb.now(), kind="opportunity")
        text = nb.format_post(it)
        self.assertIn("🎯 DiceCTF 2026", text)
        self.assertIn("#ctf", text)
        self.assertIn("Формат: Jeopardy", text)


class Polish(unittest.TestCase):
    def test_topic_tags(self):
        topics = {"ИИ": [r"anthropic", r"\bии\b"], "разработка": [r"\bgo\b", r"\bphp\b"], "игры": [r"steam"]}
        mk = lambda title, summ="": nb.Item("s", "железо", title, "https://a.ru/1", summ, nb.now())  # noqa: E731
        self.assertEqual(nb.topic_tags(mk("Anthropic собираются на IPO"), topics), ["ИИ"])
        self.assertEqual(nb.topic_tags(mk("Go-версия платформы быстрее PHP-аналога"), topics), ["разработка"])
        self.assertEqual(nb.topic_tags(mk("Термос от Xiaomi"), topics), [])  # тогда берётся тег источника

    def test_tags_in_post(self):
        it = nb.Item("Src", "железо", "Заголовок", "https://a.ru/1", "", nb.now(), tags=["ИИ", "безопасность"])
        self.assertIn("#ИИ #безопасность", nb.format_post(it))
        it.tags = []
        self.assertIn("#железо", nb.format_post(it))

    def test_ru_helpers(self):
        self.assertEqual(nb.ru_dates("Sep 15 - 30, 2026"), "сен 15 - 30, 2026")
        self.assertEqual(nb.ru_left("about 3 months left"), "Осталось: около 3 мес.")
        self.assertEqual(nb.ru_left("5 days left"), "Осталось: 5 дн.")

    def test_split_deadline(self):
        text, d = nb.split_deadline("Deadline: October 25, 2026 Applications are open.")
        self.assertEqual(text, "Дедлайн: 25.10.2026\nApplications are open.")
        self.assertEqual((d.year, d.month, d.day), (2026, 10, 25))
        self.assertEqual(nb.split_deadline("Без дедлайна"), ("Без дедлайна", None))

    def test_rss_opportunity_expired_deadline_skipped(self):
        t = nb.now()
        xml = rss([("Старая стипендия 2019", "https://g.org/old", "Deadline: January 05, 2020 Apply now", t),
                   ("Новая стипендия 2099", "https://g.org/new", "Deadline: December 31, 2099 Apply now", t)])
        with mock.patch.object(nb, "http_get", lambda url, **k: xml):
            items = nb.fetch_rss({"name": "G", "tag": "гранты", "kind": "opportunity", "url": "x"})
        self.assertEqual([i.title for i in items], ["Новая стипендия 2099"])
        self.assertIn("Дедлайн: 31.12.2099", items[0].summary)
        self.assertEqual(items[0].start.year, 2099)

    def test_devpost_cleanup(self):
        payload = {"hackathons": [
            {"title": "Zero Prize", "url": "https://z.devpost.com/", "displayed_location": {"location": "Online"},
             "submission_period_dates": "Sep 15 - Oct 30, 2026", "time_left_to_submission": "about 1 month left",
             "prize_amount": "$ <span>0</span>"},
            {"title": "Last Day", "url": "https://l.devpost.com/", "displayed_location": {"location": "Online"},
             "time_left_to_submission": "1 day left"}]}
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            items = nb.fetch_devpost({"name": "Devpost", "kind": "opportunity"})
        self.assertEqual([i.title for i in items], ["Zero Prize"])
        self.assertIn("сен 15 - окт 30, 2026", items[0].summary)
        self.assertIn("Осталось: около 1 мес.", items[0].summary)
        self.assertNotIn("Призы", items[0].summary)

    def test_ctftime_no_rating_text(self):
        t = nb.now()
        payload = [{"title": "NewCTF", "ctftime_url": "https://ctftime.org/event/2", "start": (t + timedelta(days=1)).isoformat(),
                    "finish": (t + timedelta(days=2)).isoformat(), "format": "Jeopardy", "onsite": False,
                    "restrictions": "Open", "weight": 0.0}]
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            items = nb.fetch_ctftime({"name": "CTFtime", "kind": "opportunity", "preview": False})
        self.assertIn("пока нет", items[0].summary)
        self.assertIn("открытое", items[0].summary)
        self.assertFalse(items[0].preview)


class Photos(unittest.TestCase):
    def test_rss_image_sources(self):
        import feedparser
        xml = """<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>x</title>
        <item><title>A</title><link>https://a.ru/1</link><media:content url="https://img.ru/a.jpg" medium="image"/></item>
        <item><title>B</title><link>https://a.ru/2</link><enclosure url="https://img.ru/b.png" type="image/png" length="1"/></item>
        <item><title>C</title><link>https://a.ru/3</link><description><![CDATA[<img src="https://t.ru/px.gif" width="1"><img src="//img.ru/c.webp"> текст]]></description></item>
        <item><title>D</title><link>https://a.ru/4</link><description>без картинки</description></item>
        </channel></rss>"""
        es = feedparser.parse(xml).entries
        self.assertEqual(nb.clean_img(nb.rss_image(es[0])), "https://img.ru/a.jpg")
        self.assertEqual(nb.clean_img(nb.rss_image(es[1])), "https://img.ru/b.png")
        self.assertEqual(nb.clean_img(nb.rss_image(es[2])), "https://img.ru/c.webp")  # пиксель пропущен
        self.assertIsNone(nb.rss_image(es[3]))

    def test_clean_img(self):
        self.assertIsNone(nb.clean_img("https://x.ru/logo.svg"))
        self.assertIsNone(nb.clean_img("/relative.png"))
        self.assertEqual(nb.clean_img("//x.ru/a.jpg"), "https://x.ru/a.jpg")
        self.assertEqual(nb.clean_img("https://ctftime.org//media/events/a_1.jpg"),
                         "https://ctftime.org/media/events/a_1.jpg")
        self.assertEqual(nb.clean_img("https://x.ru/a//b.jpg?u=http://y.ru/z"), "https://x.ru/a/b.jpg?u=http://y.ru/z")

    def test_telegram_photo(self):
        html_ = ('<div class="tgme_widget_message"><a class="tgme_widget_message_photo_wrap" '
                 "style=\"width:100px;background-image:url('https://cdn.tg/p.jpg')\"></a>"
                 '<div class="tgme_widget_message_text">Пост с фото и текстом</div>'
                 '<a class="tgme_widget_message_date" href="https://t.me/c/1">'
                 '<time datetime="2026-09-30T10:00:00+00:00"></time></a></div>').encode()
        items = nb.parse_telegram(html_, {"name": "CC", "tag": "x"})
        self.assertEqual(items[0].image, "https://cdn.tg/p.jpg")
        off = nb.parse_telegram(html_, {"name": "CC", "tag": "x", "images": False})
        self.assertIsNone(off[0].image)

    def test_send_photo_ok(self):
        urls = []

        def post(url, json, timeout):
            urls.append(url.rsplit("/", 1)[1])
            return FakeResp(200)
        with mock.patch.object(nb.requests, "post", side_effect=post):
            nb.send("tok", "@c", "<b>hi</b>", True, "https://img.ru/a.jpg")
        self.assertEqual(urls, ["sendPhoto"])

    def test_send_photo_falls_back_to_text(self):
        urls = []

        def post(url, json, timeout):
            urls.append(url.rsplit("/", 1)[1])
            return FakeResp(400, {"description": "failed to get HTTP URL content"}) if "Photo" in url else FakeResp(200)
        with mock.patch.object(nb.requests, "post", side_effect=post):
            nb.send("tok", "@c", "hi", True, "https://img.ru/a.jpg")
        self.assertEqual(urls, ["sendPhoto", "sendMessage"])

    def test_send_photo_rate_limit_not_swallowed(self):
        with mock.patch.object(nb.requests, "post", return_value=FakeResp(500, {"description": "oops"})):
            with self.assertRaises(nb.TelegramError):
                nb.send("tok", "@c", "hi", True, "https://img.ru/a.jpg")

    def test_caption_fits_1024(self):
        it = nb.Item("Src", "x", "Заголовок " * 10, "https://a.ru/1", "Очень длинное резюме. " * 60, nb.now())
        text = nb.render(it, {"max_summary_chars": 350}, with_image=True)
        self.assertLessEqual(len(text), 1000)
        self.assertIn("Src", text)  # подпись с источником не потерялась

    def test_devpost_thumbnail(self):
        payload = {"hackathons": [{"title": "AI Hack", "url": "https://ai.devpost.com/",
                                   "displayed_location": {"location": "Online"},
                                   "thumbnail_url": "//d1.cloudfront.net/a.png"}]}
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            items = nb.fetch_devpost({"name": "Devpost", "kind": "opportunity"})
        self.assertEqual(items[0].image, "https://d1.cloudfront.net/a.png")


class Round3(unittest.TestCase):
    def test_ctftime_logo_single_slash_and_online_only(self):
        t = nb.now()
        base = {"ctftime_url": "https://ctftime.org/event/%d", "start": (t + timedelta(days=1)).isoformat(),
                "finish": (t + timedelta(days=2)).isoformat(), "format": "Jeopardy", "weight": 1.0}
        payload = [dict(base, title="OnlineCTF", ctftime_url="https://ctftime.org/event/1", onsite=False,
                        restrictions="Open", logo="//media/events/a.png"),
                   dict(base, title="LogoCTF", ctftime_url="https://ctftime.org/event/3", onsite=False,
                        restrictions="Academic", logo="/media/events/b.png"),
                   dict(base, title="OnsiteCTF", ctftime_url="https://ctftime.org/event/2", onsite=True,
                        location="Barnaul", restrictions="Open")]
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            items = nb.fetch_ctftime({"name": "CTFtime", "kind": "opportunity", "online_only": True})
        self.assertEqual([i.title for i in items], ["OnlineCTF", "LogoCTF"])
        self.assertEqual(items[0].image, "https://ctftime.org/media/events/a.png")
        self.assertEqual(items[1].image, "https://ctftime.org/media/events/b.png")
        self.assertIn("студенческие", items[1].summary)
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            self.assertEqual(len(nb.fetch_ctftime({"name": "CTFtime", "kind": "opportunity"})), 3)

    def test_devpost_pages_and_dedupe(self):
        pages = {1: [{"title": "Ending", "url": "https://e.devpost.com/", "displayed_location": {"location": "Online"},
                      "time_left_to_submission": "about 2 hours left"}],
                 2: [{"title": "Good Hack", "url": "https://g.devpost.com/", "displayed_location": {"location": "Online"},
                      "time_left_to_submission": "about 1 month left"}],
                 3: [{"title": "Good Hack", "url": "https://g.devpost.com/", "displayed_location": {"location": "Online"}}]}

        def get(url, **k):
            return json.dumps({"hackathons": pages[int(url.rsplit("=", 1)[1])]}).encode()
        with mock.patch.object(nb, "http_get", get):
            items = nb.fetch_devpost({"name": "Devpost", "kind": "opportunity"})
        self.assertEqual([i.title for i in items], ["Good Hack"])  # страница 1 отфильтрована, дубль убран

    def test_telegram_promo_signature_removed(self):
        html_ = ('<div class="tgme_widget_message"><div class="tgme_widget_message_text">'
                 'Satisfactory получит платное дополнение. Дату релиза не называют. @prepodsteam</div>'
                 '<a class="tgme_widget_message_date" href="https://t.me/c/1">'
                 '<time datetime="2026-09-30T10:00:00+00:00"></time></a></div>').encode()
        it = nb.parse_telegram(html_, {"name": "PS", "tag": "игры"})[0]
        self.assertNotIn("@prepodsteam", it.summary)
        self.assertNotIn("@prepodsteam", nb.format_post(it))

    def test_real_topics_from_config(self):
        import yaml
        topics = yaml.safe_load(open(nb.CONFIG_PATH, encoding="utf-8"))["topics"]
        mk = lambda title, summ="": nb.Item("s", "технологии", title, "https://a.ru/1", summ, nb.now())  # noqa: E731
        self.assertNotIn("разработка", nb.topic_tags(
            mk("Powercom анонсирует ИБП", "международный разработчик и производитель источников питания"), topics))
        self.assertIn("железо", nb.topic_tags(mk("Powercom анонсирует ИБП Raptor"), topics))
        self.assertNotIn("железо", nb.topic_tags(mk("Artificial intelligence breakthrough"), topics))  # не «intel»
        self.assertEqual(nb.topic_tags(mk("Anthropic собираются на IPO"), topics), ["ИИ"])


class HumanCards(unittest.TestCase):
    def test_dates_and_delta(self):
        from datetime import datetime, timezone, date
        a = datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc)
        b = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
        nb.TZ_HOURS, nb.TZ_LABEL = 5, "по Ташкенту"
        self.assertEqual(nb.fmt_range(a, b), "сб, 3 октября 2026, 19:00 — вс, 4 октября, 03:00 (по Ташкенту)")
        self.assertEqual(nb.fmt_range(a, a + timedelta(hours=3)), "сб, 3 октября 2026, 19:00 — 22:00 (по Ташкенту)")
        self.assertEqual(nb.human_delta(timedelta(days=3, hours=5)), "3 дн. 5 ч.")
        self.assertEqual(nb.human_delta(timedelta(minutes=90)), "1 ч. 30 мин.")
        self.assertEqual(nb.human_delta(timedelta(minutes=5)), "5 мин.")
        self.assertEqual(nb.human_delta(timedelta(seconds=-5)), "уже идёт")
        self.assertEqual(nb.parse_end_date("Oct 01 - 29, 2026"), date(2026, 10, 29))
        self.assertEqual(nb.parse_end_date("Sep 29 - Oct 04, 2026"), date(2026, 10, 4))
        self.assertEqual(nb.parse_end_date("Dec 20, 2026 - Jan 15, 2027"), date(2027, 1, 15))
        self.assertIsNone(nb.parse_end_date("скоро"))

    def test_ctf_card(self):
        t = nb.now()
        payload = [{"title": "CubeCTF 2026", "ctftime_url": "https://ctftime.org/event/9", "format": "Attack-Defense",
                    "start": (t + timedelta(days=3, hours=5)).isoformat(), "finish": (t + timedelta(days=3, hours=13)).isoformat(),
                    "onsite": False, "restrictions": "Open", "weight": 24.71,
                    "organizers": [{"id": 1, "name": "CubeCtf Team"}]}]
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            it = nb.fetch_ctftime({"name": "CTFtime", "tag": "ctf", "kind": "opportunity"})[0]
        text = nb.format_post(it)
        for must in ("🎯 CubeCTF 2026", "атаковать", "📅 Когда:", "⏳ До старта: 3 дн.", "📝 Регистрация:",
                     "📍 Формат: Attack-Defense · онлайн", "🔓 Участие: открытое, можно без отбора",
                     "⭐ Рейтинг: 24.71", "🏢 Организаторы: CubeCtf Team", "📎 Что нужно:", "👉 Что делать:", "#ctf"):
            self.assertIn(must, text)
        self.assertLessEqual(len(nb.render(it, {"max_summary_chars": 350}, with_image=True)), 1000)

    def test_codeforces_card(self):
        t = nb.now()
        mk = lambda name: {"status": "OK", "result": [{"id": 5, "name": name, "type": "CF", "phase": "BEFORE",  # noqa: E731
                                                       "durationSeconds": 9000,
                                                       "startTimeSeconds": int((t + timedelta(days=2)).timestamp())}]}
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(mk("Codeforces Round (Div. 1)")).encode()):
            it = nb.fetch_codeforces({"name": "Codeforces", "kind": "opportunity", "tag": "олимпиады"})[0]
        text = nb.format_post(it)
        self.assertIn("от 1900", text)
        self.assertIn("2 ч 30 мин", text)
        self.assertIn("📝 Регистрация: на странице раунда", text)
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(mk("R (Codeforces Round, Div. 1 + Div. 2)")).encode()):
            it2 = nb.fetch_codeforces({"name": "Codeforces", "kind": "opportunity"})[0]
        self.assertIn("Открыт для всех", it2.details["about"])

    def test_devpost_card(self):
        payload = {"hackathons": [{"title": "AI Hack", "url": "https://ai.devpost.com/",
                                   "displayed_location": {"location": "Online"},
                                   "submission_period_dates": "Oct 01 - 29, 2026", "time_left_to_submission": "about 1 month left",
                                   "prize_amount": "$<span>25,000</span>", "themes": [{"name": "AI"}, {"name": "Web"}]}]}
        with mock.patch.object(nb, "http_get", lambda url, **k: json.dumps(payload).encode()):
            it = nb.fetch_devpost({"name": "Devpost", "kind": "opportunity", "tag": "хакатоны"})[0]
        text = nb.format_post(it)
        self.assertIn("📝 Регистрация: сдать проект до 29 октября 2026", text)
        self.assertIn("🏆 Призы: $ 25,000", text)
        self.assertIn("Темы: AI, Web", text)
        self.assertIn("⏳ Осталось: около 1 мес.", text)

    def test_grant_card(self):
        t = nb.now()
        xml = rss([("Новая стипендия 2099", "https://g.org/new", "Deadline: December 31, 2099 Apply now", t)])
        with mock.patch.object(nb, "http_get", lambda url, **k: xml):
            it = nb.fetch_rss({"name": "G", "tag": "гранты", "kind": "opportunity", "url": "x"})[0]
        text = nb.format_post(it)
        self.assertIn("📝 Регистрация: подать заявку до 31 декабря 2099", text)
        self.assertIn("⏳ Осталось:", text)
        self.assertIn("Apply now", text)
        self.assertIn("👉 Что делать:", text)

    def test_empty_fields_are_skipped(self):
        it = nb.Item("S", "ctf", "T", "https://a.ru/1", "", nb.now(), kind="opportunity",
                     details={"about": "Описание", "when": "", "org": "", "todo": "сделай"})
        text = nb.format_post(it)
        self.assertNotIn("📅", text)
        self.assertNotIn("🏢", text)
        self.assertIn("👉 Что делать: сделай", text)

    def test_telegram_opportunity_keeps_old_layout(self):
        it = nb.Item("EduGrants", "гранты", "Результаты конкурса", "https://t.me/c/1", "Список победителей", nb.now(),
                     kind="opportunity")
        text = nb.format_post(it)
        self.assertIn("🎯 Результаты конкурса", text)
        self.assertIn("Список победителей", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
