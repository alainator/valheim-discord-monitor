import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import stats_db  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

DAY = 86400


class DB(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def play(self, player, start, minutes):
        self.st.login(player, start)
        self.st.logout(player, start + minutes * 60)


class LeastTest(DB):
    def test_honir_goes_to_the_least_played_active_regular(self):
        now = 100 * DAY
        self.play("Ingrid", now - DAY, 600)
        self.play("Ingrid", now - 2 * DAY, 600)
        self.play("Bjorn", now - DAY, 30)
        self.play("Bjorn", now - 3 * DAY, 30)            # least, and a regular: Hœnir
        self.play("Newbie", now - DAY, 5)                 # only once: not yet
        self.play("Gone", now - 60 * DAY, 1)
        self.play("Gone", now - 61 * DAY, 1)              # not seen for 2 months: not counted
        self.assertEqual(community.title_leaders(self.c)["least"]["player"], "Bjorn")
        self.assertEqual(community.title_value(self.c, "least", "Bjorn"), 3600)
        e = community.render_titles({"least": {"player": "Bjorn", "v": 3600, "user_id": None}})
        self.assertIn("**Hœnir**: **Bjorn**", e["description"])

    def test_away_users(self):
        now = 100 * DAY
        self.play("Ingrid", now - 20 * DAY, 60)
        self.play("Ingrid2", now - DAY, 60)               # her other character played lately
        self.play("Bjorn", now - 20 * DAY, 60)
        self.play("Unlinked", now - 40 * DAY, 60)
        community.link_player(self.c, "Ingrid", 1)
        community.link_player(self.c, "Ingrid2", 1)
        community.link_player(self.c, "Bjorn", 2)
        self.assertEqual(set(community.away_users(self.c, now - 14 * DAY)), {"2"})
        e = community.render_titles({}, away=["2"], away_days=14)
        self.assertIn("**Óðr**: <@2>", e["description"])


class Member:
    def __init__(self, uid):
        self.id, self.roles = uid, []

    async def add_roles(self, role, reason=None):
        self.roles.append(role)

    async def remove_roles(self, role, reason=None):
        self.roles.remove(role)


@unittest.skipIf(discord is None, "discord.py not installed")
class AwayRoleTest(DB):
    def test_given_hourly_and_taken_back_at_login(self):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "guild_id": "9", "admin_user_ids": [5],
                                "save_dir": self.d, "titles": {"enabled": True, "away_dm": True}}, "Alheim")
        b.attach(db_path=os.path.join(self.d, "s.db"))
        now = int(time.time())
        self.play("Bjorn", now - 20 * DAY, 60)
        self.play("Ingrid", now - DAY, 60)
        community.link_player(b.db, "Bjorn", 2)
        community.link_player(b.db, "Ingrid", 1)
        roles, members = {}, {}

        async def create_role(name, colour=None, hoist=False, reason=None):
            roles[700] = SimpleNamespace(id=700, name=name, managed=False)
            return roles[700]

        async def fetch_member(uid):
            return members.setdefault(uid, Member(uid))
        guild = SimpleNamespace(id=9, roles=[], get_role=roles.get, create_role=create_role, fetch_member=fetch_member)
        b.client = SimpleNamespace(get_guild=lambda gid: guild)
        dms = []

        async def dm(uid, text):
            dms.append((uid, text))
            return True
        b._dm = dm
        given, taken = asyncio.run(b._sync_away())
        self.assertEqual((given, taken), ({"2"}, set()))
        self.assertEqual([r.name for r in members[2].roles], ["Óðr"])
        self.assertIn("The longships miss you", dms[0][1])
        self.assertEqual(asyncio.run(b._sync_away()), (set(), set()))     # nothing new: no second DM
        self.assertEqual(len(dms), 1)
        asyncio.run(b._back_from_away("Bjorn"))                            # logs in again
        self.assertEqual(members[2].roles, [])
        self.assertEqual(json.loads(b._meta("away:holders")), [])


if __name__ == "__main__":
    unittest.main()
