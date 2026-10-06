import asyncio
import gzip
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bosses  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


def keys_blob(keys):
    """Global keys the way the save stores them: a length byte, then the text."""
    out = struct.pack("<i", len(keys))
    for k in keys:
        out += bytes([len(k)]) + k.encode()
    return b"\x00" * 300 + out + b"\x01\xce/\x00\x00Ab\xe9" * 50


def write_db2(folder, n, keys, seconds=668787.4):
    """A Valheim 1.0 _main.<N>.db2: version 41, the world time, the packed size, then gzip."""
    os.makedirs(folder, exist_ok=True)
    packed = gzip.compress(keys_blob(keys))
    with open(os.path.join(folder, f"_main.{n}.db2"), "wb") as f:
        f.write(struct.pack("<i", 41) + struct.pack("<d", seconds) + struct.pack("<i", len(packed)) + packed)


REAL = ["defeated_eikthyr", "defeated_gdking", "killed_surtling", "defeated_writhan", "defeated_bonemass"]


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.worlds = os.path.join(self.d, "worlds_local")

    def test_reads_the_keys_from_a_1_0_save(self):
        write_db2(os.path.join(self.worlds, "Alheim"), 1029, REAL)
        # defeated_writhan is in the real save but isn't a boss: ignored.
        self.assertEqual(bosses.read_keys(self.d), {"defeated_eikthyr", "defeated_gdking", "defeated_bonemass"})

    def test_newest_save_of_the_live_world(self):
        write_db2(os.path.join(self.worlds, "Alheim"), 1028, ["defeated_eikthyr"])
        write_db2(os.path.join(self.worlds, "Alheim"), 1029, ["defeated_eikthyr", "defeated_gdking"])
        write_db2(os.path.join(self.worlds, "Alheim_backup_auto-20260930-153328"), 1030, REAL)
        self.assertTrue(bosses.world_save(self.d).endswith("Alheim/_main.1029.db2"))
        self.assertEqual(bosses.read_keys(self.d, "Alheim"), {"defeated_eikthyr", "defeated_gdking"})

    def test_older_db_file(self):
        os.makedirs(self.worlds)
        with open(os.path.join(self.worlds, "Alheim.db"), "wb") as f:
            f.write(keys_blob(["defeated_eikthyr"]))
        self.assertEqual(bosses.read_keys(self.d), {"defeated_eikthyr"})

    def test_kall_by_any_key_naming_him(self):
        write_db2(os.path.join(self.worlds, "Alheim"), 1, ["defeated_fader", "defeated_kallfimbulbringer"])
        self.assertEqual(bosses.read_keys(self.d), {"defeated_fader", "defeated_kall"})
        self.assertIn("Kall Fimbulbringer", bosses.render({"defeated_kall"})["description"])

    def test_a_glued_byte_doesnt_hide_a_boss(self):
        """Strings are length-prefixed with nothing between them: a length byte that's a
        letter or digit must not turn defeated_bonemass into an unknown key."""
        folder = os.path.join(self.worlds, "Alheim")
        os.makedirs(folder)
        blob = b"\x11defeated_bonemass5preset_combat_hard_deathpenalty_casual_resources_more_x"
        packed = gzip.compress(blob)
        with open(os.path.join(folder, "_main.1.db2"), "wb") as f:
            f.write(struct.pack("<i", 41) + b"\x00" * 8 + struct.pack("<i", len(packed)) + packed)
        self.assertEqual(bosses.read_keys(self.d), {"defeated_bonemass"})

    def test_unreadable_save_is_none(self):
        folder = os.path.join(self.worlds, "Alheim")
        os.makedirs(folder)
        with open(os.path.join(folder, "_main.1.db2"), "wb") as f:
            f.write(struct.pack("<i", 41) + b"\x00" * 12 + b"\x1f\x8b\x08garbage")
        self.assertIsNone(bosses.read_keys(self.d))

    def test_no_save(self):
        self.assertIsNone(bosses.read_keys(self.d))
        self.assertIsNone(bosses.channel_name(None))

    def test_progress_and_posts(self):
        keys = {"defeated_eikthyr", "defeated_gdking", "defeated_bonemass"}
        p = bosses.progress(keys)
        self.assertEqual((p["count"], p["total"], p["next"]), (3, 8, "Moder"))
        self.assertEqual(bosses.channel_name(keys), "🏆 Bosses: 3/8 · next: Moder")
        e = bosses.render(keys, "Alheim", {"defeated_gdking": 1759000000})
        self.assertIn("✅ 🌳 **The Elder** (Black Forest) · <t:1759000000:d>", e["description"])
        self.assertIn("⬜ 🐉 **Moder** (Mountains)", e["description"])
        self.assertNotIn("Writhan", e["description"])
        a = bosses.announcement("defeated_bonemass", keys, "Alheim")
        self.assertEqual(a["title"], "⚔️ Bonemass has fallen!")
        self.assertIn("**3/8** bosses down; next: **Moder**", a["description"])
        everything = set(bosses.BOSS_KEYS)
        self.assertEqual(bosses.channel_name(everything), "🏆 Bosses: 8/8 · all down!")


@unittest.skipIf(discord is None, "discord.py not installed")
class DayTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.worlds = os.path.join(self.d, "worlds_local")

    def test_real_header(self):
        """The first 16 bytes of a real Alheim _main.1029.db2."""
        os.makedirs(os.path.join(self.worlds, "Alheim"))
        with open(os.path.join(self.worlds, "Alheim", "_main.1029.db2"), "wb") as f:
            f.write(bytes.fromhex("29000000 4023b8c3e6702441 c46a0200 1f8b0800".replace(" ", "")))
        self.assertAlmostEqual(bosses.world_time(self.d), 669811.38, places=2)
        self.assertEqual(bosses.day_of(bosses.world_time(self.d)), 372)

    def test_days(self):
        self.assertEqual(bosses.day_of(2040), 1)                  # a new world starts on day 1
        self.assertEqual(bosses.day_of(3599.9), 1)
        self.assertEqual(bosses.day_of(3600), 2)
        self.assertEqual(bosses.day_of(25920.5), 14)              # the night before day 15

    def test_older_db_and_junk(self):
        os.makedirs(self.worlds)
        path = os.path.join(self.worlds, "Alheim.db")
        with open(path, "wb") as f:
            f.write(struct.pack("<id", 33, 90000.0) + b"\x00" * 64)
        self.assertEqual(bosses.day_of(bosses.world_time(self.d)), 50)
        for junk in (b"", b"\x01\x02", struct.pack("<id", -5, 1.0), struct.pack("<id", 41, float("nan"))):
            with open(path, "wb") as f:
                f.write(junk)
            self.assertIsNone(bosses.world_time(self.d))
        self.assertIsNone(bosses.world_time(tempfile.mkdtemp()))   # no save at all


@unittest.skipIf(discord is None, "discord.py not installed")
class DayChannelTest(unittest.TestCase):
    def test_save_day_and_the_log_day(self):
        import admin_bot
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            world = os.path.join(d, "worlds_local", "Alheim")
            write_db2(world, 1, [], seconds=372 * 1800 + 60)
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "Alheim")
            snap = {}
            b.live = SimpleNamespace(snapshot=lambda: dict(snap))
            self.assertEqual(b._stat_name("day"), "☀️ Day 372")             # no sleep logged yet: the save
            snap["day"] = 373                                                # everyone slept since the save
            self.assertEqual(b._stat_name("day"), "☀️ Day 373")
            write_db2(world, 2, [], seconds=375 * 1800)                      # a newer save, further along
            self.assertEqual(b._stat_name("day"), "☀️ Day 375")
        with tempfile.TemporaryDirectory() as d:                             # no save: the log, or nothing
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            b.live = SimpleNamespace(snapshot=lambda: {})
            self.assertEqual(b._stat_name("day"), "☀️ Day: not known yet")


class BotTest(unittest.TestCase):
    def test_first_check_is_quiet_then_new_kills_are_posted(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            world = os.path.join(d, "worlds_local", "Alheim")
            write_db2(world, 1029, REAL)
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "Alheim")
            posts = []
            b.attach(db_path=os.path.join(d, "s.db"), post_embed=lambda embed, kind="titles": posts.append((kind, embed)))
            self.assertEqual(asyncio.run(b._check_bosses()), [])               # baseline: 3 already down
            self.assertEqual(posts, [])
            self.assertEqual(b._stat_name("bosses"), "🏆 Bosses: 3/8 · next: Moder")
            write_db2(world, 1030, REAL + ["defeated_dragon"])
            self.assertEqual(asyncio.run(b._check_bosses()), ["defeated_dragon"])
            self.assertEqual(posts[0][0], "boss")
            self.assertEqual(posts[0][1]["title"], "⚔️ Moder has fallen!")
            self.assertIn("defeated_dragon", json.loads(b._meta("bosses:when")))
            self.assertEqual(asyncio.run(b._check_bosses()), [])               # nothing new
            self.assertEqual(len(posts), 1)
            # /odin restore to before Moder: when he falls again, that's news again.
            write_db2(world, 1031, REAL)
            asyncio.run(b._check_bosses())
            write_db2(world, 1032, REAL + ["defeated_dragon"])
            self.assertEqual(asyncio.run(b._check_bosses()), ["defeated_dragon"])
            self.assertEqual(len(posts), 2)


if __name__ == "__main__":
    unittest.main()
