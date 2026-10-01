import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import valheim_discord_monitor as vdm  # noqa: E402


def join(pf, name, owner, n):
    return [f"09/14/2026 07:44:34: PlayFab listen socket child connected to remote player {pf}",
            f'09/14/2026 07:44:34: Player joined server "S" that has join code 034505, now {n} player(s)',
            f"09/14/2026 07:45:02: Got character ZDOID from {name} : {owner}:1"]


class LogoutDetectionTest(unittest.TestCase):
    def feed(self, parser, lines):
        return [(e.kind, e.player) for line in lines for e in parser.feed(line)
                if e.kind in ("login", "logout")]

    def two_online(self):
        p = vdm.ValheimLogParser()
        self.feed(p, join("E15BFD931510F277", "Ingrid", 111, 1) + join("AAAA1111BBBB2222", "Bjorn", 222, 2))
        self.assertEqual(set(p.s.online), {"Ingrid", "Bjorn"})
        return p

    def test_disconnect_line_naming_the_connection(self):
        p = self.two_online()
        got = self.feed(p, ["09/14/2026 08:00:00: PlayFab socket with remote ID playfab/AAAA1111BBBB2222 "
                            "closed by remote"])
        self.assertEqual(got, [("logout", "Bjorn")])
        self.assertEqual(set(p.s.online), {"Ingrid"})

    def test_unrelated_lines_log_nobody_out(self):
        p = self.two_online()
        got = self.feed(p, ["09/14/2026 08:00:00: ZRpc timeout detected",
                            "ZPlayFabSocket::Dispose. State: CONNECTED",
                            "09/14/2026 08:00:01: Connection closed for peer 12345"])
        self.assertEqual(got, [])
        self.assertEqual(set(p.s.online), {"Ingrid", "Bjorn"})

    def test_unexplained_leave_is_logged_with_the_lines_around_it(self):
        p = self.two_online()
        lines = ["ZPlayFabSocket::Dispose. State: CONNECTED",
                 '09/14/2026 08:00:00: Player connection lost server "S" that has join code 034505, now 1 player(s)'] \
            + [f"09/14/2026 08:00:{i:02d}: something else {i}" for i in range(16)]
        with mock.patch.object(vdm.log, "warning") as warn:
            self.feed(p, lines)
        warn.assert_called_once()
        text = warn.call_args[0][0] % warn.call_args[0][1:]
        self.assertIn("still tracks Bjorn, Ingrid", text)
        self.assertIn("ZPlayFabSocket::Dispose", text)             # the lines before
        self.assertIn("something else 12", text)                   # and after

    def test_explained_leave_is_not_reported(self):
        p = self.two_online()
        lines = ['09/14/2026 08:00:00: Player connection lost server "S" that has join code 034505, now 1 player(s)',
                 "09/14/2026 08:00:00: Destroying abandoned non persistent zdo 222:7 owner 222"] \
            + [f"09/14/2026 08:00:{i:02d}: something else {i}" for i in range(16)]
        with mock.patch.object(vdm.log, "warning") as warn:
            got = self.feed(p, lines)
        self.assertEqual(got, [("logout", "Bjorn")])
        warn.assert_not_called()

    def test_restart_remembers_who_is_online(self):
        """What happened on 09/30: players joined, the monitor was restarted, and their
        later "abandoned zdo" lines (which only carry the owner id learned at spawn) were
        no longer recognised, so they stayed "In Valheim"."""
        import json
        before = vdm.ValheimLogParser()
        self.feed(before, join("7BF1CB302F4DF6B4", "Frankeem", 2107543341, 1)
                  + join("D30A9C442A56F3EC", "Hiemdalbars", -841609285, 2))
        saved = json.loads(json.dumps(before.state_dict()))      # through the JSON file
        after = vdm.ValheimLogParser()
        after.load_state(saved)
        got = self.feed(after, [
            "09/30/2026 11:10:39: Destroying abandoned non persistent zdo 2107543341:5863 owner 2107543341",
            "ZPlayFabSocket::Dispose. State: CLOSED",
            '09/30/2026 11:10:39: Player connection lost server "S" that has join code 183427, now 1 player(s)',
            "09/30/2026 11:34:05: Destroying abandoned non persistent zdo -841609285:1 owner -841609285",
            '09/30/2026 11:34:05: Player connection lost server "S" that has join code 183427, now 0 player(s)'])
        self.assertEqual(got, [("logout", "Frankeem"), ("logout", "Hiemdalbars")])
        # Without the saved state, neither leave is recognised.
        blank = vdm.ValheimLogParser()
        self.assertEqual(self.feed(blank, ["09/30/2026 11:10:39: Destroying abandoned non persistent zdo "
                                           "2107543341:5863 owner 2107543341"]), [])

    def test_leftovers_from_an_earlier_connection_are_not_a_leave(self):
        """Someone reconnects without a leave line we recognise: their character spawns
        with a new owner id, and the old id's objects are cleaned up later. That cleanup
        must not log them out; only their current owner id's does."""
        p = self.two_online()
        got = self.feed(p, ["09/14/2026 08:10:00: PlayFab listen socket child connected to remote player CCCC3333DDDD4444",
                            "09/14/2026 08:10:20: Got character ZDOID from Bjorn : 333:1",
                            "09/14/2026 08:11:00: Destroying abandoned non persistent zdo 222:9 owner 222"])
        self.assertEqual(got, [])
        self.assertIn("Bjorn", p.s.online)
        got = self.feed(p, ["09/14/2026 09:00:00: Destroying abandoned non persistent zdo 333:4 owner 333"])
        self.assertEqual(got, [("logout", "Bjorn")])

    def test_a_reconnect_keeps_its_connection(self):
        """Bjorn reconnects: his new connection must not be handed to the next player."""
        p = self.two_online()
        self.feed(p, ["09/14/2026 08:10:00: PlayFab listen socket child connected to remote player CCCC3333DDDD4444",
                      "09/14/2026 08:10:20: Got character ZDOID from Bjorn : 333:1"])
        self.assertEqual(p.s.pending_ids, [])
        got = self.feed(p, join("EEEE5555FFFF6666", "Erik", 444, 3)
                        + ["09/14/2026 09:00:00: PlayFab socket with remote ID playfab/CCCC3333DDDD4444 closed by remote"])
        self.assertEqual(got, [("login", "Erik"), ("logout", "Bjorn")])
        self.assertIn("Erik", p.s.online)

    def test_a_second_connection_ending_is_not_a_leave(self):
        """Steam: a player who's in connects again (e.g. Join Game from their friends list);
        that extra connection ending doesn't mean they left."""
        p = vdm.ValheimLogParser()
        self.feed(p, ["09/14/2026 07:44:34: Got connection SteamID 76561198017275560",
                      "09/14/2026 07:45:02: Got character ZDOID from Sven : 444:1"])
        got = self.feed(p, ["09/14/2026 08:00:00: Got connection SteamID 76561198017275560",
                            "09/14/2026 08:00:05: Peer 76561198017275560 disconnected"])
        self.assertEqual(got, [])
        self.assertIn("Sven", p.s.online)
        got = self.feed(p, ["09/14/2026 09:00:00: Peer 76561198017275560 disconnected"])
        self.assertEqual(got, [("logout", "Sven")])

    def test_bad_saved_state_is_ignored(self):
        p = vdm.ValheimLogParser()
        p.load_state({"online": 5})
        self.assertEqual(p.s.online, {})


if __name__ == "__main__":
    unittest.main()
