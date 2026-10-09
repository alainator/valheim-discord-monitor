import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from valheim_discord_monitor import ValheimLogParser  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


def run(lines):
    p = ValheimLogParser()
    return [ev for line in lines for ev in p.feed(line)]


class ParserTest(unittest.TestCase):
    def test_steam(self):
        evs = run(["02/21/2026 23:50:01: Got connection SteamID 76561198035590204",
                   "02/21/2026 23:50:02: Peer 76561198035590204 has wrong password"])
        self.assertEqual([(e.kind, e.extra["host_id"]) for e in evs], [("wrong_password", "76561198035590204")])

    def test_crossplay(self):
        evs = run([
            "09/28/2026 07:44:34: PlayFab listen socket child connected to remote player BADBAD0000000001",
            "09/28/2026 07:44:34: PlayFab socket with remote ID playfab/BADBAD0000000001 received local Platform ID V_76561190000000001",
            "09/28/2026 07:44:35: Peer V_76561190000000001 has wrong password",
        ])
        self.assertEqual([e.extra["host_id"] for e in evs if e.kind == "wrong_password"], ["V_76561190000000001"])

    def test_a_playfab_id_becomes_the_platform_id(self):
        evs = run([
            "09/28/2026 07:44:34: PlayFab socket with remote ID playfab/BADBAD0000000001 received local Platform ID PlayStation_7371",
            "09/28/2026 07:44:35: Peer playfab/BADBAD0000000001 has wrong password",
            "09/28/2026 07:44:36: Peer BADBAD0000000001 has wrong password",
        ])
        self.assertEqual([e.extra["host_id"] for e in evs], ["PlayStation_7371", "PlayStation_7371"])

    def test_the_failed_connection_isnt_paired_with_the_next_login(self):
        evs = run([
            "09/28/2026 07:44:34: PlayFab listen socket child connected to remote player BADBAD0000000001",
            "09/28/2026 07:44:34: PlayFab socket with remote ID playfab/BADBAD0000000001 received local Platform ID V_76561190000000001",
            "09/28/2026 07:44:35: Peer V_76561190000000001 has wrong password",
            "09/28/2026 07:50:00: PlayFab listen socket child connected to remote player GOOD000000000002",
            "09/28/2026 07:50:00: PlayFab socket with remote ID playfab/GOOD000000000002 received local Platform ID V_76561190000000002",
            "09/28/2026 07:50:20: Got character ZDOID from Friend : 1122334455:1",
        ])
        self.assertEqual(next(e for e in evs if e.kind == "login").extra.get("steam_id"), "76561190000000002")


@unittest.skipIf(discord is None, "discord.py not installed")
class NoticeTest(unittest.TestCase):
    def bot(self, d, cfg=None, join=None):
        import admin_bot
        import community
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                "save_dir": d, "join": join or {}, **(cfg or {})}, "Alheim")
        b.attach(db_path=os.path.join(d, "s.db"))
        self.sent, self.dms = [], []
        sent = self.sent

        class Channel:
            async def send(self, embed=None, view=None, allowed_mentions=None):
                sent.append((embed, [c.label for c in view.children]))
        b.client = SimpleNamespace(get_channel=lambda cid: Channel())

        async def dm(uid, text):
            self.dms.append((uid, text))
            return True
        b._dm = dm
        b.db.execute("INSERT INTO play_sessions(player, login_at, last_seen_at) VALUES ('Ingrid', 100, 100)")
        b.db.execute("INSERT OR IGNORE INTO player_platform(platform_key, platform_id, player, updated_at) "
                     "VALUES ('76561190000000001', 'Steam_76561190000000001', 'Ingrid', 100)")
        community.link_player(b.db, "Ingrid", 42)
        return b

    def test_three_tries_then_quiet_for_an_hour(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            pid = "V_76561190000000001"
            self.assertIsNone(b._password_due(pid, 1000))
            self.assertIsNone(b._password_due(pid, 1060))
            self.assertEqual(b._password_due(pid, 1240), (3, 4))
            b._password_posted[pid] = 1240
            self.assertIsNone(b._password_due(pid, 1300))                    # told already
            self.assertIsNone(b._password_due("V_2", 1300))                  # someone else: their own count
            self.assertIsNone(b._password_due(pid, 1240 + 3600))             # old tries are forgotten...
            b._password_posted.clear()
            for t in (9000, 9100):
                b._password_due(pid, t)
            self.assertEqual(b._password_due(pid, 9200), (3, 3))            # ...and it can happen again

    def test_tries_far_apart_dont_add_up(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            for t in (0, 700, 1400, 2100):                                   # one every 11+ minutes
                self.assertIsNone(b._password_due("V_1", t))

    def test_settings(self):
        with tempfile.TemporaryDirectory() as d:
            off = self.bot(d, {"password_alerts": False})
            self.assertIsNone(off._password_due("V_1", 0))
            self.assertFalse(off.notify_wrong_password("V_1", 0))
            one = self.bot(d, {"password_alerts": {"after": 1, "minutes": 5}})
            self.assertEqual(one._password_due("V_1", 0), (1, 1))

    def test_notice_and_dm(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d, join={"password": "hunter2"})
            asyncio.run(b._post_wrong_password("V_76561190000000001", 3, 4))
        embed, buttons = self.sent[0]
        self.assertEqual(embed.title, "🔑 Wrong password, again and again")
        self.assertIn("with the wrong password **3 times** in 4 minutes", embed.description)
        self.assertEqual(buttons, ["Ban", "Ignore"])                         # nothing to permit
        fields = {f.name: f.value for f in embed.fields}
        self.assertIn("`V_76561190000000001`", fields["Platform ID"])
        self.assertIn("<@42>", fields["Who this is"])
        self.assertIn("**Ingrid**", fields["Who this is"])
        self.assertEqual(self.dms[0][0], "42")
        self.assertIn("wrong password. `/valheim join` shows the address and the password", self.dms[0][1])
        self.assertNotIn("hunter2", self.dms[0][1])                          # never the password itself
        self.assertEqual(embed.footer.text, "The player was told by DM where to find the password.")

    def test_no_password_in_the_join_info(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            asyncio.run(b._post_wrong_password("V_76561190000000001", 3, 4))
        self.assertIn("wrong password. Ask an admin for it.", self.dms[0][1])

    def test_a_stranger(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            asyncio.run(b._post_wrong_password("V_999", 5, 2))
        fields = {f.name: f.value for f in self.sent[0][0].fields}
        self.assertIn("Nobody the bot has seen before", fields["Who this is"])
        self.assertEqual(self.dms, [])

    def test_buttons_are_redrawn_as_they_were(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            view = b._buttons("V_1", actions=("ban", "ignore"))
            msg = SimpleNamespace(components=[SimpleNamespace(children=view.children)])
            self.assertEqual(admin_bot.AdminBot._shown_actions(msg), ("ban", "ignore"))
            self.assertEqual(admin_bot.AdminBot._shown_actions(SimpleNamespace(components=[])),
                             ("permit", "ban", "ignore"))


if __name__ == "__main__":
    unittest.main()
