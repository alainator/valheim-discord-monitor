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


class Channel:
    def __init__(self, cid):
        self.id, self.mention, self.msgs = cid, f"<#{cid}>", {}

    async def send(self, embed=None, allowed_mentions=None):
        mid, ch = self.id * 100 + len(self.msgs), self

        class Msg:
            id, pinned = mid, False

            async def edit(self, embed=None):
                ch.msgs[mid]["embed"] = embed

            async def pin(self, reason=None):
                self.pinned = True
        self.msgs[mid] = {"embed": embed, "msg": Msg()}
        return self.msgs[mid]["msg"]

    async def fetch_message(self, mid):
        return self.msgs[mid]["msg"]


@unittest.skipIf(discord is None, "discord.py not installed")
class CommandGuideTest(unittest.TestCase):
    def test_each_group_gets_a_pinned_guide_in_its_channel(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            b = admin_bot.AdminBot({"token": "x", "channel_id": "4", "guild_id": "9", "admin_user_ids": [5],
                                    "save_dir": d}, "S")
            b.attach(db_path=os.path.join(d, "s.db"))
            chans = {1: Channel(1), 2: Channel(2), 3: Channel(3), 4: Channel(4)}
            for slot, cid in (("welcome", 1), ("bots", 2), ("plans", 3)):
                b._meta(f"layout:ch:{slot}", cid)
            guild = SimpleNamespace(get_channel=chans.get)

            async def run(create=True):
                client = b._build_client()
                b._register_commands(client.tree)
                b.client = client
                return await b._command_guides(guild, create)
            done = asyncio.run(run())
            self.assertEqual(sorted(c.id for c in done), [1, 2, 3, 4])      # admin guide in channel_id
            muninn = next(iter(chans[2].msgs.values()))
            self.assertTrue(muninn["msg"].pinned)
            text = muninn["embed"].description
            self.assertIn("`/muninn stats [player]`:", text)
            self.assertIn("`/muninn compare <player> [other]`:", text)
            self.assertIn("/valheim join", next(iter(chans[1].msgs.values()))["embed"].description)
            # Again (e.g. at start-up): the same messages are edited, nothing new is posted.
            asyncio.run(run(create=False))
            self.assertEqual([len(c.msgs) for c in chans.values()], [1, 1, 1, 1])


if __name__ == "__main__":
    unittest.main()
