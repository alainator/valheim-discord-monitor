import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
from valheim_discord_monitor import Event, ValheimLogParser  # noqa: E402


def run(lines):
    p = ValheimLogParser()
    return [ev for line in lines for ev in p.feed(line)]


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class ParserTest(unittest.TestCase):
    def test_lobby_failures(self):
        evs = run([
            '10/18/2026 05:59:01: [PlayFab] Create lobby failed: The number of lobbies exceeds the limit.',
            '10/18/2026 05:59:01: ZPlayFabMatchmaking UnregisterServer',
            '10/18/2026 06:00:00: Failed to create lobby, retrying',
        ])
        self.assertEqual([e.kind for e in evs], ["lobby_lost"] * 3)
        self.assertIn("number of lobbies exceeds the limit", evs[0].extra["detail"])

    def test_a_new_join_code_is_a_new_lobby(self):
        evs = run([
            '10/18/2026 03:00:00: Session "Alheim" with join code 111111 and IP 203.0.113.7:2456 is active with 0 player(s)',
            '10/18/2026 03:10:00: Session "Alheim" with join code 111111 and IP 203.0.113.7:2456 is active with 0 player(s)',
            '10/18/2026 03:20:00: Session "Alheim" with join code 222222 and IP 203.0.113.7:2456 is active with 0 player(s)',
        ])
        self.assertEqual([e.extra["code"] for e in evs if e.kind == "join_code"], ["111111", "222222"])


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.w = extras.LobbyWatch({}, clock=self.clock)

    def lost(self):
        self.w.observe(Event("lobby_lost", None, {"detail": "Create lobby failed: lobbies exceeds the limit"}))

    def later(self, minutes):
        self.clock.t += minutes * 60

    def test_a_lobby_that_comes_back_is_left_alone(self):
        """What happened several times overnight: lost, then a new lobby within minutes."""
        for _ in range(3):
            self.lost()
            self.later(2)
            self.w.observe(Event("join_code", None, {"code": "123456"}))
            self.later(30)
            self.assertIsNone(self.w.check(0, True))

    def test_gone_for_good_and_empty_restarts(self):
        self.lost()
        self.later(9)
        self.assertIsNone(self.w.check(0, True))                 # still within the grace period
        self.later(2)
        kind, text = self.w.check(0, True)
        self.assertEqual(kind, "restart")
        self.assertIn("lost its crossplay (PlayFab) lobby 11 minutes ago", text)
        self.assertIn("lobbies exceeds the limit", text)
        self.assertIn("Nobody is online, so it's restarting now.", text)
        self.assertIsNone(self.w.check(0, True))                 # asked once

    def test_at_most_one_restart_an_hour(self):
        self.lost()
        self.later(11)
        self.assertEqual(self.w.check(0, True)[0], "restart")
        self.w.observe(Event("server_online", None, {}))
        self.later(5)
        self.lost()                                             # it fails again right away
        self.later(11)
        kind, text = self.w.check(0, True)
        self.assertEqual(kind, "alert")
        self.assertIn("restarted for this less than an hour ago", text)
        self.assertIsNone(self.w.check(0, True))                 # told once
        self.later(45)
        self.assertEqual(self.w.check(0, True)[0], "restart")   # an hour on: it may try again

    def test_people_online_get_an_alert_not_a_restart(self):
        self.lost()
        self.later(15)
        kind, text = self.w.check(2, True)
        self.assertEqual(kind, "alert")
        self.assertIn("2 players are still connected", text)
        self.assertIsNone(self.w.check(2, True))
        self.assertEqual(self.w.check(0, True)[0], "restart")   # once they've left

    def test_no_restart_possible_or_wanted(self):
        self.lost()
        self.later(15)
        kind, text = self.w.check(0, False)                       # no updater link
        self.assertEqual(kind, "alert")
        self.assertIn("Restart it to fix this", text)
        w = extras.LobbyWatch({"restart": False, "minutes": 5}, clock=self.clock)
        w.observe(Event("lobby_lost", None, {}))
        self.later(6)
        self.assertEqual(w.check(0, True)[0], "alert")

    def test_a_restart_or_shutdown_clears_it(self):
        for kind in ("server_restart", "server_online"):
            self.lost()
            self.w.observe(Event(kind, None, {}))
            self.later(20)
            self.assertIsNone(self.w.check(0, True))

    def test_the_first_failure_counts(self):
        """Repeated failures don't push the deadline back."""
        self.lost()
        for _ in range(5):
            self.later(2)
            self.lost()
        self.assertEqual(self.w.check(0, True)[0], "restart")


if __name__ == "__main__":
    unittest.main()
