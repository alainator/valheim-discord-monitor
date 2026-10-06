import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import extras  # noqa: E402
import stats_db  # noqa: E402


def local(day, hour, minute=0):
    """Log time (offset 0) of a local date and time."""
    return int(dt.datetime(2026, 10, day, hour, minute).timestamp())


class WhenTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn
        self.now = local(12, 0)                                 # Monday 12 Oct 2026, midnight

    def tearDown(self):
        self.st.close()

    def play(self, player, start, end):
        self.st.login(player, start)
        self.st.logout(player, end)

    def test_grid(self):
        # Saturday 10 Oct (weekday 5): Ingrid 20:00-22:00, Bjorn 20:30-21:00.
        self.play("Ingrid", local(10, 20), local(10, 22))
        self.play("Bjorn", local(10, 20, 30), local(10, 21))
        grid = community.online_grid(self.c, 0, weeks=1, now=self.now)
        self.assertAlmostEqual(grid[5][20], 1.5)
        self.assertAlmostEqual(grid[5][21], 1.0)
        self.assertEqual(grid[5][19], 0)
        self.assertEqual(community.busiest(grid), (5, 20, 1.25))
        two = community.online_grid(self.c, 0, weeks=2, now=self.now)           # averaged over 2 weeks
        self.assertAlmostEqual(two[5][20], 0.75)
        mine = community.online_grid(self.c, 0, weeks=1, player="bjorn", now=self.now)
        self.assertAlmostEqual(mine[5][20], 0.5)
        self.assertEqual(sum(map(sum, mine)), 0.5)

    def test_sessions_across_midnight_and_the_window(self):
        self.play("Ingrid", local(11, 23, 30), local(12, 0, 30))               # Sun 23:30 -> past "now"
        self.play("Ingrid", local(1, 20), local(1, 21))                         # too long ago for 1 week
        grid = community.online_grid(self.c, 0, weeks=1, now=self.now)
        self.assertAlmostEqual(grid[6][23], 0.5)                                # cut at now
        self.assertEqual(sum(map(sum, grid)), 0.5)

    def test_render(self):
        self.play("Ingrid", local(10, 20), local(10, 22))
        grid = community.online_grid(self.c, 0, weeks=1, now=self.now)
        e = community.render_when(grid, 1, server_name="Alheim")
        self.assertEqual(e["title"], "🕰️ When people play on Alheim")
        self.assertIn("Sat  ·· ·· ·· ·· ·· ·· ·· ·· ·· ·· ██ ··", e["description"])
        self.assertIn("Mon  ·· ·· ·· ·· ·· ·· ·· ·· ·· ·· ·· ··", e["description"])
        self.assertIn("Best time for a game night: **Sat 20:00–22:00** (1.0 online on average)", e["description"])
        e = community.render_when(grid, 1, player="Ingrid")
        self.assertIn("Most likely on: **Sat 20:00–22:00** (100% of the time)", e["description"])
        empty = community.render_when([[0.0] * 24 for _ in range(7)], 4, player="Bjorn")
        self.assertEqual(empty["description"], "**Bjorn** hasn't played in the last 4 weeks.")

    def test_picture(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        grid = [[0.0] * 24 for _ in range(7)]
        self.assertIsNone(extras.heatmap(grid, "x"))
        grid[5][20] = 2.0
        png = extras.heatmap(grid, "Average players online")
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
