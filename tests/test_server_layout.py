import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server_layout as sl  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


def fresh_server():
    """What a new Discord server looks like, plus an admin channel and a channel of the owner's own."""
    return {"categories": [{"id": 1, "name": "Text Channels", "position": 0},
                           {"id": 2, "name": "Voice Channels", "position": 1}],
            "channels": [{"id": 10, "name": "general", "kind": "text", "category_id": 1, "position": 0},
                         {"id": 11, "name": "valheim-admin", "kind": "text", "category_id": 1, "position": 1},
                         {"id": 12, "name": "my-stuff", "kind": "text", "category_id": 1, "position": 2},
                         {"id": 20, "name": "General", "kind": "voice", "category_id": 2, "position": 0},
                         {"id": 21, "name": "Gaming", "kind": "voice", "category_id": 2, "position": 1}]}


class PlanTest(unittest.TestCase):
    def slots(self, p):
        return {c["key"]: c["id"] for c in p["channels"]}

    def test_fresh_server(self):
        p = sl.plan(fresh_server(), known={"admin": 11})
        self.assertEqual(self.slots(p), {"rules": None, "welcome": None, "general": 10, "media": None,
                                         "builds": None, "bots": None, "feed": None, "plans": None,
                                         "lore": None, "vc_main": 20, "vc_raid": 21, "vc_afk": None,
                                         "admin": 11})
        self.assertEqual({c["key"]: c["id"] for c in p["categories"]},
                         {"gates": None, "mead": 1, "wilds": None, "longhouses": 2, "odin": None})
        self.assertNotIn(12, [c["id"] for c in p["channels"]])          # unknown: untouched
        self.assertEqual(sl.changes(p), {"create": 12, "rename": 6, "move": 0, "same": 0})
        text = sl.render(p)
        self.assertIn("# 🍺┃mead-hall  ← was #general", text)
        self.assertIn("🔊 🍺 The Longhouse  ← was General", text)
        self.assertIn("# 👁️┃odins-seat 🔒  ← was #valheim-admin", text)
        self.assertIn("Nothing is deleted", text)

    def test_second_run_changes_nothing(self):
        first = sl.plan(fresh_server(), known={"admin": 11})
        ids = iter(range(100, 200))
        cats = [{"id": c["id"] or next(ids), "name": c["name"], "position": i}
                for i, c in enumerate(first["categories"])]
        cat_id = {c["key"]: cats[i]["id"] for i, c in enumerate(first["categories"])}
        chans = [{"id": c["id"] or next(ids), "name": c["name"] if c["kind"] == "voice" else c["name"].lower(),
                  "kind": c["kind"], "category_id": cat_id[c["category"]], "position": c["position"]}
                 for c in first["channels"]]
        remembered = {f"cat:{c['key']}": cats[i]["id"] for i, c in enumerate(first["categories"])}
        remembered.update({f"ch:{c['key']}": chans[i]["id"] for i, c in enumerate(first["channels"])})
        again = sl.plan({"categories": cats, "channels": chans}, known={"admin": 11}, remembered=remembered)
        n = sl.changes(again)
        self.assertEqual((n["create"], n["rename"], n["move"]), (0, 0, 0))
        # Even renamed by hand afterwards, remembered channels keep their slot.
        chans[0]["name"] = "our-rules"
        self.assertEqual(self.slots(sl.plan({"categories": cats, "channels": chans}, remembered=remembered))
                         ["rules"], chans[0]["id"])

    def test_webhook_channel(self):
        snap = fresh_server()
        snap["channels"].append({"id": 13, "name": "server-log", "kind": "text", "category_id": 1, "position": 3})
        self.assertEqual(self.slots(sl.plan(snap, known={"feed": 13}))["feed"], 13)
        # Huginn posting into the general chat: the chat stays the chat, with a note.
        p = sl.plan(fresh_server(), known={"feed": 10})
        self.assertEqual(self.slots(p)["general"], 10)
        self.assertIsNone(self.slots(p)["feed"])
        self.assertIn("Huginn's webhook posts in #general", sl.render(p))

    def test_stat_channels_are_left_alone(self):
        snap = fresh_server()
        snap["categories"].append({"id": 3, "name": "General", "position": 2})
        p = sl.plan(snap, exclude={3, 20})
        self.assertNotIn(3, [c["id"] for c in p["categories"]])
        self.assertIsNone(self.slots(p)["vc_main"])

    def test_system_messages(self):
        snap = fresh_server()
        self.assertFalse(sl.plan({**snap, "system_channel_id": 10}, known={"admin": 11})["system"]["move"])
        p = sl.plan({**snap, "system_channel_id": 11}, known={"admin": 11})
        self.assertTrue(p["system"]["move"])
        self.assertIn("go to #valheim-admin, which only admins will see; they'll go to #🚪┃the-gates", sl.render(p))
        self.assertIn("go to no channel", sl.render(sl.plan(snap)))

    def test_names_compare_loosely(self):
        self.assertEqual(sl.norm("📜┃Run-estone"), "runestone")
        self.assertEqual(sl.norm("Voice Channels"), "voicechannels")


@unittest.skipIf(discord is None, "discord.py not installed")
class ApplyUndoTest(unittest.TestCase):
    def fake_guild(self):
        T = discord.ChannelType

        class Ch:
            def __init__(self, guild, cid, name, type_, category_id=None, overwrites=None, topic=None):
                self.guild, self.id, self.name, self.type = guild, cid, name, type_
                self.category_id, self.overwrites, self.topic = category_id, overwrites or {}, topic
                self.position = cid

            async def create_webhook(self, name=None, reason=None):
                self.guild.hooks.append((self.id, name))
                return type("Hook", (), {"url": f"https://discord.com/api/webhooks/77/tok-{self.id}"})()

            async def edit(self, name=None, category=False, overwrites=None, topic=False, reason=None):
                if name is not None:
                    self.name = name.lower().replace(" ", "-") if self.type == T.text else name
                if category is not False:
                    self.category_id = category.id if category else None
                if overwrites is not None:
                    self.overwrites = overwrites
                if topic is not False:
                    self.topic = topic

        class Http:
            def __init__(self):
                self.calls = []

            async def bulk_channel_update(self, gid, payload, reason=None):
                self.calls.append(payload)

        class Guild:
            id = 9
            hooks: list
            system_channel_id = 41             # Discord's join messages go to the admin channel

            async def edit(self, system_channel=None, reason=None):
                self.system_channel_id = system_channel.id if system_channel else None

            def __init__(self):
                self.hooks = []
                self.default_role = discord.Object(1, type=discord.Role)
                self.me = discord.Object(2, type=discord.Member)
                self._state = type("S", (), {"http": Http()})()
                self.all = [Ch(self, 30, "Text Channels", T.category), Ch(self, 31, "Voice Channels", T.category),
                            Ch(self, 40, "general", T.text, 30), Ch(self, 41, "valheim-admin", T.text, 30),
                            Ch(self, 50, "General", T.voice, 31)]

            @property
            def categories(self):
                return [c for c in self.all if c.type == T.category]

            @property
            def channels(self):
                return self.all

            def get_channel(self, cid):
                return next((c for c in self.all if c.id == cid), None)

            def get_role(self, rid):
                return None

            async def fetch_member(self, uid):
                return discord.Object(uid, type=discord.Member)

            async def _make(self, name, type_, category=None, overwrites=None, topic=None, reason=None):
                ch = Ch(self, 100 + len(self.all), name.lower().replace(" ", "-") if type_ == T.text else name,
                        type_, category.id if category else None, overwrites, topic)
                self.all.append(ch)
                return ch

            async def create_category(self, name, overwrites=None, reason=None):
                return await self._make(name, T.category, overwrites=overwrites)

            async def create_text_channel(self, name, category=None, overwrites=None, reason=None, topic=None):
                return await self._make(name, T.text, category, overwrites, topic)

            async def create_voice_channel(self, name, category=None, overwrites=None, reason=None):
                return await self._make(name, T.voice, category, overwrites)
        return Guild()

    def test_apply_then_undo(self):
        import admin_bot
        guild = self.fake_guild()
        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "41", "guild_id": "9", "admin_user_ids": [5],
                                      "save_dir": d}, "S")
            given = []
            bot.attach(db_path=os.path.join(d, "s.db"), set_webhook=given.append)     # no webhook yet

            async def run():
                p = await bot._layout_plan(guild)
                problems = await bot._layout_apply(guild, p)
                return p, problems
            p, problems = asyncio.run(run())
            self.assertEqual(problems, [])
            byid = {c.id: c for c in guild.all}
            # No webhook was configured: Huginn's is created in #huginns-watch and handed over.
            feed = next(c for c in guild.all if c.name == "🐦┃huginns-watch")
            self.assertEqual(guild.hooks, [(feed.id, "Huginn")])
            self.assertEqual(given, [f"https://discord.com/api/webhooks/77/tok-{feed.id}"])
            self.assertIn("will be created in #🐦┃huginns-watch", __import__("server_layout").render(p))
            self.assertEqual(byid[40].name, "🍺┃mead-hall")
            self.assertEqual(byid[41].name, "👁️┃odins-seat")
            self.assertEqual(byid[50].name, "🍺 The Longhouse")
            self.assertEqual(byid[30].name, "🍺 The Mead Hall")
            self.assertEqual(byid[40].topic, "General chat. Pull up a bench by the fire.")
            # The admin channel is private: hidden from everyone, visible to the admin and the bot.
            ow = byid[41].overwrites
            self.assertFalse(ow[guild.default_role].view_channel)
            self.assertTrue(any(getattr(k, "id", None) == 5 and v.view_channel for k, v in ow.items()))
            # The read-only rules channel was created with send_messages off for everyone.
            rules = next(c for c in guild.all if c.name == "📜┃runestone")
            self.assertFalse(rules.overwrites[guild.default_role].send_messages)
            self.assertTrue(guild._state.http.calls)             # categories and channels ordered
            undo = json.loads(bot._meta("layout:undo"))
            self.assertEqual(set(undo["before"]), {"30", "31", "40", "41", "50"})
            # The admin channel became private, so Discord's join messages moved to the welcome channel.
            welcome = next(c for c in guild.all if c.name == "🚪┃the-gates")
            self.assertEqual(guild.system_channel_id, welcome.id)
            self.assertIn("Discord's join messages", __import__("server_layout").render(p))

            restored, created, problems = asyncio.run(bot._layout_undo(guild))
            self.assertEqual(problems, [])
            self.assertEqual(restored, 5)
            self.assertEqual([byid[i].name for i in (30, 31, 40, 41, 50)],
                             ["Text Channels", "Voice Channels", "general", "valheim-admin", "General"])
            self.assertEqual(byid[40].category_id, 30)
            self.assertEqual(byid[41].overwrites, {})             # permissions back as they were
            self.assertEqual(guild.system_channel_id, 41)
            self.assertGreaterEqual(len(created), 10)             # listed, not deleted


    def test_webhook_in_the_admin_channel_moves_and_undo_moves_it_back(self):
        import admin_bot
        guild = self.fake_guild()

        class Hook:
            channel_id = 41                        # made in the admin channel by mistake

            async def edit(self, channel=None, reason=None):
                self.channel_id = channel.id
        hook = Hook()
        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "41", "guild_id": "9", "admin_user_ids": [5],
                                      "save_dir": d}, "S")
            bot.attach(db_path=os.path.join(d, "s.db"), webhook_url="https://discord.com/api/webhooks/555/abc")
            bot._feed_channel_id = lambda: 41

            async def fetch_webhook(wid):
                self.assertEqual(wid, 555)
                return hook
            bot.client = type("C", (), {"fetch_webhook": staticmethod(fetch_webhook)})()

            async def run():
                p = await bot._layout_plan(guild)
                return p, await bot._layout_apply(guild, p)
            p, problems = asyncio.run(run())
            self.assertEqual(problems, [])
            byid = {c.id: c for c in guild.all}
            self.assertEqual(byid[41].name, "👁️┃odins-seat")            # the admin channel keeps its job
            feed = next(c for c in guild.all if c.name == "🐦┃huginns-watch")
            self.assertEqual(hook.channel_id, feed.id)                    # and Huginn moved out of it
            self.assertEqual(guild.hooks, [])                             # nothing new created
            self.assertIn("it'll be moved to #🐦┃huginns-watch", __import__("server_layout").render(p))
            asyncio.run(bot._layout_undo(guild))
            self.assertEqual(hook.channel_id, 41)


    def test_startup_warns_when_the_webhook_posts_into_the_admin_channel(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "41", "guild_id": "9", "admin_user_ids": [5],
                                      "save_dir": d}, "S")
            told = []
            bot.post_admin = told.append
            for where, warned in ((41, True), (42, False), (None, False)):
                told.clear()
                bot._feed_channel_id = lambda where=where: where
                asyncio.run(bot._check_webhook())
                self.assertEqual(bool(told), warned, where)
        self.assertEqual(told, [])


if __name__ == "__main__":
    unittest.main()
