import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maintenance  # noqa: E402
import stats_db  # noqa: E402
import steam  # noqa: E402
from valheim_discord_monitor import Discord, Event, LocalFileSource, OffsetTailer  # noqa: E402


class TmpDir(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.dir = self._d.name

    def tearDown(self):
        self._d.cleanup()

    def p(self, name):
        return os.path.join(self.dir, name)


class OffsetTailerTest(TmpDir):
    def test_log_replaced_by_a_longer_one_is_read_from_the_start(self):
        log = self.p("console.log")
        with open(log, "w") as f:
            f.write("09/28/2026 01:00:00: old session line\n")
        t = OffsetTailer(LocalFileSource(log), self.p("state.json"), start_at_end=True)
        # Server restarted while the monitor was down: new log already longer than the offset.
        new = ["09/28/2026 02:00:00: Game server connected\n"] + [f"09/28/2026 02:00:01: line {i}\n" for i in range(5)]
        with open(log, "w") as f:
            f.writelines(new)
        t = OffsetTailer(LocalFileSource(log), self.p("state.json"), start_at_end=True)
        self.assertEqual([ln + "\n" for ln in t.poll()], new)

    def test_appends_are_not_mistaken_for_a_new_file(self):
        log = self.p("console.log")
        with open(log, "w") as f:
            f.write("a\n")                       # shorter than the fingerprint length
        t = OffsetTailer(LocalFileSource(log), self.p("state.json"), start_at_end=True)
        for chunk in ("b\n", "c" * 400 + "\n", "d\n"):
            with open(log, "a") as f:
                f.write(chunk)
            self.assertEqual([ln + "\n" for ln in t.poll()], [chunk])


class DiscordTest(unittest.TestCase):
    def test_names_cannot_ping_or_break_formatting(self):
        d = Discord("https://example.invalid/hook", use_embeds=False)
        sent = {}
        with mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = b""
            d.post(Event("login", "@everyone *x*"), "S", {"login"})
            sent = json.loads(op.call_args[0][0].data)
        self.assertEqual(sent["allowed_mentions"], {"parse": []})
        self.assertIn(r"\*x\*", sent["content"])


class BackupWindowTest(TmpDir):
    def at(self, window, local):
        tz = maintenance.ZoneInfo("America/Los_Angeles")
        ts = dt.datetime.fromisoformat(local).replace(tzinfo=tz).timestamp()
        return maintenance.Maintenance({"backup": {"window": window}}, None, None, lambda *a: None,
                                       clock=lambda: ts, state_path=self.p("m.json"))

    def test_window_across_midnight_is_one_night(self):
        self.assertEqual(self.at("23:00-03:00", "2026-09-28T23:30").window_date(), "2026-09-28")
        self.assertEqual(self.at("23:00-03:00", "2026-09-29T00:30").window_date(), "2026-09-28")
        self.assertEqual(self.at("23:00-03:00", "2026-09-29T03:30").window_date(), "2026-09-28")

    def test_default_window_unchanged(self):
        self.assertEqual(self.at("02:00-06:00", "2026-09-28T02:10").window_date(), "2026-09-28")
        self.assertEqual(self.at("02:00-06:00", "2026-09-28T06:40").window_date(), "2026-09-28")


class StoreTest(TmpDir):
    def test_one_off_store_does_not_close_live_sessions(self):
        db = self.p("s.db")
        live = stats_db.Store(db, reconcile=True)
        live.login("Bjorn", 1000)
        stats_db.Store(db).close()               # e.g. --refresh-steam while the monitor runs
        self.assertEqual([r["player"] for r in stats_db.currently_online(live.conn)], ["Bjorn"])
        live.close()

    def test_steam_blip_keeps_achievement_counts(self):
        st = stats_db.Store(self.p("s.db"))
        st.save_profile("7656", unlocked=10, total=40)
        with mock.patch.object(steam, "check_key"), \
             mock.patch.object(steam, "fetch_schema", return_value=[]), \
             mock.patch.object(steam, "fetch_summaries", return_value={}), \
             mock.patch.object(steam, "fetch_achievements", return_value=(None, None, [], "error:timed out")), \
             mock.patch.object(steam.time, "sleep"), \
             mock.patch.object(st, "steam_ids_to_update", return_value=["7656"]):
            self.assertEqual(steam.update_all(st, "key", limit=5), 1)
        row = dict(st.conn.execute("SELECT unlocked, total, error FROM steam_profile").fetchone())
        self.assertEqual((row["unlocked"], row["total"], row["error"]), (10, 40, "error:timed out"))
        st.close()


if __name__ == "__main__":
    unittest.main()
