import asyncio
import datetime as dt
import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import extras  # noqa: E402
import stats_db  # noqa: E402
from valheim_discord_monitor import Discord, Event  # noqa: E402


class DB(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()


class LinksAndNotifyTest(DB):
    def test_link_rules(self):
        self.st.login("Ingrid", 100)
        self.assertEqual(community.known_player(self.c, "ingrid"), "Ingrid")
        self.assertIsNone(community.link_player(self.c, "Ingrid", 111))
        self.assertIn("already linked", community.link_player(self.c, "ingrid", 222))
        self.assertIsNone(community.link_player(self.c, "Ingrid", 222, force=True))
        self.assertEqual(community.linked_user(self.c, "INGRID"), "222")
        self.assertEqual(community.linked_players(self.c, 222), ["Ingrid"])
        self.assertTrue(community.unlink_player(self.c, "Ingrid"))

    def test_who_to_notify(self):
        community.set_notify_first(self.c, 1, True)
        community.set_follow(self.c, 2, "Ingrid", True)
        community.set_follow(self.c, 3, "Ingrid", True)
        community.link_player(self.c, "Ingrid", 3)          # Ingrid's own player isn't told
        self.assertEqual(community.who_to_notify(self.c, "Ingrid", True), {"1": "first", "2": "follow"})
        self.assertEqual(community.who_to_notify(self.c, "Ingrid", False), {"2": "follow"})
        community.notify_off(self.c, 2)
        self.assertEqual(community.my_notifications(self.c, 2), {"first": False, "follows": []})

    def test_access_requests_expire(self):
        community.request_access(self.c, "Newbie", 9)
        self.assertEqual(community.access_request(self.c, "newbie"), "9")
        self.assertIsNone(community.access_request(self.c, "Newbie", now=10 ** 12))


class StatsTest(DB):
    def test_stats_and_top(self):
        self.st.login("A", 0)
        self.st.death("A", 60)
        self.st.logout("A", 7200)
        self.st.login("B", 0)
        self.st.logout("B", 600)
        s = community.player_stats(self.c, "a")
        self.assertEqual((s["seconds"], s["deaths"], s["rank"], s["players"]), (7200, 1, 1, 2))
        e = community.render_stats(s, 3600, "42")
        self.assertIn({"name": "Rank", "value": "#1 of 2", "inline": True}, e["fields"])
        self.assertIn({"name": "Last seen", "value": "<t:10800:R>", "inline": True}, e["fields"])   # 7200 + offset
        self.assertEqual(e["description"], "Linked to <@42>")
        self.assertIsNone(community.player_stats(self.c, "Nobody"))
        t = community.render_top("time", community.top(self.c, "time"))
        self.assertIn("🥇 **A**: 2h", t["description"])
        self.assertIn("🥈 **B**: 10m", t["description"])


class PlansTest(DB):
    def test_plan_flow(self):
        pid = community.create_plan(self.c, "Bonemass", 10_000, 55, 1)
        community.rsvp(self.c, pid, 1, "going")
        community.rsvp(self.c, pid, 2, "maybe")
        community.rsvp(self.c, pid, 2, "going")             # changing your mind replaces
        p = community.get_plan(self.c, pid)
        self.assertEqual(p["rsvps"], {"going": ["1", "2"], "maybe": [], "no": []})
        self.assertIn("✅ Going (2)", [f["name"] for f in community.render_plan(p, "S")["fields"]])
        self.assertEqual(community.due_reminders(self.c, 10_000 - 3600, 900), [])
        self.assertEqual([x["id"] for x in community.due_reminders(self.c, 10_000 - 600, 900)], [pid])
        community.mark_reminded(self.c, pid)
        self.assertEqual(community.due_reminders(self.c, 10_000 - 600, 900), [])


class WhenTest(unittest.TestCase):
    now = dt.datetime(2026, 9, 29, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))   # a Tuesday

    def at(self, text):
        return community.parse_when(text, self.now)

    def test_formats(self):
        self.assertEqual(self.at("20:00"), self.now.replace(hour=20))
        self.assertEqual(self.at("8pm"), self.now.replace(hour=20))
        self.assertEqual(self.at("8:30 pm"), self.now.replace(hour=20, minute=30))
        self.assertEqual(self.at("9am"), self.now.replace(hour=9) + dt.timedelta(days=1))    # past: tomorrow
        self.assertEqual(self.at("tomorrow 8pm"), self.now.replace(hour=20) + dt.timedelta(days=1))
        self.assertEqual(self.at("sat 20:00"), self.now.replace(hour=20) + dt.timedelta(days=4))
        self.assertEqual(self.at("tue 5pm"), self.now.replace(hour=17) + dt.timedelta(days=7))  # passed today
        self.assertEqual(self.at("in 2h"), self.now + dt.timedelta(hours=2))
        self.assertEqual(self.at("in 1h 30m"), self.now + dt.timedelta(minutes=90))
        self.assertEqual(self.at("2026-10-03 20:00").hour, 20)
        for bad in ("whenever", "25:00", "13pm"):
            with self.assertRaises(ValueError):
                self.at(bad)


class SeedTest(unittest.TestCase):
    def fwl(self, path, name, seed):
        def s(x):
            b = x.encode()
            return bytes([len(b)]) + b
        pkg = struct.pack("<i", 34) + s(name) + s(seed) + struct.pack("<iq", 12345, 99)
        with open(path, "wb") as f:
            f.write(struct.pack("<i", len(pkg)) + pkg)

    def test_live_world_not_backup(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "worlds_local", "Alheim_backup_auto-1"))
        self.fwl(os.path.join(d, "worlds_local", "Alheim_backup_auto-1", "Alheim.fwl"), "Alheim", "OLDSEED")
        self.fwl(os.path.join(d, "worlds_local", "Alheim.fwl"), "Alheim", "Xy12AbCd")
        self.assertEqual(community.world_seed(d), ("Alheim", "Xy12AbCd"))
        self.assertIn("seed=Xy12AbCd", community.map_url("Xy12AbCd"))
        self.assertIsNone(community.world_seed(tempfile.mkdtemp()))


class HealthAndScheduleTest(unittest.TestCase):
    def test_health(self):
        clock = [0.0]
        h = extras.HealthWatch({"low_disk_gb": 10, "slow_save_seconds": 5}, clock=lambda: clock[0])
        gb = 1024 ** 3
        disk = lambda free: Event("disk_space", None, {"avail": free, "block": gb, "warn": 2 * gb})
        self.assertIsNone(h.observe(disk(50 * gb)))
        self.assertIn("getting full", h.observe(disk(8 * gb)))
        self.assertIsNone(h.observe(disk(8 * gb)))                  # once a day
        self.assertIn("stops saving", h.observe(disk(1.5 * gb)))
        self.assertIn("took **6.2 s**", h.observe(Event("world_saved", None, {"ms": 6200})))
        clock[0] += 86401
        self.assertIn("getting full", h.observe(disk(8 * gb)))

    def test_daily_restart(self):
        r = extras.DailyRestart({"time": "05:00", "window_minutes": 60})
        day = dt.datetime(2026, 9, 29, 4, 59)
        self.assertFalse(r.due(day, True))
        self.assertFalse(r.due(day.replace(hour=5, minute=10), False))       # someone's on
        self.assertTrue(r.due(day.replace(hour=5, minute=20), True))
        self.assertFalse(r.due(day.replace(hour=5, minute=30), True))       # once a day
        self.assertFalse(r.due(day.replace(hour=6, minute=1) + dt.timedelta(days=1), True))   # window over

    def test_exploration(self):
        text = extras.exploration(["SunkenCrypt4|-3,5", "SunkenCrypt1|2,2", "Grave1|2,2", "GoblinCamp2|9,9",
                                   "Vendor_BlackForest|1,1"])
        self.assertEqual(text, "🗺️ **4** new areas discovered: 2 sunken crypts, 1 fuling village, "
                               "1 Haldor the trader")
        self.assertIsNone(extras.exploration([]))


class MentionTest(unittest.TestCase):
    def test_linked_player_is_pinged_only_them(self):
        d = Discord("https://example.invalid/hook")
        with mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b""
            d.post(Event("welcome", "Ingrid", {"mention": "4242"}), "S", {"welcome"})
            sent = json.loads(op.call_args[0][0].data)
        self.assertEqual(sent["content"], "<@4242>")
        self.assertEqual(sent["allowed_mentions"], {"parse": [], "users": ["4242"]})


class BotFlowTest(DB):
    def bot(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": self.d}, "S")
        b.attach(db_path=os.path.join(self.d, "s.db"))
        b.client = mock.MagicMock()
        return b

    def test_login_dms_followers_once(self):
        b = self.bot()
        community.set_follow(b.db, 7, "Ingrid", True)
        with mock.patch.object(b, "_dm", mock.AsyncMock(return_value=True)) as dm:
            asyncio.run(b._on_login("Ingrid", False))
            asyncio.run(b._on_login("Ingrid", False))              # rejoin within 30 min: no second DM
        self.assertEqual(dm.call_count, 1)
        self.assertIn("**Ingrid** just joined", dm.call_args[0][1])

    def test_permit_links_the_requester_and_tells_them(self):
        b = self.bot()
        community.request_access(b.db, "Newbie", 77)
        with mock.patch.object(b, "_dm", mock.AsyncMock(return_value=True)) as dm:
            note = asyncio.run(b._welcome_requester("Newbie"))
        self.assertIn("linked to <@77>", note)
        self.assertEqual(community.linked_user(b.db, "Newbie"), "77")
        self.assertIsNone(community.access_request(b.db, "Newbie"))
        self.assertIn("let into **S** as **Newbie**", dm.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
