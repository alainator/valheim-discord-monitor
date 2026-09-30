import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stat_channels as sc  # noqa: E402
import stats_db  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=-7))
NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=TZ)        # a Wednesday
T = NOW.timestamp()


class NamesTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def name(self, key, snap=None, **kw):
        return sc.name_for(key, snap or {}, self.c, now=NOW, **kw)

    def test_live_state(self):
        up = {"known": True, "count": 3, "version": "l-1.0.16", "up_since": T - 3 * 86400 - 60,
              "join_code": "482913", "last_backup": T - 3600, "last_raid": ("The Elder's army (greydwarves)",
                                                                           T - 2 * 86400)}
        self.assertEqual(self.name("players", up), "🟢 Valheim: 3 online")
        self.assertEqual(self.name("players", {**up, "count": 0}), "🟢 Valheim: empty")
        self.assertEqual(self.name("server", up), "🟢 Server online · l-1.0.16")
        self.assertEqual(self.name("server", up, update_waiting=True), "⬆️ Update waiting · l-1.0.16")
        self.assertEqual(self.name("join_code", up), "🔑 Join code: 482913")
        self.assertEqual(self.name("join_code", {**up, "join_code": None}), "🔑 Join code: after next join")
        self.assertEqual(self.name("uptime", up), "⏱ Up 3 d")
        self.assertEqual(self.name("uptime", {**up, "up_since": T - 5 * 3600}), "⏱ Up 5 h")
        self.assertEqual(self.name("backup", up), "💾 Backup: today 11:00")
        self.assertEqual(self.name("backup", {}), "💾 Backup: none yet")
        self.assertEqual(self.name("last_raid", up), "⚔️ Last raid: The Elder's army (Mon)")
        down = {"down": True}
        for key in ("players", "server", "join_code", "uptime"):
            self.assertIn("ffline" if key != "uptime" else "Down", self.name(key, down))
        # Nothing known yet (just started): keep the channel's current name.
        self.assertIsNone(self.name("players", {}))
        self.assertIsNone(self.name("uptime", {}))
        self.assertIsNone(self.name("last_raid", {}))

    def test_database_stats(self):
        monday = sc.week_start(NOW).timestamp()
        self.st.login("Ingrid", int(monday - 3600))       # last week: not counted
        self.st.logout("Ingrid", int(monday))
        self.st.login("Ingrid", int(T - 7200))
        self.st.logout("Ingrid", int(T - 3600))
        self.st.login("Bjorn", int(T - 5400))
        self.st.logout("Bjorn", int(T - 1800))
        self.st.death("Bjorn", int(T - 2000))
        self.st.death("Bjorn", int(monday - 10))          # last week
        self.st.concurrency(2, int(T - 3000))
        self.st.concurrency(5, int(T - 86400 * 2))        # another day
        self.assertEqual(self.name("hours_week"), "⏳ This week: 2 h played")
        self.assertEqual(self.name("deaths_week"), "💀 Deaths this week: 1")
        self.assertEqual(self.name("peak_today"), "📈 Peak today: 2")
        self.assertEqual(self.name("peak_today", {"count": 3}), "📈 Peak today: 3")
        self.assertEqual(self.name("vikings"), "🧭 2 Vikings have visited")
        self.assertEqual(self.name("achievements"), "🏅 0 achievements unlocked")
        self.st.save_profile("7656", unlocked=12, total=40)
        self.assertEqual(self.name("achievements"), "🏅 12 achievements unlocked")
        # The log clock can differ from real time: offset = real - log.
        self.assertEqual(sc.name_for("deaths_week", {}, self.c, now=NOW, offset=-3 * 86400),
                         "💀 Deaths this week: 0")

    def test_plans_and_titles(self):
        self.assertEqual(self.name("next_plan"), "📅 No game night planned")
        self.c.execute("INSERT INTO plans(title, at) VALUES (?, ?)", ("Bonemass run", int(T + 3 * 86400 + 8 * 3600)))
        self.c.execute("INSERT INTO plans(title, at) VALUES (?, ?)", ("Old", int(T - 86400)))
        self.assertEqual(self.name("next_plan"), "📅 Bonemass run · Sat 20:00")
        self.assertEqual(self.name("titles", titles=[]), "👑 No titles yet")
        titles = [("Heimdall", "Ingrid"), ("Hel", None), ("Thor", "Bjorn")]
        seen = {sc.name_for("titles", {}, self.c, now=NOW + dt.timedelta(minutes=10 * i), titles=titles)
                for i in range(4)}
        self.assertEqual(seen, {"👑 Heimdall: Ingrid", "👑 Thor: Bjorn"})

    def test_when(self):
        self.assertEqual(sc.when(T - 86400, NOW), "yesterday 12:00")
        self.assertEqual(sc.when(T + 86400, NOW), "tomorrow 12:00")
        self.assertEqual(sc.when(T - 20 * 86400, NOW), "Sep 10")


class RenamerTest(unittest.TestCase):
    def test_paced_per_channel(self):
        now = [1000.0]
        r = sc.Renamer(clock=lambda: now[0])
        self.assertTrue(r.due("a", "x"))
        r.renamed("a", "x")
        self.assertFalse(r.due("a", "x"))                 # unchanged
        self.assertFalse(r.due("a", "y"))                 # too soon
        self.assertTrue(r.due("b", "y"))                  # other channels have their own limit
        self.assertFalse(r.due("a", None))
        now[0] += 300
        self.assertTrue(r.due("a", "y"))


if __name__ == "__main__":
    unittest.main()
