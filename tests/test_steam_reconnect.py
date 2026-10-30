import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import valheim_discord_monitor as vdm  # noqa: E402

BOOT = "10/29/2026 11:20:43: DungeonDB Start 19075"
READY = "10/29/2026 11:20:50: Game server connected"
DROPPED = "10/29/2026 18:51:45: Game server disconnected"
BACK = "10/29/2026 18:51:57: Game server connected"
FAILED = "10/29/2026 18:51:50: Game server connected failed"
QUIT = "10/29/2026 19:00:00: Game - OnApplicationQuit"
JOIN = ["10/29/2026 12:00:00: PlayFab listen socket child connected to remote player AAAA1111BBBB2222",
        '10/29/2026 12:00:00: Player joined server "S" that has join code 034505, now 1 player(s)',
        "10/29/2026 12:00:30: Got character ZDOID from Ingrid : 111:1"]


def kinds(parser, lines):
    return [e.kind for line in lines for e in parser.feed(line)
            if e.kind in ("server_restart", "server_online")]


class SteamReconnectTest(unittest.TestCase):
    def running(self):
        p = vdm.ValheimLogParser()
        self.assertEqual(kinds(p, [BOOT, READY]), ["server_online"])
        kinds(p, JOIN)
        self.assertEqual(set(p.s.online), {"Ingrid"})
        return p

    def test_reconnect_is_not_a_restart(self):
        p = self.running()
        self.assertEqual(kinds(p, [DROPPED, BACK]), [])
        self.assertEqual(set(p.s.online), {"Ingrid"})
        self.assertEqual(p.s.join_code, "034505")

    def test_failed_reconnect_is_not_a_start(self):
        p = self.running()
        self.assertEqual(kinds(p, [DROPPED, FAILED, FAILED, BACK]), [])
        self.assertEqual(set(p.s.online), {"Ingrid"})

    def test_clean_restart_is_still_announced(self):
        p = self.running()
        self.assertEqual(kinds(p, [QUIT]), ["server_restart"])
        self.assertEqual(kinds(p, [BOOT, READY]), ["server_online"])

    def test_crash_with_no_shutdown_line_is_still_announced(self):
        p = self.running()
        self.assertEqual(kinds(p, [BOOT, READY]), ["server_restart", "server_online"])

    def test_crash_during_a_steam_drop_is_still_announced(self):
        p = self.running()
        self.assertEqual(kinds(p, [DROPPED, BOOT, READY]), ["server_restart", "server_online"])

    def test_reconnects_stay_quiet_after_a_real_restart(self):
        p = self.running()
        kinds(p, [QUIT, BOOT, READY])
        self.assertEqual(kinds(p, [DROPPED, BACK]), [])

    def test_saved_state_survives_a_monitor_restart(self):
        p = self.running()
        q = vdm.ValheimLogParser()
        q.load_state(p.state_dict())
        self.assertEqual(kinds(q, [DROPPED, BACK]), [])

    def test_state_saved_by_the_old_version(self):
        for down, want in ((False, []), (True, ["server_online"])):
            d = self.running().state_dict()
            d.pop("ready")
            d["down"] = down
            q = vdm.ValheimLogParser()
            q.load_state(d)
            self.assertEqual(kinds(q, [BACK]), want)


if __name__ == "__main__":
    unittest.main()
