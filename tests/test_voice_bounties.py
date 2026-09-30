import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import stats_db  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


class BountyDBTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def test_lifecycle(self):
        bid = community.create_bounty(self.c, "Kill Moder without dying", "10 black metal", 7, 5, now=1000)
        b = community.get_bounty(self.c, bid)
        self.assertEqual((b["status"], b["expires_at"]), ("open", 1000 + 7 * 86400))
        e = community.render_bounty(b)
        self.assertEqual(e["title"], "🎯 Bounty: Kill Moder without dying")
        self.assertIn("**Reward:** 10 black metal", e["description"])
        self.assertEqual([x["id"] for x in community.open_bounties(self.c, "moder")], [bid])
        self.assertTrue(community.finish_bounty(self.c, bid, "done", 42, now=2000))
        self.assertFalse(community.finish_bounty(self.c, bid, "done", 43))       # only once
        self.assertIn("Claimed by <@42>", community.render_bounty(community.get_bounty(self.c, bid))["description"])
        self.assertEqual(community.bounty_hunters(self.c), [{"user_id": "42", "n": 1}])

    def test_expiry(self):
        bid = community.create_bounty(self.c, "Tame a lox", "", 1, 5, now=1000)
        self.assertEqual(community.expired_bounties(self.c, now=1000 + 3600), [])
        self.assertEqual([b["id"] for b in community.expired_bounties(self.c, now=1000 + 86400)], [bid])


class FakeChannel:
    def __init__(self, cid, name="", category=None, members=None):
        self.id, self.name, self.category = cid, name, category
        self.members = members if members is not None else []
        self.sent, self.deleted = [], False

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
        self.sent.append(SimpleNamespace(content=content, embed=embed, view=view))
        return SimpleNamespace(id=900 + len(self.sent), jump_url="https://discord.com/x")

    async def delete(self, reason=None):
        self.deleted = True


class FakeGuild:
    def __init__(self):
        self.id = 9
        self.voice_channels, self.created = [], []
        self.roles, self.members = {}, {}

    def get_channel(self, cid):
        return next((c for c in self.voice_channels + self.created if c.id == cid and not c.deleted), None)

    async def create_voice_channel(self, name, category=None, overwrites=None, user_limit=None, reason=None):
        ch = FakeChannel(500 + len(self.created), name, category)
        ch.overwrites, ch.user_limit = overwrites, user_limit
        self.created.append(ch)
        return ch

    def get_role(self, rid):
        return self.roles.get(rid)

    async def create_role(self, name, colour=None, hoist=False, reason=None):
        role = SimpleNamespace(id=700 + len(self.roles), name=name, managed=False)
        self.roles[role.id] = role
        return role

    async def fetch_member(self, uid):
        return self.members.setdefault(uid, FakeMember(uid, self))


class FakeMember:
    def __init__(self, uid, guild, name="Ingrid"):
        self.id, self.guild, self.display_name, self.bot = uid, guild, name, False
        self.roles, self.moved_to = [], None

    async def move_to(self, ch, reason=None):
        self.moved_to = ch
        ch.members.append(self)

    async def add_roles(self, role, reason=None):
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


@unittest.skipIf(discord is None, "discord.py not installed")
class VoiceLobbyTest(unittest.TestCase):
    def bot(self, d, voice=True):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                "save_dir": d, "voice_lobby": voice}, "S")
        b.attach(db_path=os.path.join(d, "s.db"))
        return b

    def test_config_and_intents(self):
        with tempfile.TemporaryDirectory() as d:
            off = self.bot(d, voice=None)
            self.assertFalse(off.voice_lobby)
            self.assertFalse(off._build_client().intents.voice_states)
            on = self.bot(d, voice={"template": "🛶 {name}'s boat", "limit": 4})
            self.assertTrue(on._build_client().intents.voice_states)
            self.assertEqual(on.voice_name("Ingrid"), "🛶 Ingrid's boat")
            self.assertEqual(on.voice_limit, 4)

    def test_join_creates_and_leaving_deletes(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            guild = FakeGuild()
            b.client = SimpleNamespace(get_guild=lambda gid: guild)
            with mock.patch("asyncio.sleep", mock.AsyncMock()):
                asyncio.run(b._voice_setup())
            lobby = guild.created[0]
            self.assertEqual(lobby.name, "➕ Raise a longship")
            ingrid = FakeMember(42, guild)
            none = SimpleNamespace(channel=None)
            asyncio.run(b._on_voice(ingrid, none, SimpleNamespace(channel=lobby)))
            room = ingrid.moved_to
            self.assertEqual(room.name, "⛵ Ingrid's longship")
            self.assertIn(ingrid, room.overwrites)                 # its maker can manage it
            self.assertEqual(json.loads(b._meta("voice:temp")), [room.id])
            # Someone else leaving a normal channel: nothing happens.
            asyncio.run(b._on_voice(FakeMember(43, guild), SimpleNamespace(channel=lobby), none))
            self.assertFalse(lobby.deleted)
            # The last one out: the channel goes.
            room.members.clear()
            asyncio.run(b._on_voice(ingrid, SimpleNamespace(channel=room), none))
            self.assertTrue(room.deleted)
            self.assertEqual(json.loads(b._meta("voice:temp")), [])

    def test_restart_finds_the_lobby_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            guild = FakeGuild()
            lobby, empty, busy = (FakeChannel(1, "➕ Raise a longship"), FakeChannel(2, "⛵ A's longship"),
                                  FakeChannel(3, "⛵ B's longship", members=["x"]))
            guild.voice_channels = [lobby, empty, busy]
            b._meta("voice:temp", json.dumps([2, 3, 4]))
            b.client = SimpleNamespace(get_guild=lambda gid: guild)
            with mock.patch("asyncio.sleep", mock.AsyncMock()):
                asyncio.run(b._voice_setup())
            self.assertEqual(guild.created, [])                    # found by name, not made again
            self.assertEqual(b._voice_lobby_id, 1)
            self.assertTrue(empty.deleted)
            self.assertFalse(busy.deleted)
            self.assertEqual(json.loads(b._meta("voice:temp")), [3])


@unittest.skipIf(discord is None, "discord.py not installed")
class BountyBotTest(unittest.TestCase):
    def setUp(self):
        import admin_bot
        self.d = tempfile.mkdtemp()
        self.b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                     "save_dir": self.d, "bounties": {"channel_id": "77", "role_days": 7}}, "S")
        self.b.attach(db_path=os.path.join(self.d, "s.db"))
        self.admin, self.hunt, self.guild = FakeChannel(1), FakeChannel(77), FakeGuild()
        chans = {1: self.admin, 77: self.hunt}
        edits = self.edits = []

        class Partial:
            def __init__(self, cid):
                self.cid = cid

            def get_partial_message(self, mid):
                async def edit(embed=None, view=None):
                    edits.append((mid, embed, view))
                return SimpleNamespace(edit=edit)

            async def send(self, *a, **kw):
                return await chans[self.cid].send(*a, **kw)
        self.b.client = SimpleNamespace(get_channel=chans.get, get_guild=lambda gid: self.guild,
                                        get_partial_messageable=Partial)
        self.dms = []

        async def dm(uid, text):
            self.dms.append((str(uid), text))
            return True
        self.b._dm = dm

    def interaction(self, user_id, message=None):
        replies = []

        async def send_message(text=None, ephemeral=False, **kw):
            replies.append(text)

        async def edit_message(embed=None, view=None):
            replies.append(("edit", embed, view))
        user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        return SimpleNamespace(user=user, message=message,
                               response=SimpleNamespace(send_message=send_message, edit_message=edit_message)), replies

    def test_claim_confirm_and_role(self):
        b = self.b
        target = asyncio.run(b._bounty_target())
        self.assertIs(target, self.hunt)
        asyncio.run(b._post_bounty(self.hunt, "Kill Moder without dying", "10 black metal", 7, 5))
        post = self.hunt.sent[0]
        self.assertEqual(post.view.children[0].custom_id, "vdm:bclaim:1")
        # A member claims it: the admins get Confirm / Reject; pressing twice doesn't repeat.
        it, replies = self.interaction(42)
        asyncio.run(b._on_bounty_claim(it, "1"))
        asyncio.run(b._on_bounty_claim(it, "1"))
        self.assertEqual(len(self.admin.sent), 1)
        self.assertIn("already told", replies[1])
        notice = self.admin.sent[0]
        self.assertEqual([c.custom_id for c in notice.view.children], ["vdm:bconfirm:1-42", "vdm:breject:1-42"])
        # An admin confirms: bounty closed, post updated, role given, announced.
        msg = SimpleNamespace(embeds=[notice.embed], channel=self.admin, id=55)
        it, replies = self.interaction(5, msg)
        asyncio.run(b._on_bounty_decision(it, True, "1-42"))
        self.assertEqual(community.get_bounty(b.db, 1)["status"], "done")
        self.assertTrue(self.edits[-1][2].children[0].disabled)
        self.assertEqual([r.name for r in self.guild.members[42].roles], ["Skadi"])
        self.assertIn("🏆 <@42> claimed the bounty **Kill Moder without dying**", self.hunt.sent[-1].content)
        self.assertIn("**Skadi** for 7 days", self.hunt.sent[-1].content)
        # A second confirmation (another claimant) finds it closed.
        it, replies = self.interaction(5, msg)
        asyncio.run(b._on_bounty_decision(it, True, "1-43"))
        self.assertEqual(replies, ["That bounty was already closed."])
        # The role runs out after a week.
        holders = json.loads(b._meta("bounty:holders"))
        b._meta("bounty:holders", json.dumps({k: time.time() - 1 for k in holders}))
        asyncio.run(b._bounty_tick())
        self.assertEqual(self.guild.members[42].roles, [])

    def test_reject_tells_them(self):
        b = self.b
        asyncio.run(b._post_bounty(self.hunt, "Tame a lox", "", 7, 5))
        it, _ = self.interaction(42)
        asyncio.run(b._on_bounty_claim(it, "1"))
        msg = SimpleNamespace(embeds=[self.admin.sent[0].embed], channel=self.admin, id=55)
        it, _ = self.interaction(5, msg)
        asyncio.run(b._on_bounty_decision(it, False, "1-42"))
        self.assertEqual(community.get_bounty(b.db, 1)["status"], "open")
        self.assertIn("couldn't confirm", self.dms[0][1])

    def test_expired_bounty_is_closed(self):
        b = self.b
        asyncio.run(b._post_bounty(self.hunt, "Tame a lox", "", 1, 5))
        b.db.execute("UPDATE bounties SET expires_at = 0")
        asyncio.run(b._bounty_tick())
        self.assertEqual(community.get_bounty(b.db, 1)["status"], "expired")
        self.assertIn("Nobody claimed it", self.edits[-1][1].description)


if __name__ == "__main__":
    unittest.main()
