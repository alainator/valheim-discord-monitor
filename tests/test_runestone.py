import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


class Stone:
    """A fake #runestone that remembers its messages."""

    def __init__(self):
        self.id, self.mention, self.msgs = 77, "<#77>", {}

    async def send(self, content=None, embed=None, allowed_mentions=None, view=None):
        mid = 1000 + len(self.msgs)
        stone = self

        class Msg:
            id, pinned, jump_url = mid, False, f"https://discord.com/channels/9/77/{mid}"

            async def edit(self, embed=None):
                stone.msgs[mid]["embed"] = embed

            async def pin(self, reason=None):
                self.pinned = True
                stone.msgs[mid]["pinned"] = True
        msg = Msg()
        self.msgs[mid] = {"content": content, "embed": embed, "msg": msg, "pinned": False}
        return msg

    async def fetch_message(self, mid):
        if mid not in self.msgs:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return self.msgs[mid]["msg"]


@unittest.skipIf(discord is None, "discord.py not installed")
class RunestoneTest(unittest.TestCase):
    def bot(self, d, **cfg):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d, **cfg}, "Alheim")
        b.attach(db_path=os.path.join(d, "s.db"))
        stone = Stone()
        b.client = SimpleNamespace(get_channel=lambda cid: stone if cid == 77 else None)
        return b, stone

    def test_rules_are_one_pinned_message_edited_in_place(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            b, stone = self.bot(d)
            self.assertIsNone(asyncio.run(b._runestone()))         # no #runestone yet
            b._meta("layout:ch:rules", 77)                           # /odin setup made it
            ch = asyncio.run(b._runestone())
            msg, created = asyncio.run(b._post_rules(ch, admin_bot.DEFAULT_RULES))
            self.assertTrue(created)
            self.assertTrue(stone.msgs[msg.id]["pinned"])
            self.assertIn("No griefing", stone.msgs[msg.id]["embed"].description)
            msg2, created = asyncio.run(b._post_rules(ch, "Be nice."))
            self.assertFalse(created)
            self.assertEqual((msg2.id, len(stone.msgs)), (msg.id, 1))
            self.assertEqual(stone.msgs[msg.id]["embed"].description, "Be nice.")
            self.assertEqual(b._meta("rules:text"), "Be nice.")

    def test_announce_channel_id_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            b, stone = self.bot(d, announce_channel_id="77")
            self.assertIs(asyncio.run(b._runestone()), stone)

    def test_news_only_when_turned_on(self):
        with tempfile.TemporaryDirectory() as d:
            b, stone = self.bot(d, announce_channel_id="77")
            self.assertFalse(b.runestone_news)
            b, stone = self.bot(d, announce_channel_id="77", runestone_news=True)
            asyncio.run(b._post_news("✅ Valheim updated: **1.0.16** → **1.0.17**."))
            self.assertIn("1.0.17", stone.msgs[1000]["embed"].description)

    def test_boss_kill_is_kept_in_the_runestone(self):
        import struct
        import gzip
        with tempfile.TemporaryDirectory() as d:
            b, stone = self.bot(d, announce_channel_id="77", runestone_news=True)
            world = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(world)

            def save(n, keys):
                packed = gzip.compress(b"".join(bytes([len(k)]) + k.encode() for k in keys))
                with open(os.path.join(world, f"_main.{n}.db2"), "wb") as f:
                    f.write(struct.pack("<i", 41) + b"\x00" * 8 + struct.pack("<i", len(packed)) + packed)
            save(1, ["defeated_eikthyr"])
            asyncio.run(b._check_bosses())                           # baseline
            save(2, ["defeated_eikthyr", "defeated_gdking"])
            asyncio.run(b._check_bosses())
            self.assertEqual(stone.msgs[1000]["embed"].title, "⚔️ The Elder has fallen!")


if __name__ == "__main__":
    unittest.main()
