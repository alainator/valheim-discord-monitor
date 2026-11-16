import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ev(kind):
    return vdm.Event(kind, None, {})


class SilenceWatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.w = extras.SilenceWatch(900, clock=self.clock)

    def test_a_quiet_log_alerts_once(self):
        self.clock.t += 899
        self.assertFalse(self.w.check(down=False))
        self.clock.t += 2
        self.assertTrue(self.w.check(down=False))
        self.assertIn("logged nothing for", self.w.reason())
        self.clock.t += 3600
        self.assertFalse(self.w.check(down=False))          # not again

    def test_lines_keep_it_quiet(self):
        for _ in range(10):
            self.clock.t += 600                              # "Connections N" every 10 minutes
            self.w.saw_lines()
            self.assertFalse(self.w.check(down=False))

    def test_a_shutdown_already_covers_it(self):
        self.clock.t += 5000
        self.assertFalse(self.w.check(down=True))

    def test_our_own_maintenance_holds_it(self):
        self.clock.t += 5000
        self.assertFalse(self.w.check(down=False, hold=True))
        self.assertTrue(self.w.check(down=False, hold=False))

    def test_an_unreadable_log_is_not_healthy(self):
        self.clock.t += 1000
        self.w.feed_failed(RuntimeError("No such file: /logs/valheim_console.log"))
        self.assertTrue(self.w.check(down=False))
        self.assertIn("can't be read", self.w.reason())
        self.assertIn("No such file", self.w.reason())

    def test_a_reboot_ends_it_without_a_second_post(self):
        self.clock.t += 1000
        self.assertTrue(self.w.check(down=False))
        self.w.saw_lines()
        self.assertFalse(self.w.observe(ev("count"), ready=False))   # still booting
        self.assertFalse(self.w.observe(ev("server_online"), ready=True))
        self.assertFalse(self.w.alerted)

    def test_a_frozen_host_coming_back_is_announced(self):
        self.clock.t += 1000
        self.assertTrue(self.w.check(down=False))
        self.w.saw_lines()
        self.assertTrue(self.w.observe(ev("count"), ready=True))
        self.assertFalse(self.w.observe(ev("count"), ready=True))    # once
        self.clock.t += 1000
        self.assertTrue(self.w.check(down=False))                    # and it can alert again

    def test_nothing_happens_without_an_alert(self):
        self.assertFalse(self.w.observe(ev("count"), ready=True))


if __name__ == "__main__":
    unittest.main()
