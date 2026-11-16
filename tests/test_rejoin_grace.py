import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def login(name, ts):
    return vdm.Event("login", name, {"ts": ts})


def logout(name, ts):
    return vdm.Event("logout", name, {"ts": ts})


def summary(secs, deaths=0):
    return {"duration": extras.fmt_duration(secs), "duration_seconds": secs,
            "deaths": deaths, "deaths_text": extras.deaths_text(deaths)}


class RejoinGraceTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.g = extras.RejoinGrace(180, slack=15, clock=self.clock)

    def test_a_quick_rejoin_drops_the_leave(self):
        self.g.hold(logout("Bjorn", 1000), summary(3600))
        self.clock.t += 40
        self.assertTrue(self.g.rejoin(login("Bjorn", 1040)))
        self.clock.t += 1000
        self.assertEqual(self.g.due(), [])                 # nothing left to post

    def test_the_next_summary_covers_both_parts(self):
        self.g.hold(logout("Bjorn", 1000), summary(3600, deaths=1))
        self.assertTrue(self.g.rejoin(login("Bjorn", 1040)))
        s = self.g.with_carry("Bjorn", summary(1800, deaths=2))
        self.assertEqual(s["duration_seconds"], 5400)
        self.assertEqual(s["duration"], "1h 30m")
        self.assertEqual(s["deaths"], 3)
        self.assertEqual(s["deaths_text"], " and died 3 times")
        self.assertIsNone(self.g.with_carry("Bjorn", None))     # used up

    def test_several_blips_add_up(self):
        self.g.hold(logout("Bjorn", 1000), self.g.with_carry("Bjorn", summary(600)))
        self.assertTrue(self.g.rejoin(login("Bjorn", 1030)))
        self.g.hold(logout("Bjorn", 1630), self.g.with_carry("Bjorn", summary(600)))
        self.assertTrue(self.g.rejoin(login("Bjorn", 1660)))
        self.assertEqual(self.g.with_carry("Bjorn", summary(600))["duration_seconds"], 1800)

    def test_a_late_return_is_a_new_visit(self):
        self.g.hold(logout("Bjorn", 1000), summary(3600))
        self.assertFalse(self.g.rejoin(login("Bjorn", 1000 + 181)))
        self.assertEqual(self.g.release_all()[0][0].player, "Bjorn")

    def test_someone_else_arriving_changes_nothing(self):
        self.g.hold(logout("Bjorn", 1000), summary(3600))
        self.assertFalse(self.g.rejoin(login("Ingrid", 1010)))
        self.assertIn("Bjorn", self.g.held)

    def test_released_after_real_time_when_the_log_is_quiet(self):
        self.g.hold(logout("Bjorn", 1000), summary(3600))
        self.clock.t += 180
        self.assertEqual(self.g.due(log_ts=1000), [])
        self.clock.t += 16                                  # window + poll slack
        got = self.g.due(log_ts=1000)
        self.assertEqual([(e.player, s["duration_seconds"]) for e, s in got], [("Bjorn", 3600)])

    def test_a_backlog_is_judged_by_log_time(self):
        """After a monitor restart a whole backlog arrives at once: real time barely moves,
        but a leave and a return hours apart in the log are still two visits."""
        self.g.hold(logout("Bjorn", 1000), summary(3600))
        self.assertEqual([e.player for e, _ in self.g.due(log_ts=1000 + 181)], ["Bjorn"])
        self.assertFalse(self.g.rejoin(login("Bjorn", 1000 + 7200)))

    def test_off_when_zero(self):
        self.assertFalse(extras.RejoinGrace(0))
        self.assertTrue(extras.RejoinGrace(180))


if __name__ == "__main__":
    unittest.main()
