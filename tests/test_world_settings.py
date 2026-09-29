import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import world_settings as ws  # noqa: E402


def read(path):
    with open(path) as f:
        return f.read()


class RulesTest(unittest.TestCase):
    def test_parse_format_round_trip(self):
        st = ws.parse_args("-modifier raids less -setkey nomap -preset hard")
        self.assertEqual((st.preset, st.modifiers, st.setkeys), ("hard", {"raids": "less"}, {"nomap"}))
        self.assertEqual(ws.format_args(st), "-preset hard -modifier raids less -setkey nomap")
        self.assertEqual(ws.parse_args(ws.format_args(st)), st)

    def test_apply(self):
        st = ws.parse_args("-modifier raids less")
        st = ws.apply(st, "modifier", "combat", "hard")
        st = ws.apply(st, "modifier", "raids", "normal")          # normal = remove
        st = ws.apply(st, "setkey", "passivemobs", "on")
        st = ws.apply(st, "preset", "casual")
        self.assertEqual(ws.format_args(st), "-preset casual -modifier combat hard -setkey passivemobs")
        st = ws.apply(ws.apply(st, "setkey", "passivemobs", "off"), "preset", "default")
        self.assertEqual(ws.format_args(st), "-modifier combat hard")

    def test_only_known_values(self):
        st = ws.State()
        for bad in (("modifier", "combat", "insane"), ("modifier", "savedir", "x"), ("preset", "godmode", ""),
                    ("setkey", "nomap", "maybe"), ("setkey", "devcommands", "on"), ("port", "2456", "")):
            with self.assertRaises(ValueError):
                ws.apply(st, *bad)


class HostScriptTest(unittest.TestCase):
    """The host side end to end: the request handler (bash) calling this file as the writer."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = os.path.join(self.d, "world-settings.env")
        with open(self.env, "w") as f:
            f.write("WORLD_ARGS=-modifier raids less\n")
        os.makedirs(os.path.join(self.d, "bot"))
        with open(os.path.join(ROOT, "host", "valheim-bot-request.sh")) as f:
            script = f.read()
        for old, new in (("/home/valheim/bot", os.path.join(self.d, "bot")),
                         ("/home/valheim/update_check.log", os.path.join(self.d, "update.log")),
                         ("/home/valheim/valheim-world-settings.py", os.path.join(ROOT, "world_settings.py")),
                         ("/home/valheim/world-settings.env", self.env)):
            script = script.replace(old, new)
        self.handler = os.path.join(self.d, "handler.sh")
        with open(self.handler, "w") as f:
            f.write(script)

    def request(self, text):
        with open(os.path.join(self.d, "bot", "request"), "w") as f:
            f.write(text + "\n")
        subprocess.run(["bash", self.handler], check=True)
        self.assertFalse(os.path.exists(os.path.join(self.d, "bot", "request")))

    def test_set_and_reject(self):
        self.request("set modifier combat hard")
        self.request("set setkey nomap on")
        self.assertEqual(ws.format_args(ws.read_file(self.env)), "-modifier combat hard -modifier raids less -setkey nomap")
        # Injection attempts: the handler strips everything but a-z0-9 and spaces, and
        # the writer only accepts known values, so the file is unchanged.
        before = read(self.env)
        self.request("set modifier combat hard; rm -rf /")
        self.request('set modifier raids "-savedir /tmp"')
        self.request("set preset godmode")
        self.assertEqual(read(self.env), before)
        log = read(os.path.join(self.d, "update.log"))
        self.assertIn("World setting from Discord (modifier combat hard): WORLD_ARGS=-modifier combat hard", log)
        self.assertIn("ERROR: unknown preset 'godmode'", log)


class ChangeFlowTest(unittest.TestCase):
    def test_command_waits_for_the_host_and_confirms(self):
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py not installed")
        import admin_bot
        import updater
        d = tempfile.mkdtemp()
        env = os.path.join(d, "world-settings.env")
        with open(env, "w") as f:
            f.write("WORLD_ARGS=-modifier raids less\n")
        bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d,
                                  "world_settings": {"file": env}}, "S")
        bot.attach(updater=updater.UpdateWatcher({"bot_dir": d}))
        it = mock.MagicMock()
        it.response.send_message = mock.AsyncMock()
        it.response.defer = mock.AsyncMock()
        it.followup.send = mock.AsyncMock()

        async def run():
            real = asyncio.sleep

            async def host_acts(n):                   # the host helper picks up the request
                req = os.path.join(d, "request")
                if os.path.exists(req):
                    _, kind, key, value = read(req).split()
                    ws.write_file(env, ws.apply(ws.read_file(env), kind, key, value))
                    os.remove(req)
                await real(0)
            with mock.patch.object(admin_bot.asyncio, "sleep", host_acts):
                await bot._change_setting(it, "modifier", "raids", "more")
        asyncio.run(run())
        text = it.followup.send.call_args[0][0]
        self.assertIn("✅ Saved: **modifier raids → more**", text)
        self.assertIn("-modifier raids more", text)
        self.assertEqual(ws.format_args(ws.read_file(env)), "-modifier raids more")

        it.response.send_message.reset_mock()
        asyncio.run(bot._change_setting(it, "modifier", "raids", "insane"))
        self.assertIn("Not changed", it.response.send_message.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
