import datetime as dt
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
import stats_db  # noqa: E402
from valheim_discord_monitor import Event, ValheimLogParser  # noqa: E402


def feed(lines):
    p = ValheimLogParser()
    return [ev for ln in lines for ev in p.feed(ln)]


class ParserExtrasTest(unittest.TestCase):
    def test_server_lines(self):
        evs = feed([
            "09/28/2026 01:00:00: Valheim version: l-1.0.16 (network version 40)",
            "09/28/2026 01:00:01: [ Connected 7 portals ]",
            "09/28/2026 01:10:00: Random event set:army_moder",
            "09/28/2026 01:11:00: Network version check, their:40, mine:40",
            "09/28/2026 01:12:00: Network version check, their:41, mine:40",
            "09/28/2026 01:20:00: World save (5/5) done. Total time [108ms]",
            "09/28/2026 01:20:01: Backup created in Alheim_backup_auto-20260928012001 [12.3ms]",
            "09/28/2026 01:30:00: Random event set:army_somethingnew",
        ])
        self.assertEqual([e.kind for e in evs], ["server_version", "portals", "raid", "version_mismatch",
                                                 "world_saved", "backup_saved", "raid"])
        self.assertEqual(evs[0].extra["version"], "l-1.0.16")
        self.assertEqual(evs[2].extra["raid"], "Moder's army (drakes)")
        self.assertTrue(evs[3].extra["newer"])
        self.assertEqual(evs[6].extra["raid"], "Somethingnew")


class LiveStateTest(unittest.TestCase):
    def test_session_summary_and_board(self):
        live = extras.LiveState()
        live.observe(Event("server_online", None, {"ts": 0}), now=1000)
        live.observe(Event("login", "Ingrid", {"ts": 100, "count": 1}), now=1100)
        live.observe(Event("death", "Ingrid", {"ts": 200}), now=1200)
        live.observe(Event("death", "Ingrid", {"ts": 300}), now=1300)
        live.observe(Event("respawn", "Chet", {"ts": 310}), now=1310)   # online before we started
        snap = live.snapshot()
        self.assertEqual([n for n, _ in snap["online"]], ["Ingrid", "Chet"])
        board = extras.render_board(snap, "Alheim")
        self.assertEqual(board["title"], "🟢 Alheim: 2 online")
        self.assertIn("**Ingrid** · joined <t:1100:R>", board["description"])
        s = live.observe(Event("logout", "Ingrid", {"ts": 100 + 8040}), now=9000)
        self.assertEqual((s["duration"], s["deaths"], s["deaths_text"]), ("2h 14m", 2, " and died 2 times"))
        # No summary without a known login (Chet), nor for a shutdown flush.
        self.assertIsNone(live.observe(Event("logout", "Chet", {"ts": 9000}), now=9000))

    def test_board_states(self):
        live = extras.LiveState()
        self.assertTrue(extras.render_board(live.snapshot(), "S")["title"].startswith("⚪"))
        live.observe(Event("server_restart", None, {"ts": 1}), now=1)
        self.assertEqual(extras.render_board(live.snapshot(), "S")["title"], "🔴 S: offline")
        live.observe(Event("server_online", None, {"ts": 2}), now=2)
        live.observe(Event("raid", None, {"raid": "Trolls", "ts": 3}), now=3)
        b = extras.render_board(live.snapshot(), "S")
        self.assertEqual(b["title"], "🟢 S: empty")
        self.assertIn({"name": "Last raid", "value": "Trolls <t:3:R>", "inline": True}, b["fields"])


class MilestoneTest(unittest.TestCase):
    def test_marks(self):
        self.assertEqual(extras.hours_crossed(9.5 * 3600, 10.2 * 3600), 10)
        self.assertEqual(extras.hours_crossed(20 * 3600, 60 * 3600), 50)
        self.assertIsNone(extras.hours_crossed(11 * 3600, 12 * 3600))
        self.assertEqual(extras.deaths_reached(100), 100)
        self.assertIsNone(extras.deaths_reached(101))
        self.assertEqual(extras.fmt_duration(45), "45s")
        self.assertEqual(extras.fmt_duration(3600), "1h")


class RecapTest(unittest.TestCase):
    def test_build_and_due(self):
        with tempfile.TemporaryDirectory() as d:
            st = stats_db.Store(os.path.join(d, "s.db"))
            week = 7 * 86400
            now = 10 * week
            st.login("Old", now - 3 * week)
            st.logout("Old", now - 3 * week + 3600)
            st.login("Ingrid", now - 2 * 86400)
            st.death("Ingrid", now - 2 * 86400 + 60)
            st.logout("Ingrid", now - 2 * 86400 + 7200)
            st.login("Old", now - week - 1800)          # session straddling the window start
            st.logout("Old", now - week + 1800)
            st.server_event("raid", "Trolls", now - 86400)
            e = extras.WeeklyRecap.build(st.conn, now, "Alheim")
            self.assertIn("**2** vikings played **2h 30m**", e["description"])
            self.assertIn("🥇 **Ingrid**: 2h", e["fields"][0]["value"])
            self.assertIn("**Old**: 30m", e["fields"][0]["value"])
            self.assertIn({"name": "New vikings", "value": "Ingrid", "inline": False}, e["fields"])
            r = extras.WeeklyRecap({"day": "sunday", "hour": 18})
            sunday = dt.datetime(2026, 9, 27, 18, 5).astimezone()
            self.assertEqual(r.due(st, sunday), "2026-W39")
            st.set_meta("weekly_recap_week", "2026-W39")
            self.assertIsNone(r.due(st, sunday))
            self.assertIsNone(r.due(st, dt.datetime(2026, 9, 28, 18, 5).astimezone()))   # Monday
            self.assertIsNone(extras.WeeklyRecap.build(st.conn, now + 5 * week, "Alheim"))
            st.close()


class BackupTest(unittest.TestCase):
    def test_copy_prune_list_and_stale(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dest:
            def make(stem, age):
                for ext, size in (("db", 1000), ("fwl", 10)):
                    p = os.path.join(src, f"{stem}.{ext}")
                    with open(p, "wb") as f:
                        f.write(b"x" * size)
                    os.utime(p, (time.time() - age, time.time() - age))
            for i in range(4):
                make(f"Alheim_backup_auto-2026092{i}", age=(4 - i) * 3600)
            with open(os.path.join(src, "Alheim.db"), "w") as f:      # the live world: never copied
                f.write("live")
            b = extras.BackupCopier({"source_dir": src, "dest_dir": dest, "keep": 3, "alert_after_hours": 2})
            self.assertEqual(b.copy_new(), 3)                          # newest 3 pairs only
            self.assertEqual(b.copy_new(), 0)                          # nothing new
            names = sorted(os.listdir(dest))
            self.assertEqual(len(names), 6)                            # 3 newest pairs kept
            self.assertNotIn("Alheim_backup_auto-20260920.db", names)
            self.assertNotIn("Alheim.db", names)
            self.assertEqual([stem for _, _, stem in b.listing()][0], "Alheim_backup_auto-20260923")
            self.assertIsNone(b.stale_alert(True))                     # newest is 1 h old
            later = time.time() + 5 * 3600
            self.assertIn("hasn't made a world backup", b.stale_alert(True, now=later))
            self.assertIsNone(b.stale_alert(True, now=later))          # only once

    def test_valheim_1_0_backup_folders(self):
        # Valheim 1.0: the world and each backup are folders of chunk files.
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dest:
            def make(name, age, chunks=3):
                d = os.path.join(src, name)
                os.makedirs(os.path.join(d, "chunks"))
                for i in range(chunks):
                    with open(os.path.join(d, "chunks", f"c{i}.bin"), "wb") as f:
                        f.write(b"x" * 100)
                with open(os.path.join(d, "Alheim.fwl"), "wb") as f:
                    f.write(b"meta")
                t = time.time() - age
                os.utime(d, (t, t))
            make("Alheim", 0, chunks=5)                                  # the live world: never copied
            for i, stamp in enumerate(("20260927-190102", "20260928-050110",
                                       "20260928-150118", "20260928-170645")):
                make(f"Alheim_backup_auto-{stamp}", age=(4 - i) * 3600)
            b = extras.BackupCopier({"source_dir": src, "dest_dir": dest, "keep": 3})
            self.assertEqual(b.copy_new(), 3)
            self.assertEqual(sorted(os.listdir(dest)), ["Alheim_backup_auto-20260928-050110",
                                                         "Alheim_backup_auto-20260928-150118",
                                                         "Alheim_backup_auto-20260928-170645"])
            copied = os.path.join(dest, "Alheim_backup_auto-20260928-170645")
            self.assertEqual(sorted(os.listdir(os.path.join(copied, "chunks"))), ["c0.bin", "c1.bin", "c2.bin"])
            self.assertEqual(b.copy_new(), 0)                          # already there
            newest = b.listing()[0]
            self.assertEqual((newest[1], newest[2]), (304, "Alheim_backup_auto-20260928-170645"))
            make("Alheim_backup_auto-20260928-210000", age=0)          # a new one arrives
            self.assertEqual(b.copy_new(), 1)
            self.assertNotIn("Alheim_backup_auto-20260928-050110", os.listdir(dest))   # pruned to 3
            self.assertFalse([n for n in os.listdir(dest) if n.startswith(".")])      # no leftovers


if __name__ == "__main__":
    unittest.main()


class PrimeTest(unittest.TestCase):
    def test_board_fields_from_the_existing_log(self):
        from valheim_discord_monitor import LocalFileSource, prime_live_state
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            with open(path, "w") as f:
                f.write("09/28/2026 10:00:00: Valheim version: l-1.0.16 (network version 40)\n"
                        "09/28/2026 10:00:05: Game server connected\n"
                        "09/28/2026 10:00:06: [ Connected 7 portals ]\n"
                        "09/28/2026 11:00:00: Random event set:army_moder\n"
                        "09/28/2026 11:30:00: World save (5/5) done. Total time [108ms]\n"
                        "09/28/2026 11:30:01: Backup created in Alheim_backup_auto-1 [1ms]\n"
                        "09/28/2026 12:00:00:  Connections 0 ZDOS:1  sent:0 recv:0\n")
            log_noon = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.timezone.utc).timestamp()
            real_noon = log_noon + 7 * 3600 + 42           # server 7 h behind UTC, file touched 42 s later
            os.utime(path, (real_noon, real_noon))
            live = extras.LiveState()
            prime_live_state(live, LocalFileSource(path))
            off = 7 * 3600
            self.assertEqual((live.version, live.portals), ("l-1.0.16", 7))
            self.assertEqual(live.up_since, log_noon - 2 * 3600 + 5 + off)
            self.assertEqual(live.last_save, log_noon - 1800 + off)
            self.assertEqual(live.last_backup, log_noon - 1799 + off)
            self.assertEqual(live.last_raid, ("Moder's army (drakes)", log_noon - 3600 + off))
            # Known history, but not who's online now: the board must not claim "empty".
            self.assertFalse(live.snapshot()["known"])
            self.assertTrue(extras.render_board(live.snapshot(), "S")["title"].startswith("⚪"))

    def test_down_server_has_no_up_since(self):
        from valheim_discord_monitor import LocalFileSource, prime_live_state
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            with open(path, "w") as f:
                f.write("09/28/2026 10:00:05: Game server connected\n09/28/2026 11:00:00: OnApplicationQuit\n")
            live = extras.LiveState()
            prime_live_state(live, LocalFileSource(path))
            self.assertIsNone(live.up_since)
