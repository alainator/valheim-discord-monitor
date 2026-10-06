import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bosses  # noqa: E402
import world_objects as wo  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

# A real tombstone from an Alheim .chunk file (Valheim 1.0.16), with the object before it.
TOMBSTONE = bytes.fromhex(
    "29713f881092c7070000bb521d7f3f881092c7070000bc521d7f3f881092c7070000f41130e4fbc437629e428e629244"
    "23091ea3198201030518 2c30e4fbc48e49a0428e62924402e86e49eb0000000089f5177601000000028957c9a2c7aaf5"
    "8500000000b467e5fdb63050b67b06000001 96fc294908 4765656 46f72616801c6100ac8110300006d00000020001027"
    .replace(" ", ""))


def portal(x, z, tag=None, prefab="portal_wood"):
    """A portal object laid out like the tombstone above: ids, a short, position, prefab,
    a short, then data sections with the tag among the strings."""
    out = b"\x3f\x88\x10\x92\xc7\x07\x00\x00\xf4\x11" + struct.pack("<3f", x, 30.0, z)
    out += struct.pack("<I", wo.stable_hash(prefab)) + b"\x19\x82"
    out += b"\x01" + struct.pack("<I", wo.stable_hash("creator")) + struct.pack("<q", 12345)
    if tag is not None:
        raw = tag.encode()
        out += b"\x01" + struct.pack("<I", wo.stable_hash("tag")) + bytes([len(raw)]) + raw
    return out + b"\x00" * 24


class ParseTest(unittest.TestCase):
    def test_hash(self):
        self.assertEqual(struct.unpack("<i", struct.pack("<I", wo.stable_hash("portal_wood")))[0], -661882940)

    def test_real_tombstone(self):
        found = wo.scan_bytes(b"\x00" * 50 + TOMBSTONE)
        self.assertEqual(found["portals"], [])
        (t,) = found["tombstones"]
        self.assertEqual(t["owner"], "Geedorah")
        self.assertEqual(bosses.day_of(t["died"]), 396)
        self.assertEqual((round(t["x"]), round(t["z"])), (-2015, 1171))

    def test_portals(self):
        data = (portal(100, 200, "Home") + portal(-3000, 50, "Home") + portal(10, 10, "Swamp")
                + portal(5, 5) + portal(7, 7, "Mine") + portal(1, 1, "Mine") + portal(2, 2, "Mine")
                + portal(800, -900, "Ash", prefab="portal_stone"))
        found = wo.scan_bytes(data)
        tags = [p["tag"] for p in found["portals"]]
        self.assertEqual(tags, ["Home", "Home", "Swamp", "", "Mine", "Mine", "Mine", "Ash"])   # no name stays nameless
        self.assertEqual(found["portals"][-1]["kind"], "stone portal")
        pairs = wo.portal_pairs(found["portals"])
        self.assertEqual(pairs["pairs"], ["Home"])
        self.assertEqual(sorted(p["tag"] for p in pairs["lonely"]), ["Ash", "Swamp"])
        self.assertEqual(list(pairs["crowded"]), ["Mine"])
        self.assertEqual(len(pairs["unnamed"]), 1)
        e = wo.render_portals(found["portals"], "Alheim")
        self.assertEqual(e["title"], "🌀 Portals in Alheim: 8")
        self.assertIn("🔸 **Swamp** · x 10, z 10 · near the start", e["description"])
        self.assertIn("🔶 **Mine** ×3", e["description"])
        self.assertIn("**Connected** (1): Home", e["description"])

    def test_junk_hits_are_ignored(self):
        """A prefab hash turning up by chance, with no sensible position before it."""
        junk = struct.pack("<3f", float("nan"), 1, 1) + struct.pack("<I", wo.stable_hash("portal_wood"))
        junk += struct.pack("<3f", 1e9, 1, 1) + struct.pack("<I", wo.stable_hash("Player_tombstone"))
        self.assertEqual(wo.scan_bytes(junk), {"portals": [], "tombstones": []})

    def test_where(self):
        self.assertEqual(wo.where(20, -40), "x 20, z -40 · near the start")
        self.assertEqual(wo.where(-2015, 1171), "x -2015, z 1171 · 2.3 km NW of the start")
        self.assertEqual(wo.where(0, -5000), "x 0, z -5000 · 5.0 km S of the start")

    def test_tombstone_card(self):
        t = [{"owner": "Ingrid", "died": 396 * 1800 + 5, "x": 1, "y": 0, "z": 1},
             {"owner": "Bjorn", "died": None, "x": 3000, "y": 0, "z": 0}]
        e = wo.render_tombstones(t, bosses.day_of, "Alheim")
        self.assertEqual(e["title"], "🪦 Tombstones still out there in Alheim: 2")
        self.assertIn("🪦 **Ingrid** · died on day 396\n   x 1, z 1 · near the start", e["description"])
        self.assertIn("🪦 **Bjorn**\n", e["description"])
        self.assertIn("Everyone has picked up", wo.render_tombstones([])["description"])


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.world = os.path.join(self.d, "worlds_local", "Alheim")
        os.makedirs(self.world)
        with open(os.path.join(self.world, "_main.5.db2"), "wb") as f:
            f.write(struct.pack("<id", 41, 700000.0))

    def chunk(self, name, data, folder=None):
        with open(os.path.join(folder or self.world, name), "wb") as f:
            f.write(b"\x29\x00" + data)

    def test_reads_every_chunk_of_the_live_world(self):
        self.chunk("1e_1e__1_2.chunk", portal(1, 1, "Home"))
        self.chunk("20_20__1_9.chunk", portal(2, 2, "Home") + TOMBSTONE)
        backup = os.path.join(self.d, "worlds_local", "Alheim_backup_auto-20261001-120000")
        os.makedirs(backup)
        self.chunk("1e_1e__1_1.chunk", portal(3, 3, "Old"), backup)
        found = wo.scan(self.d)
        self.assertEqual(sorted(p["tag"] for p in found["portals"]), ["Home", "Home"])
        self.assertEqual([t["owner"] for t in found["tombstones"]], ["Geedorah"])
        self.assertIsNone(wo.scan(tempfile.mkdtemp()))

    def test_command_line(self):
        import io
        from contextlib import redirect_stdout
        self.chunk("1e_1e__1_2.chunk", portal(1, 1, "Home") + TOMBSTONE)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(wo.main([self.d]), 0)
        self.assertIn("1 portals: 0 pairs, 1 alone", out.getvalue())
        self.assertIn("Geedorah", out.getvalue())

    @unittest.skipIf(discord is None, "discord.py not installed")
    def test_bot_rereads_only_after_a_save(self):
        import admin_bot
        self.chunk("1e_1e__1_2.chunk", portal(1, 1, "Home"))
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": self.d}, "S")
        with mock.patch.object(wo, "scan", wraps=wo.scan) as spy:
            self.assertEqual(len(b.world_objects()["portals"]), 1)
            b.world_objects()
            self.assertEqual(spy.call_count, 1)
            with open(os.path.join(self.world, "_main.6.db2"), "wb") as f:     # the next save
                f.write(struct.pack("<id", 41, 701800.0))
            b.world_objects()
            self.assertEqual(spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
