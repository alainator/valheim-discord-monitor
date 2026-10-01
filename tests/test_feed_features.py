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
import stat_channels  # noqa: E402
import stats_db  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402
from valheim_discord_monitor import Discord, Event  # noqa: E402


def local(y, m, d, h=20, mi=0):
    return dt.datetime(y, m, d, h, mi).astimezone()


class DB(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def session(self, player, when, hours=1.0):
        t = int(when.timestamp())
        self.st.login(player, t)
        self.st.logout(player, t + int(hours * 3600))


class StreakTest(DB):
    def test_streak_and_once_a_day(self):
        for day in (26, 27, 28, 29):
            self.session("Ingrid", local(2026, 9, day))
        self.st.login("Ingrid", int(local(2026, 9, 30).timestamp()))
        now = local(2026, 9, 30, 21)
        self.assertEqual(community.streak(self.c, "Ingrid", now.date()), 5)
        self.assertEqual(community.login_milestone(self.c, "ingrid", now), "is on a **5-day streak** 🔥")
        self.assertIsNone(community.login_milestone(self.c, "Ingrid", now))   # second login today

    def test_no_milestone_between_marks(self):
        for day in (29, 30):
            self.session("Bjorn", local(2026, 9, day))
        self.assertIsNone(community.login_milestone(self.c, "Bjorn", local(2026, 9, 30, 22)))

    def test_anniversary(self):
        self.assertEqual(community.anniversary(dt.date(2025, 9, 30), dt.date(2026, 9, 30)), "1 year")
        self.assertEqual(community.anniversary(dt.date(2024, 9, 30), dt.date(2026, 9, 30)), "2 years")
        self.assertEqual(community.anniversary(dt.date(2026, 6, 22), dt.date(2026, 9, 30)), "100 days")
        self.assertIsNone(community.anniversary(dt.date(2026, 9, 29), dt.date(2026, 9, 30)))
        self.session("Sigrid", local(2025, 9, 30))
        self.st.login("Sigrid", int(local(2026, 9, 30).timestamp()))
        self.assertEqual(community.login_milestone(self.c, "Sigrid", local(2026, 9, 30, 21)),
                         "first set sail here **1 year ago** today 🎂")


class CompareAndUptimeTest(DB):
    def test_compare(self):
        self.session("Ingrid", local(2026, 9, 28), hours=3)
        self.session("Bjorn", local(2026, 9, 28), hours=1)
        self.session("Bjorn", local(2026, 9, 29), hours=1)
        self.st.death("Bjorn", int(local(2026, 9, 29).timestamp()))
        e = community.render_compare(community.player_stats(self.c, "Ingrid"), community.player_stats(self.c, "Bjorn"))
        self.assertEqual(e["title"], "⚔️ Ingrid vs Bjorn")
        lines = e["description"].split("\n")
        self.assertIn("⬅️", lines[0])                   # Ingrid played longer
        self.assertIn("➡️", lines[1])                   # Bjorn visited more
        self.assertTrue(lines[3].endswith("1 ➡️"))       # and died more

    def test_uptime(self):
        for kind, ts in (("down", 1000), ("up", 1100), ("down", 5000), ("up", 5300)):
            self.st.server_event(kind, None, ts)
        u = community.uptime(self.c, 0, 10000)
        self.assertEqual((u["restarts"], u["down_seconds"]), (2, 400))
        self.assertAlmostEqual(u["fraction"], 0.96)
        # Down when the window starts: counted from the start.
        self.assertEqual(community.uptime(self.c, 1050, 2000)["down_seconds"], 50)
        # Down now: counted until the end.
        self.st.server_event("down", None, 9000)
        self.assertEqual(community.uptime(self.c, 8000, 10000)["down_seconds"], 1000)

    def test_restarts_are_recorded(self):
        for line in ("09/30/2026 04:00:00: OnApplicationQuit",
                     "09/30/2026 04:01:00: Game server connected"):
            for ev in vdm.ValheimLogParser().feed(line):
                vdm.record_event(self.st, ev)
        kinds = [r[0] for r in self.c.execute("SELECT kind FROM server_events ORDER BY at")]
        self.assertEqual(kinds, ["down", "up"])

    def test_a_crash_counts_as_downtime(self):
        """No shutdown line: the server was down from the last thing it logged to the boot."""
        self.st.uptime_event("up", 1000)
        self.st.heartbeat(5000)                        # its last "Connections" line
        self.st.uptime_event("up", 9000, unclean=True)  # boots again without a shutdown line
        u = community.uptime(self.c, 0, 10000)
        self.assertEqual((u["restarts"], u["down_seconds"]), (1, 4000))
        # A clean shutdown, then the boot: one outage, not two.
        self.st.uptime_event("down", 9500)
        self.st.uptime_event("down", 9600, unclean=True)
        self.st.uptime_event("up", 9800)
        self.assertEqual(community.uptime(self.c, 0, 10000)["restarts"], 2)

    def test_stat_channel(self):
        now = local(2026, 9, 30, 12)
        start = int(stat_channels.week_start(now).timestamp())
        self.st.server_event("down", None, start + 3600)
        self.st.server_event("up", None, start + 3600 + 432)
        name = stat_channels.name_for("uptime_week", {}, self.c, now)
        self.assertTrue(name.startswith("📶 Up 99."), name)
        self.assertTrue(name.endswith("· 1 restart"), name)


class QuietAndDigestTest(unittest.TestCase):
    def feed(self, **kw):
        clock = [0.0]
        d = Discord("https://x", clock=lambda: clock[0], **kw)
        sent = []
        d.send = lambda payload, kind=None, file=None: sent.append(payload)
        return d, sent, clock

    def test_quiet_hours_hold_joins_until_morning(self):
        d, sent, _ = self.feed(quiet_hours={"from": "23:00", "to": "08:00"})
        night, morning = local(2026, 9, 30, 2), local(2026, 9, 30, 9)
        self.assertTrue(d.is_quiet(night))
        self.assertFalse(d.is_quiet(morning))
        with mock.patch.object(d, "is_quiet", return_value=True):
            d.post(Event("login", "Ingrid", {}), "S", {"login", "death"})
            d.post(Event("death", "Ingrid", {}), "S", {"login", "death"})   # deaths still post
            d.flush(night)
        self.assertEqual(len(sent), 1)
        d.flush(morning)
        self.assertEqual(sent[1]["embeds"][0]["title"], "🌙 While it was quiet")
        self.assertIn("**Ingrid** has arrived", sent[1]["embeds"][0]["description"])
        d.flush(morning)
        self.assertEqual(len(sent), 2)

    def test_same_day_window(self):
        d, _, _ = self.feed(quiet_hours={"from": "01:00", "to": "06:00"})
        self.assertTrue(d.is_quiet(local(2026, 9, 30, 3)))
        self.assertFalse(d.is_quiet(local(2026, 9, 30, 7)))

    def test_digest_groups_joins(self):
        d, sent, clock = self.feed(digest_seconds=60)
        d.post(Event("login", "Ingrid", {}), "S", {"login"})
        clock[0] = 30
        d.post(Event("login", "Bjorn", {}), "S", {"login"})
        d.flush()
        self.assertEqual(sent, [])
        clock[0] = 61
        d.flush()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["embeds"][0]["description"].count("has arrived"), 2)

    def test_send_says_whether_it_went_out(self):
        self.assertFalse(Discord("").post_embed("announcement", {"description": "x"}, {"announcement"}))

    def test_announcement_can_ping_everyone(self):
        d, sent, _ = self.feed()
        d.post_embed("announcement", {"description": "Boss night!"}, {"announcement"}, content="@everyone",
                     allowed_mentions={"parse": ["everyone"]})
        self.assertEqual(sent[0]["content"], "@everyone")
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": ["everyone"]})


class CrowdTest(DB):
    def test_who_to_nudge(self):
        community.set_notify_crowd(self.c, 7, 3)
        community.set_notify_crowd(self.c, 8, 2)
        self.assertEqual(community.my_notifications(self.c, 7)["crowd"], 3)
        self.assertEqual(community.who_to_nudge(self.c, 2, 3), ["7"])
        self.assertEqual(sorted(community.who_to_nudge(self.c, 1, 3)), ["7", "8"])
        self.assertEqual(community.who_to_nudge(self.c, 3, 4), [])            # already past it
        # Not someone whose own character is online.
        self.st.login("Ingrid", 100)
        community.link_player(self.c, "Ingrid", 7)
        self.assertEqual(community.who_to_nudge(self.c, 2, 3, ["Ingrid"]), [])
        community.notify_off(self.c, 8)
        self.assertIsNone(community.my_notifications(self.c, 8)["crowd"])

    def test_bot_nudges_once(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": self.d}, "S")
        b.attach(db_path=os.path.join(self.d, "s.db"))
        community.set_notify_crowd(b.db, 7, 2)
        with mock.patch.object(b, "_dm", mock.AsyncMock(return_value=True)) as dm:
            asyncio.run(b._on_login("Bjorn", False, 1, 2, ["Ingrid", "Bjorn"]))
            asyncio.run(b._on_login("Sigrid", False, 1, 2, ["Ingrid", "Sigrid"]))   # within 3 h: no second DM
        self.assertEqual(dm.call_count, 1)
        self.assertIn("**2** Vikings are on **S**", dm.call_args[0][1])


class WelcomeDMTest(unittest.TestCase):
    def test_welcome_dm_config(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            base = {"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}
            off = admin_bot.AdminBot(base, "S")
            self.assertIsNone(off.welcome_dm)
            self.assertFalse(off._build_client().intents.members)     # no privileged intent unless asked
            on = admin_bot.AdminBot({**base, "welcome_dm": True}, "Alheim")
            self.assertTrue(on._build_client().intents.members)
            self.assertIn("Welcome to **Vikings**, Ingrid", on.welcome_text("Ingrid", "Vikings"))
            self.assertIn("**Alheim**", on.welcome_text("Ingrid", "Vikings"))
            custom = admin_bot.AdminBot({**base, "welcome_dm": "Hi {member}, see #rules"}, "S")
            self.assertEqual(custom.welcome_text("Ingrid", "G"), "Hi Ingrid, see #rules")


if __name__ == "__main__":
    unittest.main()
