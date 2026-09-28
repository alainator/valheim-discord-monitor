import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin_bot import ServerLists, steam_profile  # noqa: E402
from valheim_discord_monitor import ValheimLogParser  # noqa: E402


def run(lines):
    p = ValheimLogParser()
    return [ev for line in lines for ev in p.feed(line)]


class ParserTest(unittest.TestCase):
    def test_crossplay_refusal_then_real_login_links_right_steam_id(self):
        evs = run([
            "09/14/2026 07:44:34: PlayFab listen socket child connected to remote player BADBAD0000000001",
            "09/14/2026 07:44:34: PlayFab socket with remote ID playfab/BADBAD0000000001 received local Platform ID Steam_76561190000000001",
            "09/14/2026 07:44:35: Player Griefer : Steam_76561190000000001 is blacklisted or not in whitelist.",
            "09/14/2026 07:50:00: PlayFab listen socket child connected to remote player GOOD000000000002",
            "09/14/2026 07:50:00: PlayFab socket with remote ID playfab/GOOD000000000002 received local Platform ID Steam_76561190000000002",
            "09/14/2026 07:50:20: Got character ZDOID from Friend : 1122334455:1",
        ])
        refused = [e for e in evs if e.kind == "join_refused"]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0].player, "Griefer")
        self.assertEqual(refused[0].extra["host_id"], "Steam_76561190000000001")
        login = next(e for e in evs if e.kind == "login")
        self.assertEqual(login.extra.get("steam_id"), "76561190000000002")

    def test_steam_only_bare_id(self):
        evs = run([
            "02/21/2021 23:50:01: Got connection SteamID 76561198035590204",
            "02/21/2021 23:50:02: Player Myawy : 76561198035590204 is blacklisted or not in whitelist.",
        ])
        self.assertEqual([(e.kind, e.player, e.extra["host_id"]) for e in evs],
                         [("join_refused", "Myawy", "76561198035590204")])

    def test_name_with_spaces_and_colon(self):
        evs = run(["Player Thorvald the: Bold : Xbox_2535400000000000 is blacklisted or not in whitelist."])
        self.assertEqual(evs[0].player, "Thorvald the: Bold")
        self.assertEqual(evs[0].extra["host_id"], "Xbox_2535400000000000")


class ListsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lists = ServerLists(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, which, text):
        with open(self.lists.path(which), "w") as f:
            f.write(text)

    def read(self, which):
        with open(self.lists.path(which)) as f:
            return f.read()

    def test_permit_with_open_server_only_unbans(self):
        self.write("banned", "// List banned players ID  ONE per line\nSteam_7656111\n")
        self.write("permitted", "// List permitted players ID  ONE per line\n")
        self.assertEqual(self.lists.permit("Steam_7656111"), ["removed from bannedlist.txt"])
        self.assertEqual(self.lists.ids("banned"), [])
        # Must not start a permitted list: that would lock everyone else out.
        self.assertEqual(self.lists.ids("permitted"), [])

    def test_permit_adds_when_permitted_list_in_use(self):
        self.write("permitted", "// header\nSteam_7656000\n")
        self.assertEqual(self.lists.permit("Steam_7656222"), ["added to permittedlist.txt"])
        self.assertEqual(self.read("permitted"), "// header\nSteam_7656000\nSteam_7656222\n")
        self.assertEqual(self.lists.permit("Steam_7656222"), [])       # idempotent

    def test_ban_creates_file_and_removes_from_permitted(self):
        self.write("permitted", "// header\n76561190000000009\nSteam_7656000\n")
        changes = self.lists.ban("Steam_76561190000000009")             # bare id in the file still matches
        self.assertEqual(changes, ["added to bannedlist.txt", "removed from permittedlist.txt"])
        self.assertEqual(self.lists.ids("banned"), ["Steam_76561190000000009"])
        self.assertEqual(self.lists.ids("permitted"), ["Steam_7656000"])
        self.assertTrue(self.read("banned").startswith("//"))

    def test_refusal_reason(self):
        self.write("banned", "Steam_1\n")
        self.write("permitted", "Steam_2\n")
        self.assertEqual(self.lists.refusal_reason("Steam_1"), "on the ban list")
        self.assertEqual(self.lists.refusal_reason("Steam_3"), "not on the permitted list")

    def test_steam_profile(self):
        self.assertEqual(steam_profile("Steam_76561198000000000"),
                         "https://steamcommunity.com/profiles/76561198000000000")
        self.assertIsNone(steam_profile("Xbox_2535400000000000"))


if __name__ == "__main__":
    unittest.main()
