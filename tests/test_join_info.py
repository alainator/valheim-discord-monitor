import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import extras  # noqa: E402
from valheim_discord_monitor import Event, LocalFileSource, ValheimLogParser, prime_live_state  # noqa: E402


def feed(lines):
    p = ValheimLogParser()
    return [ev for ln in lines for ev in p.feed(ln)]


class JoinCodeTest(unittest.TestCase):
    def test_code_from_join_lines_without_losing_the_count(self):
        evs = feed([
            '09/28/2026 10:00:00: Player joined server "Alheim Server" that has join code 034505, now 1 player(s)',
            '09/28/2026 10:05:00: Player connection lost server "Alheim Server" that has join code 034505, now 0 player(s)',
            '09/28/2026 11:00:00: Session "Alheim Server" with join code 777111 and IP 203.0.113.7:2456 is active with 0 player(s)',
        ])
        self.assertEqual([(e.kind, e.extra.get("code"), e.extra.get("count")) for e in evs],
                         [("join_code", "034505", None), ("count", None, 1),
                          ("count", None, 0), ("join_code", "777111", None)])
        self.assertEqual(evs[-1].extra["ip"], "203.0.113.7:2456")

    def test_a_restart_forgets_the_code(self):
        live = extras.LiveState()
        live.observe(Event("join_code", None, {"code": "034505", "ip": None, "ts": 1}), now=1)
        self.assertEqual(live.snapshot()["join_code"], "034505")
        live.observe(Event("server_restart", None, {"ts": 2}), now=2)
        self.assertIsNone(live.snapshot()["join_code"])

    def test_primed_from_the_log_only_for_the_current_session(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "console.log")
            with open(path, "w") as f:
                f.write('09/28/2026 09:00:00: Player joined server "A" that has join code 111111, now 1 player(s)\n'
                        "09/28/2026 09:30:00: OnApplicationQuit\n"
                        "09/28/2026 09:31:00: Game server connected\n"
                        '09/28/2026 09:40:00: Player joined server "A" that has join code 222222, now 1 player(s)\n')
            live = extras.LiveState()
            prime_live_state(live, LocalFileSource(path))
            self.assertEqual(live.join_code, "222222")
            with open(path, "a") as f:
                f.write("09/28/2026 10:00:00: OnApplicationQuit\n")
            live = extras.LiveState()
            prime_live_state(live, LocalFileSource(path))
            self.assertIsNone(live.join_code)


class JoinEmbedTest(unittest.TestCase):
    def make_bot(self, d, join=None):
        import admin_bot
        cfg = {"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}
        if join:
            cfg["join"] = join
        bot = admin_bot.AdminBot(cfg, "Alheim Server")
        live = extras.LiveState()
        live.observe(Event("server_online", None, {"ts": 1}), now=1)
        bot.attach(live=live)
        return bot, live

    def fields(self, embed):
        return {f["name"]: f["value"] for f in embed["fields"]}

    def test_with_code_address_password_and_permitted_list(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "permittedlist.txt"), "w") as f:
                f.write("// header\nV_76561198000000001\n")
            bot, live = self.make_bot(d, {"address": "valheim.example.com:2456", "password": "odin"})
            live.observe(Event("join_code", None, {"code": "034505", "ip": "203.0.113.7:2456", "ts": 2}), now=2)
            e = bot.join_embed()
            f = self.fields(e)
            self.assertEqual(e["title"], "⚔️ Join Alheim Server")
            self.assertIn("034505", f["Join code (any platform, crossplay)"])
            self.assertEqual(f["Address (PC / Steam)"], "**`valheim.example.com:2456`**")   # config wins
            self.assertEqual(f["Password"], "||`odin`||")
            self.assertIn("Add server", f["How to join"])
            self.assertIn("turned away", f["First time?"])

    def test_before_the_code_is_known(self):
        with tempfile.TemporaryDirectory() as d:
            bot, _ = self.make_bot(d, {"address": "YOUR.PUBLIC.IP:2456"})      # placeholder = unset
            f = self.fields(bot.join_embed())
            self.assertIn("Not known yet", f["Join code (any platform, crossplay)"])
            self.assertNotIn("Address (PC / Steam)", f)         # none configured or logged
            self.assertNotIn("Password", f)
            self.assertNotIn("First time?", f)                   # open server: no list note


if __name__ == "__main__":
    unittest.main()
