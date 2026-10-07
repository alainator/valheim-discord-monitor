"""Regression tests for the review of the features added after the first audit: each test
is one bug that was found, so it can't come back."""
import asyncio
import datetime as dt
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import community  # noqa: E402
import stats_db  # noqa: E402
import wiki  # noqa: E402
import world_objects as wo  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


class WikiCacheTest(unittest.TestCase):
    def setUp(self):
        wiki._cache.clear()

    def test_a_wrongly_cased_title_doesnt_hide_the_page(self):
        """Wiki titles are case-sensitive after the first letter: "Deer Trophy" isn't a page,
        "Deer trophy" is. A miss mustn't be cached under a key the right title shares."""
        def fake(params, timeout=10.0):
            if params["action"] == "opensearch":
                return [params["search"], ["Deer trophy"], [], []]
            if params["action"] == "parse":
                if params["page"] != "Deer trophy":
                    return {"error": {"code": "missingtitle"}}
                return {"parse": {"title": "Deer trophy", "wikitext": {"*": "'''Deer trophy''' is a trophy."}}}
            return {"query": {"pages": {"1": {"fullurl": "u"}}}}
        with mock.patch.object(wiki, "_get", fake):
            self.assertEqual(wiki.lookup("Deer Trophy")["title"], "Deer trophy")
            self.assertEqual(wiki.lookup("Deer trophy")["title"], "Deer trophy")

    def test_lookup_and_autocomplete_dont_share_a_search(self):
        def fake(params, timeout=10.0):
            return [params["search"], [f"T{i}" for i in range(params["limit"])], [], []]
        with mock.patch.object(wiki, "_get", fake):
            self.assertEqual(len(wiki.search("trol", 1)), 1)
            self.assertEqual(len(wiki.search("trol")), 10)


class ProgressBoardTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def test_a_list_added_in_an_update_isnt_announced(self):
        community.save_progress(self.c, "Ingrid", 1, {"fishing": [3, 12]})
        _, finished = community.save_progress(self.c, "Ingrid", 1, {"fishing": [3, 12], "trophies": [9, 9]})
        self.assertEqual(finished, [])

    def test_take_over_and_force_drop(self):
        community.save_progress(self.c, "Ingrid", 1, {"fishing": [3, 12]})
        err, _ = community.save_progress(self.c, "Ingrid", 2, {"fishing": [4, 12]})
        self.assertIn("link it with `/valheim link`", err)
        self.assertEqual(community.save_progress(self.c, "Ingrid", 2, {"fishing": [4, 12]}, force=True)[0], None)
        self.assertEqual(community.progress_entry(self.c, "Ingrid")["user_id"], "2")
        self.assertFalse(community.drop_progress(self.c, "Ingrid", 3))
        self.assertTrue(community.drop_progress(self.c, "Ingrid", 3, force=True))

    @unittest.skipIf(discord is None, "discord.py not installed")
    def test_a_renamed_file_cant_stand_in_for_a_linked_character(self):
        import admin_bot
        import fch_progress
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": self.d}, "S")
        b.attach(db_path=os.path.join(self.d, "s.db"))
        b.db.execute("INSERT INTO play_sessions(player, login_at, last_seen_at) VALUES ('Ingrid', 1, 1)")
        community.link_player(b.db, "Ingrid", 111)
        sections = [{"key": "fishing", "done": {"a": 1}, "total": 12}]

        def upload(user, board, admin=False):
            with mock.patch.object(fch_progress, "report", lambda parsed, wanted=None: sections):
                return asyncio.run(b._progress_board(user, "ingrid", {}, board, admin=admin))
        self.assertIn("linked to <@111>", upload(222, True))                 # someone else's file
        self.assertIsNone(community.progress_entry(b.db, "Ingrid"))
        self.assertIn("on the progress board", upload(111, True))
        self.assertEqual(upload(222, None), "")                              # no misleading hint
        self.assertIn("off the progress board", upload(5, False, admin=True))


@unittest.skipIf(discord is None, "discord.py not installed")
class PatchNotesTest(unittest.TestCase):
    def test_an_empty_first_feed_doesnt_make_old_patches_new(self):
        import admin_bot
        from test_patch_notes import PATCH, HOTFIX
        with tempfile.TemporaryDirectory() as d:
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            b.attach(db_path=os.path.join(d, "s.db"))
            posted = []

            async def post(embed):
                posted.append(embed["title"])
                return True
            b._post_patch = post
            self.assertEqual(asyncio.run(b._check_patches([])), [])         # Steam hiccup on the first check
            self.assertIsNone(b._meta("patch:seen"))
            self.assertEqual(asyncio.run(b._check_patches([PATCH, HOTFIX])), [])   # still the first real check
            self.assertEqual(posted, [])


def world(d, chunk: bytes, n: int = 1):
    folder = os.path.join(d, "worlds_local", "Alheim")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"_main.{n}.db2"), "wb") as f:
        f.write(struct.pack("<id", 41, 700000.0 + n))
    with open(os.path.join(folder, "a.chunk"), "wb") as f:
        f.write(b"\x29\x00" + chunk)


@unittest.skipIf(discord is None, "discord.py not installed")
class WorldScanTest(unittest.TestCase):
    def bot(self, d):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
        b.attach(db_path=os.path.join(d, "s.db"))
        return b

    def test_a_failed_write_is_retried(self):
        from test_world_objects import TOMBSTONE
        with tempfile.TemporaryDirectory() as d:
            world(d, TOMBSTONE)
            b = self.bot(d)
            with mock.patch.object(community, "record_death_spots", side_effect=RuntimeError("database is locked")):
                with self.assertLogs("valheim-monitor.bot", "WARNING"):
                    self.assertEqual(len(b.world_objects()["tombstones"]), 1)
            b.world_objects()                                                # same save: stored this time
            self.assertEqual([p["owner"] for p in community.death_spots(b.db)], ["Geedorah"])

    def test_one_scan_at_a_time(self):
        from test_world_objects import piece
        with tempfile.TemporaryDirectory() as d:
            world(d, piece(111) * 3)
            b = self.bot(d)
            calls, gate = [], threading.Event()
            real = wo.scan

            def slow(*a):
                calls.append(1)
                gate.wait(2)
                return real(*a)
            with mock.patch.object(wo, "scan", slow):
                threads = [threading.Thread(target=b.world_objects) for _ in range(4)]
                for t in threads:
                    t.start()
                gate.set()
                for t in threads:
                    t.join(5)
            self.assertEqual(len(calls), 1)

    def test_a_save_removed_while_looking_is_skipped(self):
        import bosses
        with tempfile.TemporaryDirectory() as d:
            world(d, b"")
            world(d, b"", n=2)
            real = os.path.getmtime

            def gone(p):
                if p.endswith("_main.1.db2"):
                    raise FileNotFoundError(p)
                return real(p)
            with mock.patch.object(os.path, "getmtime", gone):
                self.assertTrue(bosses.world_save(d).endswith("_main.2.db2"))


class DeathSpotsTest(unittest.TestCase):
    def test_a_drifting_tombstone_counts_once(self):
        with tempfile.TemporaryDirectory() as d:
            st = stats_db.Store(os.path.join(d, "s.db"))
            t = {"owner": "Ingrid", "died": 700000.0, "x": 10.4, "y": 0, "z": 5.0}
            community.record_death_spots(st.conn, [t])
            community.record_death_spots(st.conn, [dict(t, x=11.6)])          # floated a little
            community.record_death_spots(st.conn, [dict(t, died=None), dict(t, died=None, x=500)])
            self.assertEqual(len(community.death_spots(st.conn)), 3)
            st.close()


class DigestTest(unittest.TestCase):
    def snap(self, ships=(), graves=()):
        return {"pieces": 0, "builders": {}, "unnamed": 0, "portals": [], "tames": [],
                "ships": [list(s) for s in ships], "tombstones": [list(g) for g in graves]}

    def test_a_ship_that_stayed_isnt_moved(self):
        old = self.snap([("karve", 0, 0), ("karve", 1000, 0)])
        new = self.snap([("karve", 600, 0), ("karve", 1000, 0)])
        self.assertEqual(wo.digest(old, new), ["⛵ 1 ship sailed somewhere new"])

    def test_a_cart_isnt_a_ship(self):
        old = self.snap([("cart", 0, 0)])
        self.assertEqual(wo.digest(old, self.snap([("cart", 900, 900)])), [])
        self.assertEqual(wo.digest(old, self.snap([("cart", 0, 0), ("karve", 5, 5)])), ["⛵ 1 karve built"])

    def test_tombstones_without_a_time_are_counted_each(self):
        old = self.snap(graves=[("Bob", 0, 1, 1)])
        new = self.snap(graves=[("Bob", 0, 1, 1), ("Bob", 0, 50, 50), ("Bob", 0, 90, 90)])
        self.assertEqual(wo.digest(old, new), ["🪦 Tombstones: 2 new (Bob), 1 still out there"])

    def test_snapshot_tells_tombstones_apart(self):
        found = {"builders": {}, "names": {}, "portals": [], "ships": [], "tames": [],
                 "tombstones": [{"owner": "Bob", "died": None, "x": 1.2, "z": 2.0},
                                {"owner": "Bob", "died": None, "x": 80.0, "z": 2.0},
                                {"owner": "Ann", "died": 700000.4, "x": 3.0, "z": 3.0}]}
        self.assertEqual(wo.snapshot(found)["tombstones"], [["Bob", 0, 1, 2], ["Bob", 0, 80, 2], ["Ann", 700000]])


class BirthdayTest(unittest.TestCase):
    def test_29_february(self):
        first = dt.date(2024, 2, 29)
        self.assertEqual(community.anniversary(first, dt.date(2025, 2, 28)), "1 year")
        self.assertIsNone(community.anniversary(first, dt.date(2025, 3, 1)))
        self.assertEqual(community.anniversary(first, dt.date(2028, 2, 29)), "4 years")
        self.assertIsNone(community.anniversary(first, dt.date(2028, 2, 28)))


@unittest.skipIf(discord is None, "discord.py not installed")
class BossChannelTest(unittest.TestCase):
    def test_no_bosses_channel_when_boss_progress_is_off(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            for on in (True, False):
                b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d,
                                        "guild_id": "9", "stat_channels": {"enabled": True},
                                        "bosses": {"enabled": on}}, "S")
                b.attach(db_path=os.path.join(d, "s.db"))
                with mock.patch.object(asyncio, "ensure_future", lambda c: c.close()):
                    b._start_tasks()
                self.assertEqual("bosses" in b.stats_show, on)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(discord is None, "discord.py not installed")
class PrivateErrorTest(unittest.TestCase):
    def test_a_world_command_without_a_save_answers_privately(self):
        """/muninn portals defers publicly ("thinking…" for everyone); its "can't find the
        world save" must still only reach the user who asked."""
        import admin_bot
        from types import SimpleNamespace
        log = []

        async def defer(**kw):
            log.append(("defer", kw.get("ephemeral", False)))

        async def followup(text, ephemeral=False, **kw):
            log.append(("followup", ephemeral))

        async def delete():
            log.append(("deleted thinking", None))

        async def original_response():
            return SimpleNamespace(flags=SimpleNamespace(loading=True), delete=delete)

        with tempfile.TemporaryDirectory() as d:
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            it = SimpleNamespace(user=SimpleNamespace(id=9, roles=[]), original_response=original_response,
                                 response=SimpleNamespace(defer=defer, is_done=lambda: True),
                                 followup=SimpleNamespace(send=followup))

            async def run():
                tree = b._build_client().tree
                b._register_commands(tree)
                await tree.get_command("muninn").get_command("portals").callback(it)
            asyncio.run(run())
        self.assertEqual(log, [("defer", False), ("deleted thinking", None), ("followup", True)])
