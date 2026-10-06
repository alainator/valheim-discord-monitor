import datetime as dt
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import extras  # noqa: E402
import stats_db  # noqa: E402

D = dt.date


def noon(day: dt.date) -> int:
    """Log time (offset 0) of local noon on a day."""
    return int(dt.datetime(day.year, day.month, day.day, 12).timestamp())


class StreakTest(unittest.TestCase):
    def test_current_and_best(self):
        days = {D(2026, 10, 1), D(2026, 10, 2), D(2026, 10, 3), D(2026, 10, 5), D(2026, 10, 6)}
        self.assertEqual(community.current_streak(days, D(2026, 10, 6)), 2)
        self.assertEqual(community.current_streak(days, D(2026, 10, 7)), 2)    # not played yet today: still alive
        self.assertEqual(community.current_streak(days, D(2026, 10, 8)), 0)    # missed a day
        self.assertEqual(community.best_streak(days), 3)
        self.assertEqual(community.best_streak(set()), 0)


class DB(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def play(self, player, day, minutes=60):
        self.st.login(player, noon(day))
        self.st.logout(player, noon(day) + minutes * 60)


class StreakBoardTest(DB):
    def test_board_stats_and_top(self):
        for n in range(4):
            self.play("Ingrid", D(2026, 9, 1) + dt.timedelta(days=n))
        self.play("Bjorn", D(2026, 9, 1))
        self.play("Bjorn", D(2026, 9, 2))
        self.play("Bjorn", D(2026, 9, 2), 30)                   # two sessions on a day count once
        self.assertEqual(community.streak_board(self.c), [{"player": "Ingrid", "v": 4}, {"player": "Bjorn", "v": 2}])
        top = community.render_top("streak", community.top(self.c, "streak"))
        self.assertEqual(top["title"], "🏆 Longest play streak (days in a row)")
        self.assertIn("🥇 **Ingrid**: 4 days", top["description"])
        with mock.patch.object(community._dt, "date", wraps=dt.date) as fake:
            fake.today.return_value = D(2026, 9, 5)
            s = community.player_stats(self.c, "ingrid")
        self.assertEqual((s["streak"], s["best_streak"]), (4, 4))
        field = next(f for f in community.render_stats(s, 0)["fields"] if f["name"] == "🔥 Play streak")
        self.assertEqual(field["value"], "4 days now · best 4 days")
        s["streak"] = 0
        field = next(f for f in community.render_stats(s, 0)["fields"] if f["name"] == "🔥 Play streak")
        self.assertEqual(field["value"], "best 4 days")


class BirthdayTest(DB):
    def setUp(self):
        super().setUp()
        self.play("Ingrid", D(2025, 10, 6), 180)
        self.play("Bjorn", D(2025, 10, 7))
        self.play("Bjorn", D(2025, 10, 8))
        self.st.death("Bjorn", noon(D(2025, 10, 8)) + 60)

    def at(self, day):
        return dt.datetime(day.year, day.month, day.day, 13).astimezone()

    def test_one_year(self):
        e = extras.server_birthday(self.c, self.at(D(2026, 10, 6)), 0, "Alheim")
        self.assertEqual(e["title"], "🎂 Alheim is 1 year old today!")
        fields = {f["name"]: f["value"] for f in e["fields"]}
        self.assertEqual(fields["Vikings"], "2")
        self.assertEqual(fields["Deaths"], "1")
        self.assertTrue(fields["First to set sail"].startswith("**Ingrid**, <t:"))
        self.assertTrue(fields["Most time in-game"].startswith("**Ingrid**: 3h"))
        self.assertEqual(fields["Longest streak"], "**Bjorn**: 2 days in a row")

    def test_100_days_and_other_days(self):
        e = extras.server_birthday(self.c, self.at(D(2025, 10, 6) + dt.timedelta(days=100)), 0, "Alheim")
        self.assertEqual(e["title"], "🎂 100 days of Alheim!")
        self.assertIsNone(extras.server_birthday(self.c, self.at(D(2026, 10, 7)), 0, "Alheim"))

    def test_empty_database(self):
        st = stats_db.Store(os.path.join(self.d, "empty.db"))
        self.assertIsNone(extras.server_birthday(st.conn, self.at(D(2026, 10, 6)), 0, "Alheim"))
        st.close()


if __name__ == "__main__":
    unittest.main()
