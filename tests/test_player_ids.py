import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import extras  # noqa: E402
import stat_channels  # noqa: E402
import stats_db  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

SWITCH = [
    "09/30/2026 12:58:43: PlayFab listen socket child connected to remote player 7BF1CB302F4DF6B4",
    '09/30/2026 12:58:43: Player joined server "S" that has join code 183427, now 1 player(s)',
    "09/30/2026 12:58:43: PlayFab socket with remote ID playfab/7BF1CB302F4DF6B4 received local Platform ID "
    "Nintendo_14212333628479574677",
    "09/30/2026 12:59:14: Got character ZDOID from Frankeem : 75807738:1",
]


def events(lines, parser=None):
    parser = parser or vdm.ValheimLogParser()
    return [e for line in lines for e in parser.feed(line)]


class ParserTest(unittest.TestCase):
    def test_login_carries_the_platform_id(self):
        login = next(e for e in events(SWITCH) if e.kind == "login")
        self.assertEqual(login.extra["platform_id"], "Nintendo_14212333628479574677")
        self.assertNotIn("steam_id", login.extra)

    def test_version_mismatch_names_the_platform(self):
        got = events(["09/30/2026 13:00:00: PlayFab socket with remote ID playfab/AB12 received local Platform "
                      "ID Steam_76561198017275560",
                      "09/30/2026 13:00:00: Network version check, their:39, mine:40"])
        mm = next(e for e in got if e.kind == "version_mismatch")
        self.assertEqual(mm.extra["platform_id"], "Steam_76561198017275560")
        self.assertFalse(mm.extra["newer"])
        # Without a handshake just before it (Steam-only server), no platform id.
        mm = next(e for e in events(["09/30/2026 13:00:00: Network version check, their:41, mine:40"])
                  if e.kind == "version_mismatch")
        self.assertIsNone(mm.extra["platform_id"])

    def test_day(self):
        got = events(["09/30/2026 22:14:02: Time 25920.5, day:15    nextm:27000  skipspeed:59.85"])
        self.assertEqual([(e.kind, e.extra["day"]) for e in got], [("world_day", 15)])
        live = extras.LiveState()
        live.observe(got[0])
        self.assertEqual(live.snapshot()["day"], 15)
        self.assertEqual(stat_channels.name_for("day", live.snapshot()), "☀️ Day 15")
        self.assertEqual(stat_channels.name_for("day", {}), "☀️ Day: after the next sleep")

    def test_day_is_read_from_the_log_at_start_up(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            with open(path, "w") as f:
                f.write("09/30/2026 22:14:02: Time 25920.5, day:15    nextm:27000  skipspeed:59.85\n"
                        "09/30/2026 23:00:00: World save (1/1) done. Total time [100ms]\n")
            live = extras.LiveState()
            vdm.prime_live_state(live, vdm.LocalFileSource(path))
        self.assertEqual(live.day, 15)


class WhoIsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def test_platform_key(self):
        self.assertEqual(stats_db.platform_key("V_76561198017275560"), "76561198017275560")
        self.assertEqual(stats_db.platform_key("Steam_76561198017275560"), "76561198017275560")
        self.assertEqual(stats_db.platform_key("N_1421"), stats_db.platform_key("Nintendo_1421"))
        self.assertEqual(stats_db.platform_key("7656"), "7656")

    def test_login_records_the_platform_and_who_is_finds_the_player(self):
        for e in events(SWITCH):
            vdm.record_event(self.st, e)
        community.link_player(self.c, "Frankeem", 42)
        # The ban/permitted lists write N_… where the log wrote Nintendo_…: still the same player.
        self.assertEqual(community.who_is(self.c, "N_14212333628479574677"),
                         {"characters": ["Frankeem"], "users": ["42"]})
        # An unknown account refused under a new name, asked for with /valheim request-access.
        community.request_access(self.c, "Newbie", 77)
        self.assertEqual(community.who_is(self.c, "V_1", "Newbie"), {"characters": ["Newbie"], "users": ["77"]})
        self.assertEqual(community.who_is(self.c, "V_1"), {"characters": [], "users": []})


@unittest.skipIf(discord is None, "discord.py not installed")
class NoticeTest(unittest.TestCase):
    def bot(self, d, lists=""):
        import admin_bot
        with open(os.path.join(d, "permittedlist.txt"), "w") as f:
            f.write("V_999\n")
        with open(os.path.join(d, "bannedlist.txt"), "w") as f:
            f.write(lists)
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                "save_dir": d}, "Alheim")
        b.attach(db_path=os.path.join(d, "s.db"))
        sent, dms = [], []

        class Channel:
            async def send(self, embed=None, view=None, allowed_mentions=None):
                sent.append(embed)
        b.client = type("C", (), {"get_channel": lambda self, cid: Channel()})()

        async def dm(uid, text):
            dms.append((uid, text))
            return True
        b._dm = dm
        st = stats_db.Store(os.path.join(d, "s.db"))
        for e in events(SWITCH):
            vdm.record_event(st, e)
        community.link_player(st.conn, "Frankeem", 42)
        st.close()
        return b, sent, dms

    def test_refused_notice_says_who_and_tells_them(self):
        with tempfile.TemporaryDirectory() as d:
            b, sent, dms = self.bot(d)
            asyncio.run(b._post_refused("Frankeem2", "N_14212333628479574677"))
        fields = {f.name: f.value for f in sent[0].fields}
        self.assertIn("<@42>", fields["Who this is"])
        self.assertIn("**Frankeem**", fields["Who this is"])
        self.assertEqual(dms[0][0], "42")
        self.assertIn("you're not on the permitted list", dms[0][1])
        self.assertEqual(sent[0].footer.text, "The player was told why by DM.")

    def test_banned_players_are_not_told(self):
        with tempfile.TemporaryDirectory() as d:
            b, sent, dms = self.bot(d, lists="N_14212333628479574677\n")
            asyncio.run(b._post_refused("Frankeem", "N_14212333628479574677"))
        self.assertEqual(dms, [])
        self.assertIn("ban list", sent[0].description)

    def test_version_mismatch_dm_once(self):
        with tempfile.TemporaryDirectory() as d:
            b, _, dms = self.bot(d)
            extra = {"their": 39, "mine": 40, "newer": False, "platform_id": "Nintendo_14212333628479574677"}
            asyncio.run(b._version_dm(extra))
            asyncio.run(b._version_dm(extra))               # a retry a minute later: no second DM
        self.assertEqual(len(dms), 1)
        self.assertIn("older than the server's", dms[0][1])


if __name__ == "__main__":
    unittest.main()
