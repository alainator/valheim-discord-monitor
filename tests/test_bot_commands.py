import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

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
                                           "update-check", "restart", "restart-cancel", "lists", "join",
                                           "settings", "modifier", "preset", "setkey",
                                           "stats", "top", "notify", "link", "unlink", "request-access",
                                           "plan", "map", "titles"]))

    def test_database_tasks_start_after_attach(self):
        """start() waits for on_ready, and attach() hands over the database only after that,
        so the plan and title loops must start from attach(), not just from on_ready."""
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                      "save_dir": d, "titles": {"enabled": True}}, "S")

            async def idle():
                await asyncio.sleep(3600)
            bot._plan_loop = bot._titles_loop = idle

            async def run():
                bot.loop = asyncio.get_running_loop()
                bot._start_tasks()                       # on_ready: no database yet
                before = (bot._plan_task, bot._titles_task)
                bot.ready.set()
                bot.attach(db_path=os.path.join(d, "s.db"))
                await asyncio.sleep(0)                   # let call_soon_threadsafe run
                after = (bot._plan_task, bot._titles_task)
                for t in after:
                    t.cancel()
                return before, after
            with mock.patch.object(admin_bot.log, "warning") as warn:
                before, after = asyncio.run(run())
        warn.assert_not_called()
        self.assertEqual(before, (None, None))
        self.assertTrue(all(after))

    def test_stat_channels_are_created_once_and_locked(self):
        import admin_bot
        import extras

        class Channel:
            def __init__(self, cid, name, overwrites, category=None):
                self.id, self.name, self.overwrites, self.category = cid, name, overwrites, category

            async def edit(self, name=None, reason=None):
                self.name = name

        class Guild:
            def __init__(self):
                self.default_role, self.me = "everyone", "bot"
                self.categories, self.voice = [], []

            def get_channel(self, cid):
                return next((c for c in self.categories + self.voice if c.id == cid), None)

            async def create_category(self, name, overwrites=None, position=None, reason=None):
                self.categories.append(Channel(500 + len(self.categories), name, overwrites))
                return self.categories[-1]

            async def create_voice_channel(self, name, category=None, overwrites=None, position=None, reason=None):
                self.voice.append(Channel(600 + len(self.voice), name, overwrites, category))
                return self.voice[-1]

        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                      "save_dir": d, "stat_channels": {"enabled": True,
                                                                        "show": ["join_code", "deaths_week"]}}, "S")
            bot.attach(db_path=os.path.join(d, "s.db"))
            live = extras.LiveState()
            live.join_code, live.count = "482913", 1
            bot.live = live
            guild = Guild()
            bot.client = type("C", (), {"get_guild": lambda self, gid: guild})()
            bot.stats_show = ["join_code", "deaths_week"]
            first = asyncio.run(bot._ensure_stat_channels())
            again = asyncio.run(bot._ensure_stat_channels())
        self.assertEqual([c.name for c in guild.categories], ["📊 Valheim"])
        self.assertEqual([c.name for c in guild.voice], ["🔑 Join code: 482913", "💀 Deaths this week: 0"])
        self.assertEqual({k: c.id for k, c in first.items()}, {k: c.id for k, c in again.items()})
        locked = guild.voice[0].overwrites["everyone"]
        self.assertFalse(locked.connect)
        self.assertTrue(locked.view_channel)
        self.assertTrue(guild.voice[0].overwrites["bot"].manage_channels)

    def test_title_roles_move_with_the_leaders(self):
        import datetime as dt
        import admin_bot
        import community
        import stats_db

        class Member:
            def __init__(self, uid, log):
                self.id, self.log = uid, log

            async def add_roles(self, role, reason=None):
                self.log.append(("add", self.id, role.name))

            async def remove_roles(self, role, reason=None):
                self.log.append(("remove", self.id, role.name))

        class Role:
            def __init__(self, rid, name):
                self.id, self.name, self.position = rid, name, 1

            async def edit(self, position=None, name=None, colour=None, reason=None):
                self.position = position if position is not None else self.position
                self.name = name or self.name

        class Guild:
            def __init__(self):
                self.roles, self.log, self.owner_id = [], [], 42
                bot_role = Role(1, "Odin")                  # the bot is called Odin too:
                bot_role.position, bot_role.managed = 10, True   # its own role must not be reused
                self.roles.append(bot_role)
                self.me = type("Me", (), {"top_role": bot_role})()

            def get_role(self, rid):
                return next((r for r in self.roles if r.id == rid), None)

            async def create_role(self, name, colour=None, hoist=False, reason=None):
                self.roles.append(Role(len(self.roles) + 100, name))
                self.roles[-1].hoist = hoist
                return self.roles[-1]

            async def fetch_member(self, uid):
                return Member(uid, self.log)

        with tempfile.TemporaryDirectory() as d:
            st = stats_db.Store(os.path.join(d, "s.db"))
            st.login("Ingrid", 1000)
            st.logout("Ingrid", 8200)
            st.death("Bjorn", 1100)
            st.login("Bjorn", 2000)
            st.logout("Bjorn", 2100)
            community.link_player(st.conn, "Ingrid", 42)
            st.close()
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                      "save_dir": d, "titles": {"enabled": True}}, "S")
            bot.attach(db_path=os.path.join(d, "s.db"))
            guild = Guild()

            class Client:
                def get_guild(self, gid):
                    return guild
            bot.client = Client()

            async def run():
                first = await bot._sync_titles(recompute=True)
                community.link_player(bot.db, "Bjorn", 77)        # Bjorn links later
                await bot._sync_titles(recompute=False)
                return first
            holders, changed = asyncio.run(run())
        self.assertEqual(changed, {"time", "deaths", "sessions", "longest"})
        self.assertEqual(holders["time"], {"player": "Ingrid", "user_id": "42", "v": 7200})
        self.assertEqual(holders["deaths"]["user_id"], None)                 # not linked yet
        self.assertEqual(sorted(r.name for r in guild.roles), ["Heimdall", "Hel", "Odin", "Sleipnir", "Thor"])
        self.assertIn(("add", 42, "Heimdall"), guild.log)
        self.assertIn(("add", 77, "Hel"), guild.log)                          # given after linking
        self.assertIn(("add", 77, "Sleipnir"), guild.log)                       # tied on visits: first by name
        # The "In Valheim" role with "online_role": true: created once (shown separately), then reused.
        bot.online_role_name = "In Valheim"
        asyncio.run(bot._set_role("Ingrid", True))
        asyncio.run(bot._set_role("Ingrid", False))
        online = [r for r in guild.roles if r.name == "In Valheim"]
        self.assertEqual(len(online), 1)
        self.assertTrue(online[0].hoist)
        self.assertIn(("add", 42, "In Valheim"), guild.log)
        self.assertIn(("remove", 42, "In Valheim"), guild.log)
        # A title role made under its old name (Huginn) is renamed, not duplicated.
        sleipnir = next(r for r in guild.roles if r.name == "Sleipnir")
        sleipnir.name = "Huginn"
        asyncio.run(bot._rename_old_titles())
        self.assertEqual(sleipnir.name, "Sleipnir")
        self.assertEqual(sum(r.name == "Sleipnir" for r in guild.roles), 1)
        # Owner role: created once, moved up under the bot's role, and follows a change of owner.
        bot.owner_role_name = "Odin"
        asyncio.run(bot._sync_owner_role())
        asyncio.run(bot._sync_owner_role())                                  # no change: no new role
        odin = [r for r in guild.roles if r.name == "Odin" and not getattr(r, "managed", False)]
        self.assertEqual(len(odin), 1)
        self.assertEqual(odin[0].position, 9)
        self.assertEqual(guild.log.count(("add", 42, "Odin")), 1)
        guild.owner_id = 77
        asyncio.run(bot._sync_owner_role())
        self.assertIn(("remove", 42, "Odin"), guild.log)
        self.assertIn(("add", 77, "Odin"), guild.log)
        # Weekly schedule: first run right away, then only on the configured day and hour.
        self.assertIsNotNone(bot._titles_due())
        community.set_meta(bot.db, "titles_week", "2026-W39")
        sunday = dt.datetime(2026, 10, 4, 18, 5)
        self.assertEqual(bot._titles_due(sunday), "2026-W40")
        self.assertIsNone(bot._titles_due(sunday.replace(hour=17)))
        community.set_meta(bot.db, "titles_week", "2026-W40")
        self.assertIsNone(bot._titles_due(sunday))


if __name__ == "__main__":
    unittest.main()
