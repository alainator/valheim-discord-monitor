import datetime as dt
import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
import stats_db  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402


def at(day, hour=12):
    return dt.datetime(2026, 10, day, hour).timestamp()


def session(day, name, owner):
    return [f"10/{day:02d}/2026 20:00:00: Got character ZDOID from {name} : {owner}:1",
            f"10/{day:02d}/2026 20:30:00: Got character ZDOID from {name} : 0:0",
            f"10/{day:02d}/2026 20:59:00: Destroying abandoned non persistent zdo {owner}:9 owner {owner}"]


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.now = [at(1)]
        self.a = extras.LogArchive(os.path.join(self.d, "logs_archive"), clock=lambda: self.now[0])

    def write(self, lines):
        for line in lines:
            self.a.add(line + "\n")
        self.a.flush()

    def test_one_file_a_day_gzipped_when_the_day_is_over(self):
        self.write(["a", "b"])
        self.write(["c"])
        self.assertEqual([os.path.basename(f) for f in self.a.files()], ["valheim_console-2026-10-01.log"])
        with open(self.a.files()[0]) as f:
            self.assertEqual(f.read(), "a\nb\nc\n")
        self.now[0] = at(2)
        self.write(["d"])
        self.assertEqual([os.path.basename(f) for f in self.a.files()],
                         ["valheim_console-2026-10-01.log.gz", "valheim_console-2026-10-02.log"])
        with gzip.open(self.a.files()[0], "rt") as f:
            self.assertEqual(f.read(), "a\nb\nc\n")

    def test_keep_days(self):
        a = extras.LogArchive(self.a.folder, keep_days=2, clock=lambda: self.now[0])
        for day in (1, 2, 3, 4):
            self.now[0] = at(day)
            a.add(f"day {day}")
            a.flush()
        self.assertEqual([os.path.basename(f)[16:26] for f in a.files()], ["2026-10-02", "2026-10-03", "2026-10-04"])

    def test_nothing_to_write(self):
        self.a.flush()
        self.assertEqual(self.a.files(), [])


class BackfillFromArchiveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.log = os.path.join(self.d, "valheim_console.log")
        self.now = [at(1)]
        self.a = extras.LogArchive(os.path.join(self.d, "logs_archive"), clock=lambda: self.now[0])

    def tearDown(self):
        self.st.close()

    def deaths(self):
        return dict(self.st.conn.execute("SELECT player, COUNT(*) FROM deaths GROUP BY player").fetchall())

    def test_history_from_before_a_restart_comes_back(self):
        """Valheim wiped its log at the restart on the 2nd; the archive still has the 1st."""
        for line in session(1, "Ingrid", 111):
            self.a.add(line)
        self.a.flush()
        self.now[0] = at(2)
        live = session(2, "Bjorn", 222)
        for line in live:
            self.a.add(line)
        self.a.flush()
        with open(self.log, "w") as f:
            f.write("\n".join(live) + "\n")                       # only since the restart
        vdm.backfill(self.st, vdm.log_files(self.log) + self.a.files())
        self.assertEqual(self.deaths(), {"Ingrid": 1, "Bjorn": 1})   # the 2nd isn't counted twice
        vdm.backfill(self.st, vdm.log_files(self.log) + self.a.files())
        self.assertEqual(self.deaths(), {"Ingrid": 1, "Bjorn": 1})


if __name__ == "__main__":
    unittest.main()
