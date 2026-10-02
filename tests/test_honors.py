import asyncio
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import stats_db  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


class HonorDBTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def test_built_ins_and_lookup(self):
        names = [h["name"] for h in community.honors(self.c)]
        self.assertEqual(len(names), 16)
        self.assertEqual(names[:5], ["Hermóðr", "Andhrímnir", "Svaðilfari", "Freyr", "Gangleri"])
        self.assertEqual(community.get_honor(self.c, "hermodr")["name"], "Hermóðr")
        self.assertEqual(community.get_honor(self.c, "HERMÓÐR")["name"], "Hermóðr")      # case and accents
        self.assertEqual([h["name"] for h in community.honors(self.c, "cook")], ["Andhrímnir"])

    def test_custom_give_take_and_render(self):
        self.assertIsNone(community.create_honor(self.c, "Tree Whisperer", "🌲", "dies to falling trees", 0x2ECC71, 5))
        self.assertIn("already", community.create_honor(self.c, "tree whisperer", "🌲", "", 0, 5))
        self.assertIn("letter", community.create_honor(self.c, "!!!", "🌲", "", 0, 5))
        self.assertTrue(community.give_honor(self.c, "treewhisperer", 42, "admin", "third time this week"))
        self.assertFalse(community.give_honor(self.c, "treewhisperer", 42, "admin"))     # already
        community.give_honor(self.c, "hermodr", 42, "admin")
        self.assertEqual([h["name"] for h in community.user_honors(self.c, 42)], ["Tree Whisperer", "Hermóðr"])
        mine = community.render_honors(self.c, 42)["description"]
        self.assertIn("🌲 **Tree Whisperer**: *third time this week*", mine)
        everyone = community.render_honors(self.c)["description"]
        self.assertIn("⚰️ **Hermóðr** (crypt raider): <@42>", everyone)
        self.assertIn("**Tree Whisperer** (dies to falling trees): <@42>", everyone)
        post = community.render_honor_given(community.get_honor(self.c, "hermodr"), 42, "200 iron")
        self.assertEqual(post["title"], "⚰️ A new Hermóðr!")
        self.assertIn("<@42> is honored as **Hermóðr**, crypt raider.", post["description"])
        self.assertTrue(community.take_honor(self.c, "hermodr", 42))
        community.delete_honor(self.c, "treewhisperer")
        self.assertEqual(community.user_honors(self.c, 42), [])


class Member:
    def __init__(self, uid):
        self.id, self.roles, self.display_name = uid, [], f"Viking{uid}"

    async def add_roles(self, role, reason=None):
        self.roles.append(role)


class Guild:
    def __init__(self):
        self.id, self.roles, self.members, self.made = 9, [], {}, {}

    def get_role(self, rid):
        return self.made.get(rid)

    async def create_role(self, name, colour=None, reason=None, hoist=False):
        role = SimpleNamespace(id=700 + len(self.made), name=name, managed=False)
        self.made[role.id] = role
        self.roles.append(role)
        return role

    async def fetch_member(self, uid):
        return self.members.setdefault(int(uid), Member(int(uid)))


@unittest.skipIf(discord is None, "discord.py not installed")
class HonorBotTest(unittest.TestCase):
    def setUp(self):
        import admin_bot
        self.d = tempfile.mkdtemp()
        self.posts = []
        self.b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                     "save_dir": self.d}, "S")
        self.b.attach(db_path=os.path.join(self.d, "s.db"), webhook_url="https://x",
                      post_embed=lambda embed, kind="titles": self.posts.append((kind, embed)) or True)
        self.guild = Guild()
        self.b.client = SimpleNamespace(get_guild=lambda gid: self.guild)

    def test_give_creates_the_role_once_and_posts(self):
        h = community.get_honor(self.b.db, "andhrimnir")
        out = asyncio.run(self.b._give_honor(self.guild, h, 42, "Alain", "best stew in the realm"))
        self.assertIn("<@42> is now **Andhrímnir**", out)
        self.assertEqual([r.name for r in self.guild.members[42].roles], ["Andhrímnir"])
        self.assertEqual(self.posts[0][0], "honor")
        self.assertIn("best stew", self.posts[0][1]["description"])
        asyncio.run(self.b._give_honor(self.guild, community.get_honor(self.b.db, "andhrimnir"), 43, "Alain"))
        self.assertEqual(len(self.guild.made), 1)                               # the same role, reused
        self.assertIn("already", asyncio.run(self.b._give_honor(
            self.guild, community.get_honor(self.b.db, "andhrimnir"), 42, "Alain")))

    def test_vote_winner_gets_it_and_ties_are_left_to_the_admins(self):
        sent = []

        class Channel:
            guild = self.guild

            async def send(self, content=None, embed=None, allowed_mentions=None):
                sent.append(content or embed)

        def poll(*votes):
            return SimpleNamespace(poll=SimpleNamespace(answers=[SimpleNamespace(vote_count=v) for v in votes]))
        rec = {"kind": "honor", "honor": "gangleri", "candidates": ["42", "43", "44"]}
        asyncio.run(self.b._resolve_honor_vote(Channel(), poll(1, 3, 0), rec))
        self.assertIn("<@43> is now **Gangleri**", sent[-1])
        asyncio.run(self.b._resolve_honor_vote(Channel(), poll(2, 2, 0), dict(rec, honor="ullr")))
        self.assertIn("a tie between <@42>, <@43>", sent[-1])
        asyncio.run(self.b._resolve_honor_vote(Channel(), poll(0, 0, 0), dict(rec, honor="eir")))
        self.assertIn("Nobody voted", sent[-1])

    def test_bounty_can_carry_an_honor(self):
        sent, edits = [], []

        class Channel:
            id = 77

            async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
                sent.append(content or embed)
                return SimpleNamespace(id=900, jump_url="u")

        class Partial:
            def __init__(self, cid):
                pass

            def get_partial_message(self, mid):
                async def edit(embed=None, view=None):
                    edits.append(embed)
                return SimpleNamespace(edit=edit)

            async def send(self, *a, **kw):
                return await Channel().send(*a, **kw)
        self.b.client = SimpleNamespace(get_guild=lambda gid: self.guild, get_channel=lambda cid: Channel(),
                                        get_partial_messageable=Partial)
        self.b.bounty_role_name = None
        asyncio.run(self.b._post_bounty(Channel(), "Clear 5 crypts", "", 7, 5, "hermodr"))
        self.assertIn("**Honor:** ⚰️ Hermóðr, for good", sent[0].description)
        notice = SimpleNamespace(embeds=[], channel=Channel(), id=55)
        replies = []

        async def edit_message(embed=None, view=None):
            replies.append(embed)
        it = SimpleNamespace(user=SimpleNamespace(id=5, mention="<@5>"), message=notice,
                             response=SimpleNamespace(edit_message=edit_message))
        asyncio.run(self.b._on_bounty_decision(it, True, "1-42"))
        self.assertEqual([h["name"] for h in community.user_honors(self.b.db, 42)], ["Hermóðr"])
        self.assertIn("honored as **Hermóðr** for good", sent[-1])


if __name__ == "__main__":
    unittest.main()
