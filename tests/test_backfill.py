import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import stats_db  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402


def session(day, name, owner, hour=20):
    t = f"09/{day:02d}/2026 {hour:02d}"
    return [f"{t}:00:00: Got character ZDOID from {name} : {owner}:1",
            f"{t}:30:00: Got character ZDOID from {name} : 0:0",
            f"{t}:59:00: Destroying abandoned non persistent zdo {owner}:9 owner {owner}"]


def write(path, lines, gz=False):
    opener = gzip.open if gz else open
    with opener(path, "wt") as f:
        f.write("\n".join(lines) + "\n")


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.log = os.path.join(self.d, "valheim_console.log")

    def tearDown(self):
        self.st.close()

    def counts(self):
        c = self.st.conn
        return (c.execute("SELECT COUNT(*) FROM play_sessions").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM deaths").fetchone()[0])

    def test_rotated_logs_oldest_first_and_safe_to_repeat(self):
        write(self.log + "-20260928.gz", session(27, "Ingrid", 111), gz=True)
        write(self.log + ".1", session(28, "Bjorn", 222))
        write(self.log, session(29, "Ingrid", 333))
        files = vdm.log_files(self.log)
        self.assertEqual([os.path.basename(f) for f in files],
                         ["valheim_console.log-20260928.gz", "valheim_console.log.1", "valheim_console.log"])
        vdm.backfill(self.st, files)
        self.assertEqual(self.counts(), (3, 3))
        vdm.backfill(self.st, files)                                   # again: nothing doubles
        self.assertEqual(self.counts(), (3, 3))

    def test_history_older_than_the_logs_is_kept(self):
        """The oldest logs were rotated away since the first backfill: what they gave stays."""
        write(self.log + ".1", session(28, "Bjorn", 222))
        write(self.log, session(29, "Ingrid", 333))
        vdm.backfill(self.st, vdm.log_files(self.log))
        os.remove(self.log + ".1")
        write(self.log, session(29, "Ingrid", 333) + session(30, "Sigrid", 444))
        vdm.backfill(self.st, vdm.log_files(self.log))
        players = sorted(r[0] for r in self.st.conn.execute("SELECT player FROM play_sessions"))
        self.assertEqual(players, ["Bjorn", "Ingrid", "Sigrid"])

    def test_someone_still_online_is_handed_to_the_live_parser(self):
        write(self.log, ["09/30/2026 20:00:00: Got character ZDOID from Ingrid : 111:1"])
        n, parser = vdm.backfill(self.st, [self.log])
        self.assertEqual((n, list(parser.s.online)), (1, ["Ingrid"]))
        live = vdm.ValheimLogParser()
        live.load_state(parser.state_dict())
        got = [e.kind for e in live.feed("09/30/2026 21:00:00: Destroying abandoned non persistent zdo 111:2 owner 111")]
        self.assertEqual(got, ["logout"])

    def test_no_timestamps_does_nothing(self):
        write(self.log, ["no timestamps here"])
        self.assertEqual(vdm.backfill(self.st, [self.log])[0], 0)


if __name__ == "__main__":
    unittest.main()
