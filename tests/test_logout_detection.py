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


if __name__ == "__main__":
    unittest.main()
