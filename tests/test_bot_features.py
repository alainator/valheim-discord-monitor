import asyncio
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import extras  # noqa: E402
import stats_db  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

NOW = dt.datetime(2026, 9, 30, 12, 0).astimezone()


class TimePollTest(unittest.TestCase):
    def test_several_times(self):
        got = community.parse_whens("sun 18:00, sat 20:00 or sat 20:00", NOW)
        self.assertEqual([t.strftime("%a %H:%M") for t in got], ["Sat 20:00", "Sun 18:00"])
        self.assertEqual(len(community.parse_whens("8pm", NOW)), 1)
        with self.assertRaises(ValueError):
            community.parse_whens("sat 20:00, whenever", NOW)

    def test_winner(self):
        self.assertEqual(community.poll_winner([(1, 100), (3, 200), (3, 150)]), 150)   # tie: earlier
        self.assertIsNone(community.poll_winner([(0, 100), (0, 200)]))


class ChartTest(unittest.TestCase):
    def test_hours_by_day_and_chart(self):
        with tempfile.TemporaryDirectory() as d:
            st = stats_db.Store(os.path.join(d, "s.db"))
            now = int(NOW.timestamp())
            st.login("Ingrid", now - 3600 * 3)
            st.logout("Ingrid", now - 3600)                   # 2 h today
            st.login("Bjorn", now - 86400 * 2)
            st.logout("Bjorn", now - 86400 * 2 + 1800)        # 0.5 h two days ago
            st.login("Old", now - 86400 * 20)
            st.logout("Old", now - 86400 * 20 + 3600)         # outside the week
            hours = extras.WeeklyRecap.hours_by_day(st.conn, now)
            st.close()
        self.assertEqual(len(hours), 7)
        self.assertEqual(hours[-1], (NOW.date(), 2.0))
        self.assertEqual(hours[-3][1], 0.5)
        self.assertAlmostEqual(sum(h for _, h in hours), 2.5)
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        png = extras.WeeklyRecap.chart(hours)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertIsNone(extras.WeeklyRecap.chart([(NOW.date(), 0.0)]))


class WebhookPostTest(unittest.TestCase):
    def post(self, **kw):
        sent = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"id": "555", "channel_id": "777"}).encode()

        def urlopen(req, timeout=None):
            sent.append(req)
            return Reply()
        d = vdm.Discord("https://discord.com/api/webhooks/1/abc")
        return d, sent, urlopen

    def test_reactions_get_the_message_id(self):
        d, sent, urlopen = self.post()
        seen = []
        d.on_posted = lambda kind, cid, mid: seen.append((kind, cid, mid))
        with mock.patch.object(vdm.urllib.request, "urlopen", urlopen):
            d.post_embed("titles", {"title": "t"}, {"titles"})
        self.assertTrue(sent[0].full_url.endswith("?wait=true"))
        self.assertEqual(seen, [("titles", "777", "555")])

    def test_file_is_sent_as_multipart(self):
        d, sent, urlopen = self.post()
        with mock.patch.object(vdm.urllib.request, "urlopen", urlopen):
            d.post_embed("weekly_recap", {"title": "t", "image": {"url": "attachment://activity.png"}},
                         {"weekly_recap"}, file=("activity.png", b"\x89PNGdata"))
        req = sent[0]
        self.assertIn("multipart/form-data", req.headers["Content-type"])
        self.assertIn(b'name="payload_json"', req.data)
        self.assertIn(b'filename="activity.png"', req.data)
        self.assertIn(b"\x89PNGdata", req.data)
        self.assertFalse(req.full_url.endswith("wait=true"))  # no reactions hooked up


@unittest.skipIf(discord is None, "discord.py not installed")
class BotFeatureTest(unittest.TestCase):
    def bot(self, d, **cfg):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                "save_dir": d, **cfg}, "Alheim")
        b.attach(db_path=os.path.join(d, "s.db"))
        return b

    def fake_channel(self):
        class Msg:
            def __init__(self, mid, **kw):
                self.id, self.kw, self.pinned, self.threads = mid, kw, False, []
                self.jump_url = f"https://discord.com/channels/9/1/{mid}"

            async def create_thread(self, name=None, auto_archive_duration=None):
                thread = Channel(self.id)
                self.threads.append((name, thread))
                return thread

            async def pin(self, reason=None):
                self.pinned = True

        class Channel:
            def __init__(self, cid=1):
                self.id, self.sent = cid, []

            async def send(self, content=None, embed=None, view=None, poll=None, allowed_mentions=None):
                msg = Msg(1000 + len(self.sent), content=content, embed=embed, poll=poll)
                self.sent.append(msg)
                return msg
        return Channel()

    def test_plan_opens_a_thread(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            ch = self.fake_channel()
            msg = asyncio.run(b._post_plan(ch, "Bonemass run", NOW + dt.timedelta(days=1), 42))
            (name, thread), = msg.threads
            self.assertEqual(name, "🗺️ Bonemass run")
            self.assertEqual(thread.id, msg.id)                 # threads share the message's id
            self.assertIn("Plan **Bonemass run** here", thread.sent[0].kw["content"])
            p = community.get_plan(b.db, 1)
            self.assertEqual(p["message_id"], str(msg.id))
            self.assertEqual(p["rsvps"]["going"], ["42"])

    def test_time_poll_becomes_a_signup(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            ch = self.fake_channel()
            now = dt.datetime.now().astimezone()
            times = [now + dt.timedelta(days=2, minutes=5), now + dt.timedelta(days=3)]
            asyncio.run(b._post_time_poll(ch, "Bonemass run", times, 42))
            poll = ch.sent[0].kw["poll"]
            self.assertEqual(len(poll.answers), 2)
            self.assertEqual(poll.duration, dt.timedelta(hours=47))   # closes an hour before the first
            pending = b._pending_polls()
            self.assertEqual(pending[0]["title"], "Bonemass run")

            # The poll has closed: the second time got more votes.
            answers = [type("A", (), {"vote_count": 1})(), type("A", (), {"vote_count": 3})()]
            closed = type("M", (), {"poll": type("P", (), {"answers": answers})()})()

            async def fetch_message(mid):
                return closed
            ch.fetch_message = fetch_message
            b.client = type("C", (), {"get_channel": lambda self, cid: ch})()
            planned = []

            async def post_plan(channel, title, at, creator, guild=None):
                planned.append((title, int(at.timestamp()), creator))
            b._post_plan = post_plan
            with mock.patch("admin_bot.time.time", return_value=pending[0]["ends_at"] + 120):
                asyncio.run(b._resolve_polls())
            self.assertEqual(planned, [("Bonemass run", int(times[1].timestamp()), 42)])
            self.assertEqual(b._pending_polls(), [])

    def test_handled_notices_are_tidied(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d, tidy_notices_hours=24)
            deleted = []

            class Partial:
                def __init__(self, cid):
                    self.cid = cid

                def get_partial_message(self, mid):
                    cid = self.cid

                    class M:
                        async def delete(self):
                            deleted.append((cid, mid))
                    return M()
            b.client = type("C", (), {"get_partial_messageable": lambda self, cid: Partial(cid)})()
            with mock.patch("admin_bot.time.time", return_value=1000):
                b._queue_tidy(1, 50)
            with mock.patch("admin_bot.time.time", return_value=1000 + 3600):
                asyncio.run(b._tidy_notices())
            self.assertEqual(deleted, [])                          # not a day old yet
            with mock.patch("admin_bot.time.time", return_value=1000 + 86400 + 1):
                asyncio.run(b._tidy_notices())
            self.assertEqual(deleted, [(1, 50)])
            self.assertEqual(json.loads(b._meta("tidy:queue")), [])

    def test_channel_guide_is_pinned(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            ch = self.fake_channel()
            b._meta("layout:ch:welcome", 1)
            import server_layout
            p = server_layout.plan({"categories": [], "channels": []})
            guild = type("G", (), {"get_channel": lambda self, cid: ch})()
            asyncio.run(b._layout_guide(guild, p))
            self.assertTrue(ch.sent[0].pinned)
            self.assertEqual(ch.sent[0].kw["embed"].title, "📖 A guide to the realm")

    def test_reaction_emoji(self):
        import admin_bot
        self.assertEqual(admin_bot.REACTIONS["raid"], "⚔️")
        self.assertNotIn("login", admin_bot.REACTIONS)                # not on every login


if __name__ == "__main__":
    unittest.main()
