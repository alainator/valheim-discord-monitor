import asyncio
import json
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
                out = {}
                for group in client.tree.get_commands():
                    # to_dict() resolves every parameter type, like the sync to Discord does.
                    payload = group.to_dict(client.tree)
                    out[group.name] = (sorted(c["name"] for c in payload["options"]),
                                       payload.get("default_member_permissions"))
                return out
            groups = asyncio.run(build())
        self.assertEqual({k: v[0] for k, v in groups.items()}, {
            "valheim": sorted(["join", "map", "link", "unlink", "notify", "request-access", "progress",
                              "patch-notes", "wiki"]),
            "muninn": sorted(["stats", "top", "titles", "online", "compare", "uptime", "bosses", "honors",
                              "progress"]),
            "warcouncil": ["bounties", "plan"],
            "odin": sorted(["permit", "ban", "unban", "unpermit", "lists", "settings", "modifier", "preset",
                            "setkey", "backups", "update-check", "restart", "restart-cancel", "setup",
                            "announce", "bounty", "bounty-close", "restore", "rules", "honor"]),
        })
        # /odin is hidden from members without Manage Server; the others are for everyone.
        self.assertEqual(int(groups["odin"][1]), 1 << 5)
        self.assertIsNone(groups["muninn"][1])

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

    def stat_guild(self):
        class Channel:
            def __init__(self, cid, name, overwrites, category=None):
                self.id, self.name, self.overwrites, self.category = cid, name, overwrites, category

            async def edit(self, name=None, category=None, overwrites=None, position=None, reason=None):
                self.name = name or self.name
                self.category = category or self.category
                self.overwrites = overwrites or self.overwrites

        class Guild:
            owner_id = 7

            def __init__(self):
                self.default_role, self.me = "everyone", "bot"
                self.categories, self.voice = [], []

            def get_channel(self, cid):
                return next((c for c in self.categories + self.voice if c.id == cid), None)

            async def fetch_member(self, uid):
                return type("M", (), {"display_name": "Alain"})()

            async def create_category(self, name, overwrites=None, position=None, reason=None):
                self.categories.append(Channel(500 + len(self.categories), name, overwrites))
                return self.categories[-1]

            async def create_voice_channel(self, name, category=None, overwrites=None, position=None, reason=None):
                self.voice.append(Channel(600 + len(self.voice), name, overwrites, category))
                return self.voice[-1]
        return Guild, Channel

    def stat_bot(self, d, guild, **stat_cfg):
        import admin_bot
        import extras
        cfg = {"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5], "save_dir": d,
               "stat_channels": {"enabled": True, **stat_cfg}, "owner_role": True}
        bot = admin_bot.AdminBot(cfg, "S")
        bot.attach(db_path=os.path.join(d, "s.db"))
        live = extras.LiveState()
        live.join_code, live.count, live.online = "482913", 1, {"Ingrid": 1.0}
        bot.live = live
        bot.client = type("C", (), {"get_guild": lambda self, gid: guild})()
        return bot

    def test_stat_channels_are_grouped_created_once_and_locked(self):
        Guild, _ = self.stat_guild()
        guild = Guild()
        with tempfile.TemporaryDirectory() as d:
            bot = self.stat_bot(d, guild, show=["join_code", "deaths_week", "title_owner"])
            bot.stats_show = ["join_code", "deaths_week", "title_owner"]
            first = asyncio.run(bot._ensure_stat_channels())
            again = asyncio.run(bot._ensure_stat_channels())
        self.assertEqual([c.name for c in guild.categories],
                         ["🛡️ Heimdall's Watch · live", "📜 The Saga · this week", "👑 Hall of Champions · titles"])
        watch, saga, hall = guild.categories
        self.assertEqual([(c.name, c.category) for c in guild.voice],
                         [("🔑 Join code: 482913", watch), ("💀 Deaths this week: 0", saga),
                          ("👁️ Odin (server owner): Alain", hall)])
        self.assertEqual({k: c.id for k, c in first.items()}, {k: c.id for k, c in again.items()})
        locked = guild.voice[0].overwrites["everyone"]
        self.assertFalse(locked.connect)
        self.assertTrue(locked.view_channel)
        self.assertTrue(guild.voice[0].overwrites["bot"].manage_channels)

    def test_single_layout(self):
        Guild, _ = self.stat_guild()
        guild = Guild()
        with tempfile.TemporaryDirectory() as d:
            bot = self.stat_bot(d, guild, layout="single", category="Valheim stats")
            bot.stats_show = ["join_code", "deaths_week", "title_owner"]
            asyncio.run(bot._ensure_stat_channels())
        self.assertEqual([c.name for c in guild.categories], ["Valheim stats"])
        self.assertEqual({c.category.name for c in guild.voice}, {"Valheim stats"})

    def test_taking_over_the_status_channel_and_old_category(self):
        Guild, Channel = self.stat_guild()
        guild = Guild()
        mine = Channel(42, "🟢 Valheim: 2 online", {"everyone": "open"})     # a status_channel made by hand
        old_cat = Channel(41, "📊 Valheim", {})                              # the category from before
        guild.voice.append(mine)
        guild.categories.append(old_cat)
        with tempfile.TemporaryDirectory() as d:
            import admin_bot
            cfg = {"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5], "save_dir": d,
                   "status_channel": {"channel_id": "42"},
                   "stat_channels": {"enabled": True, "show": ["players", "server"]}}
            bot = admin_bot.AdminBot(cfg, "S")
            self.assertTrue(bot.stats_take_status)             # so the old status loop doesn't start
            bot.attach(db_path=os.path.join(d, "s.db"))
            bot.live = __import__("extras").LiveState()
            bot.client = type("C", (), {"get_guild": lambda self, gid: guild})()
            bot.stats_show = ["players", "server"]
            bot._meta("statchan:category", 41)
            chans = asyncio.run(bot._ensure_stat_channels())
        self.assertIs(chans["players"], mine)                  # reused, not duplicated
        self.assertIs(mine.category, old_cat)                  # moved into the category
        self.assertFalse(mine.overwrites["everyone"].connect)  # and locked
        self.assertEqual(old_cat.name, "🛡️ Heimdall's Watch · live")
        self.assertEqual(len(guild.categories), 1)             # "server" is in the same group

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
        # Hœnir too: Ingrid's 2 h is the least of those with 10+ minutes (Bjorn has 100 s).
        self.assertEqual(changed, {"time", "deaths", "sessions", "longest", "least"})
        self.assertEqual(holders["time"], {"player": "Ingrid", "user_id": "42", "v": 7200})
        self.assertEqual(holders["deaths"]["user_id"], None)                 # not linked yet
        self.assertEqual(sorted(r.name for r in guild.roles),
                         ["Heimdall", "Hel", "Hœnir", "Odin", "Sleipnir", "Thor"])
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
        # The role follows who's online: a holder who isn't in the game any more loses it.
        live = __import__("extras").LiveState()
        live.count, live.online = 1, {"Ingrid": 1.0}
        bot.live = live
        guild.log.clear()
        bot._meta("online_role:holders", json.dumps(["42", "77"]))       # 77 (Bjorn) left unnoticed
        asyncio.run(bot._sync_online_role())
        self.assertEqual(guild.log, [("remove", 77, "In Valheim")])
        self.assertEqual(json.loads(bot._meta("online_role:holders")), ["42"])
        # First run with this version (no list yet): every linked player is checked.
        bot.db.execute("DELETE FROM meta WHERE key = 'online_role:holders'")
        guild.log.clear()
        asyncio.run(bot._sync_online_role())
        self.assertEqual(guild.log, [("remove", 77, "In Valheim")])
        # Empty server: everyone loses it.
        live.online, live.count = {}, 0
        asyncio.run(bot._sync_online_role())
        self.assertIn(("remove", 42, "In Valheim"), guild.log)
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
