import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import community  # noqa: E402
import fch_progress  # noqa: E402
import stats_db  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


def counts(fish=9, bosses=3, crafted=100):
    return {"fishing": [fish, 12], "bosses": [bosses, 8], "crafted": [crafted, 414]}


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.st = stats_db.Store(os.path.join(self.d, "s.db"))
        self.c = self.st.conn

    def tearDown(self):
        self.st.close()

    def test_counts_only(self):
        sections = [{"key": "fishing", "done": {"Perch": 2, "Pike": 1}, "missing": ["Tuna"], "total": 12}]
        self.assertEqual(community.progress_counts(sections), {"fishing": [2, 12]})

    def test_first_upload_finishes_nothing_then_finishing_is_noticed(self):
        self.assertEqual(community.save_progress(self.c, "Ingrid", 111, counts(fish=12), now=1000), (None, []))
        e = community.progress_entry(self.c, "ingrid")
        self.assertEqual((e["player"], e["done"], e["total"], e["sections"]["fishing"]), ("Ingrid", 115, 434, [12, 12]))
        self.assertEqual(community.save_progress(self.c, "Ingrid", 111, counts(fish=12, bosses=8)), (None, ["bosses"]))
        self.assertEqual(community.save_progress(self.c, "Ingrid", 111, counts(fish=12, bosses=8)), (None, []))

    def test_one_uploader_per_character(self):
        community.save_progress(self.c, "Ingrid", 111, counts())
        err, _ = community.save_progress(self.c, "INGRID", 222, counts(fish=12))
        self.assertIn("on the board for <@111>", err)
        self.assertFalse(community.drop_progress(self.c, "Ingrid", 222))
        self.assertTrue(community.drop_progress(self.c, "ingrid", 111))
        self.assertIsNone(community.progress_entry(self.c, "Ingrid"))

    def test_board_and_title(self):
        community.save_progress(self.c, "Ingrid", 111, counts(fish=12, crafted=50), now=1000)
        community.save_progress(self.c, "Bjorn", 222, counts(fish=4, crafted=300), now=2000)
        self.assertEqual([r["player"] for r in community.progress_board(self.c)], ["Bjorn", "Ingrid"])
        fish = community.progress_board(self.c, "fishing")
        self.assertEqual([(r["player"], r["done"], r["total"]) for r in fish], [("Ingrid", 12, 12), ("Bjorn", 4, 12)])
        e = community.render_progress_board(fish, "fishing", "Fish caught", "🎣")
        self.assertEqual(e["title"], "🎣 Achievement progress: Fish caught")
        self.assertIn("🥇 **Ingrid**: 12/12 (100%) ✅ · <t:1000:R>", e["description"])
        self.assertIn("board:True", community.render_progress_board([])["description"])
        lead = community.title_leaders(self.c, since=10 ** 9)          # the period doesn't matter
        self.assertEqual((lead["progress"]["player"], lead["progress"]["v"]), ("Bjorn", 307))
        self.assertEqual(community.title_value(self.c, "progress", "ingrid"), 65)
        self.assertIn("**Mímir**", community.render_titles({"progress": dict(lead["progress"], user_id=None)})
                      ["description"])

    def test_every_list_has_a_finished_line(self):
        self.assertEqual(set(fch_progress.FINISHED), set(fch_progress.SECTIONS))


@unittest.skipIf(discord is None, "discord.py not installed")
class BoardBotTest(unittest.TestCase):
    def bot(self, d):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
        self.posts = []
        b.attach(db_path=os.path.join(d, "s.db"),
                 post_embed=lambda e, kind="titles": self.posts.append((kind, e["title"])) or True)
        b.db.execute("INSERT INTO play_sessions(player, login_at, last_seen_at) VALUES ('Ingrid', 100, 100)")
        return b

    def upload(self, b, board, c, user=111, name="ingrid"):
        sections = [{"key": k, "done": dict.fromkeys(range(v[0]), 1), "total": v[1]} for k, v in c.items()]
        with mock.patch.object(fch_progress, "report", lambda parsed, wanted=None: sections):
            return asyncio.run(b._progress_board(user, name, {}, board))

    def test_opt_in_update_finish_and_leave(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            self.assertIn("board:True", self.upload(b, None, counts()))         # not on it: a hint
            self.assertIsNone(community.progress_entry(b.db, "Ingrid"))
            note = self.upload(b, True, counts())
            self.assertIn("on the progress board (112 done)", note)
            self.assertIn("/valheim link Ingrid", note)                          # not linked: no Mímir role
            self.assertEqual(community.progress_entry(b.db, "Ingrid")["player"], "Ingrid")   # the log's spelling
            self.upload(b, None, counts(fish=12))                                 # remembered: updated
            self.assertEqual(self.posts, [("progress", "🎣 Ingrid has caught every fish!")])
            self.assertIn("off the progress board", self.upload(b, False, counts()))
            self.assertIsNone(community.progress_entry(b.db, "Ingrid"))

    def test_someone_elses_character(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            self.upload(b, True, counts())
            self.assertIn("on the board for <@111>", self.upload(b, True, counts(fish=12), user=222))
            self.assertEqual(self.posts, [])

    def test_valheim_progress_works_anywhere(self):
        """/muninn progress belongs in #muninns-roost; /valheim progress has the same name
        and works anywhere."""
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            b._meta("layout:ch:bots", 300)

            def it(group):
                cmd = SimpleNamespace(name="progress", parent=SimpleNamespace(name=group))
                return SimpleNamespace(command=cmd, channel_id=100, channel=SimpleNamespace(parent_id=None),
                                       user=SimpleNamespace(id=9, roles=[]))
            self.assertEqual(b.wrong_channel(it("muninn")), 300)
            self.assertIsNone(b.wrong_channel(it("valheim")))


if __name__ == "__main__":
    unittest.main()
