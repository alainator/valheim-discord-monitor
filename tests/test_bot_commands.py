import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


@unittest.skipIf(discord is None, "discord.py not installed")
class BotCommandsTest(unittest.TestCase):
    def test_every_command_registers(self):
        """Builds the real command tree offline, the same step the bot runs on start-up.
        A bad parameter annotation fails here instead of taking the bot down."""
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")

            async def build():
                client = bot._build_client()
                bot._register_commands(client.tree)
                group = client.tree.get_commands()[0]
                # to_dict() resolves every parameter type, like the sync to Discord does.
                payload = group.to_dict(client.tree)
                return group.name, sorted(c["name"] for c in payload["options"])
            name, commands = asyncio.run(build())
        self.assertEqual(name, "valheim")
        self.assertEqual(commands, sorted(["permit", "ban", "unban", "unpermit", "online", "backups",
                                           "update-check", "restart", "restart-cancel", "lists"]))


if __name__ == "__main__":
    unittest.main()
