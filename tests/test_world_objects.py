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


def thing(x, z, prefab, ints=(), name=None):
    """A ship or animal in the same layout: ints (key, value), then strings."""
    out = b"\x3f\x88\x10\x92\xc7\x07\x00\x00\xf4\x11" + struct.pack("<3f", x, 30.0, z)
    out += struct.pack("<I", wo.stable_hash(prefab)) + b"\x19\x82"
    if ints:
        out += bytes([len(ints)]) + b"".join(struct.pack("<Ii", wo.stable_hash(k), v) for k, v in ints)
    if name is not None:
        raw = name.encode()
        out += b"\x01" + struct.pack("<I", wo.stable_hash("TamedName")) + bytes([len(raw)]) + raw
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
        self.assertEqual(wo.scan_bytes(junk), {"portals": [], "tombstones": [], "ships": [], "tames": [],
                                              "containers": [], "signs": [], "pieces": [], "pins": [], "explored": None,
                                              "builders": {}, "names": {}})

    def test_ships_and_tames(self):
        data = (thing(10, 10, "Karve") + thing(-4000, 2000, "VikingShip") + thing(5, 5, "Cart")
                + thing(1, 1, "Wolf", [("tamed", 1), ("level", 3)], "Fenrir")
                + thing(2, 2, "Wolf", [("level", 2)])                        # wild: not listed
                + thing(3, 3, "Boar", [("tamed", 1)])
                + thing(4, 4, "Boar", [("tamed", 1)])
                + thing(6, 6, "Lox", [("tamed", 1), ("level", 1)], "Big Bertha"))
        found = wo.scan_bytes(data)
        self.assertEqual([s["kind"] for s in found["ships"]], ["karve", "longship", "cart"])
        self.assertEqual([(a["kind"], a["name"], a["stars"]) for a in found["tames"]],
                         [("wolf", "Fenrir", 2), ("boar", "", 0), ("boar", "", 0), ("lox", "Big Bertha", 0)])
        e = wo.render_ships(found["ships"], "Alheim")
        self.assertEqual(e["title"], "⛵ Ships in Alheim: 2")
        self.assertTrue(e["description"].startswith("1 karve · 1 longship"))
        self.assertLess(e["description"].index("Longship"), e["description"].index("Karve"))   # furthest first
        self.assertIn("🛒 **Carts** (1)", e["description"])
        e = wo.render_tames(found["tames"])
        self.assertTrue(e["description"].startswith("🐗 2 boars · 🐺 1 wolf · 🦣 1 lox"))
        self.assertIn("🦣 **Big Bertha** (lox) · x 6, z 6 · near the start", e["description"])
        self.assertIn("🐺 **Fenrir** (wolf ★★)", e["description"])
        self.assertIn("…plus 2 without a name", e["description"])
        self.assertIn("No tamed animals yet", wo.render_tames([])["description"])
        self.assertIn("No ships yet", wo.render_ships([])["description"])

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


def piece(pid, prefab="wood_wall"):
    """A placed piece: position, prefab, then the longs with "creator"."""
    out = b"\xf4\x11" + struct.pack("<3f", 1.0, 30.0, 1.0) + struct.pack("<I", wo.stable_hash(prefab)) + b"\x19\x82"
    return out + b"\x01" + struct.pack("<I", wo.stable_hash("creator")) + struct.pack("<q", pid) + b"\x00" * 8


def bed(pid, name):
    """A bed: "owner" in the longs, "ownerName" in the strings, like the tombstone."""
    raw = name.encode()
    out = b"\xf4\x11" + struct.pack("<3f", 2.0, 30.0, 2.0) + struct.pack("<I", wo.stable_hash("bed")) + b"\x19\x82"
    out += b"\x01" + struct.pack("<I", wo.stable_hash("owner")) + struct.pack("<q", pid)
    return out + b"\x01" + struct.pack("<I", wo.stable_hash("ownerName")) + bytes([len(raw)]) + raw + b"\x00" * 8


class BuildersTest(unittest.TestCase):
    def test_counts_and_names(self):
        data = (piece(111) * 5 + piece(222) * 3 + piece(333) * 2 + piece(444)
                + bed(111, "Ingrid") + bed(222, "Bjorn") + bed(444, "Ingrid"))   # Ingrid remade: two IDs
        found = wo.scan_bytes(data + TOMBSTONE)
        self.assertEqual(found["builders"], {111: 5, 222: 3, 333: 2, 444: 1})
        self.assertEqual(found["names"][2247469767], "Geedorah")                # from the real tombstone
        named, unnamed = wo.builder_counts(found)
        self.assertEqual(named, [("Ingrid", 6), ("Bjorn", 3)])
        self.assertEqual(unnamed, (1, 2))
        e = wo.render_builders(found, "Alheim")
        self.assertEqual(e["title"], "🔨 Builders of Alheim: 11 pieces")
        self.assertIn("🥇 **Ingrid**: 6 pieces", e["description"])
        self.assertIn("…plus **1** builder I can't name yet (2 pieces)", e["description"])

    def test_title_and_table(self):
        import community
        import stats_db
        with tempfile.TemporaryDirectory() as d:
            st = stats_db.Store(os.path.join(d, "s.db"))
            st.login("Ingrid", 100)
            community.save_builders(st.conn, [("ingrid", 600), ("Bjorn", 300)])
            rows = dict(st.conn.execute("SELECT player, pieces FROM builders").fetchall())
            self.assertEqual(rows, {"Ingrid": 600, "Bjorn": 300})              # the log's spelling
            lead = community.title_leaders(st.conn, since=10 ** 9)
            self.assertEqual((lead["builder"]["player"], lead["builder"]["v"]), ("Ingrid", 600))
            community.save_builders(st.conn, [("Bjorn", 900)])                 # replaced, not added
            self.assertEqual(dict(st.conn.execute("SELECT player, pieces FROM builders").fetchall()), {"Bjorn": 900})
            st.close()

    @unittest.skipIf(discord is None, "discord.py not installed")
    def test_bot_stores_the_builders_after_a_scan(self):
        import admin_bot
        import community
        with tempfile.TemporaryDirectory() as d:
            world = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(world)
            with open(os.path.join(world, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 700000.0))
            with open(os.path.join(world, "a.chunk"), "wb") as f:
                f.write(b"\x29\x00" + piece(111) * 4 + bed(111, "Ingrid"))
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            b.attach(db_path=os.path.join(d, "s.db"))
            b.world_objects()
            self.assertEqual(community.title_leaders(b.db)["builder"]["player"], "Ingrid")


class DigestTest(unittest.TestCase):
    def snap(self, pieces=100, builders=None, portals=(), ships=(), graves=(), tames=()):
        return {"pieces": pieces, "builders": builders or {}, "unnamed": 0, "portals": sorted(portals),
                "ships": [list(s) for s in ships], "tombstones": [list(g) for g in graves], "tames": [list(t) for t in tames]}

    def test_a_week_of_changes(self):
        old = self.snap(1000, {"Ingrid": 600, "Bjorn": 400}, ["Home", "Home", "Swamp"],
                        [("karve", 0, 0), ("longship", 500, 500), ("raft", 10, 10)],
                        [("Ingrid", 700000), ("Bjorn", 700100)], [("boar", ""), ("wolf", "Fenrir")])
        new = self.snap(1650, {"Ingrid": 1000, "Bjorn": 450, "Sigrid": 200}, ["Home", "Home", "Plains", "Plains"],
                        [("karve", 20, 30), ("longship", 3000, 400), ("karve", 900, 900)],
                        [("Bjorn", 700100), ("Sigrid", 720000)],
                        [("boar", ""), ("boar", ""), ("boar", ""), ("wolf", "Fenrir"), ("lox", "Bertha")])
        lines = wo.digest(old, new)
        self.assertEqual(lines[0], "🔨 **+650 pieces** (now 1,650) · most building: Ingrid +400, Sigrid +200, Bjorn +50")
        self.assertIn("🌀 New portals: Plains", lines)
        self.assertIn("🌀 Portals taken down: Swamp", lines)
        self.assertIn("⛵ 1 karve built · 1 raft gone · 1 ship sailed somewhere new", lines)
        self.assertIn("🪦 Tombstones: 1 recovered, 1 new (Sigrid), 1 still out there", lines)
        self.assertIn("🐾 Tames: +2 boars, +1 lox · newly named: Bertha", lines)

    def test_quiet_and_shrinking(self):
        same = self.snap(500, {"Ingrid": 500})
        self.assertEqual(wo.digest(same, same), [])
        lines = wo.digest(same, self.snap(420, {"Ingrid": 420}))
        self.assertEqual(lines, ["🔨 **−80 pieces** (now 420) · torn down, or wrecked by raids"])

    def test_recap_step(self):
        import extras
        import stats_db
        with tempfile.TemporaryDirectory() as d:
            world = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(world)
            with open(os.path.join(world, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 700000.0))
            chunk = os.path.join(world, "a.chunk")
            with open(chunk, "wb") as f:
                f.write(b"\x29\x00" + piece(111) * 3 + bed(111, "Ingrid"))
            st = stats_db.Store(os.path.join(d, "s.db"))

            def recap():                                                       # digest, post, then keep
                text, snap = extras.world_digest(st, d)
                extras.keep_world_snapshot(st, snap)
                return text
            self.assertIsNone(recap())                                         # first week: just remembers
            self.assertEqual(recap(), "A quiet week: nothing built, sailed, tamed or lost.")
            with open(chunk, "wb") as f:
                f.write(b"\x29\x00" + piece(111) * 5 + bed(111, "Ingrid") + portal(1, 1, "Home"))
            extras.world_digest(st, d)                                         # a recap that failed to post…
            text, _ = extras.world_digest(st, d)                               # …retried: still against last week
            self.assertIn("🔨 **+3 pieces** (now 6) · most building: Ingrid +2", text)      # the portal is a piece too
            self.assertIn("🌀 New portals: Home", text)
            self.assertEqual(extras.world_digest(st, tempfile.mkdtemp()), (None, None))      # no save: nothing
            st.close()
        self.assertTrue(extras.WeeklyRecap({}).world)
        self.assertFalse(extras.WeeklyRecap({"world": False}).world)


@unittest.skipIf(discord is None, "discord.py not installed")
class ThreadTest(unittest.TestCase):
    def test_scan_in_a_worker_thread_like_the_commands(self):
        """The commands run world_objects() in a worker thread while the bot's own database
        connection belongs to the bot's thread. Storing the builders must still work."""
        import asyncio
        import admin_bot
        import community
        with tempfile.TemporaryDirectory() as d:
            world = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(world)
            with open(os.path.join(world, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 700000.0))
            with open(os.path.join(world, "a.chunk"), "wb") as f:
                f.write(b"\x29\x00" + piece(111) * 4 + bed(111, "Ingrid"))
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            b.attach(db_path=os.path.join(d, "s.db"))
            b._meta("touch", 1)                                   # the bot thread opens its connection

            async def command():
                return await asyncio.to_thread(b.world_objects)
            found = asyncio.run(command())
            self.assertEqual(found["builders"], {111: 4})
            self.assertEqual(community.title_leaders(b.db)["builder"]["player"], "Ingrid")


@unittest.skipIf(discord is None, "discord.py not installed")
class CommandErrorTest(unittest.TestCase):
    """A failing command answers privately, whatever state its reply was in."""

    def run_error(self, done, loading=None):
        import asyncio
        import admin_bot
        from types import SimpleNamespace
        from discord import app_commands
        log = []

        async def followup(text, ephemeral=False):
            log.append(("followup", ephemeral))

        async def send_message(text, ephemeral=False):
            log.append(("response", ephemeral))

        async def delete():
            log.append(("deleted thinking", None))

        async def original_response():
            return SimpleNamespace(flags=SimpleNamespace(loading=loading), delete=delete)

        async def run():
            with tempfile.TemporaryDirectory() as d:
                b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
                tree = b._build_client().tree
            it = SimpleNamespace(command=SimpleNamespace(qualified_name="muninn builders"),
                                 response=SimpleNamespace(is_done=lambda: done, send_message=send_message),
                                 followup=SimpleNamespace(send=followup), original_response=original_response)
            err = app_commands.CommandInvokeError(SimpleNamespace(name="builders", qualified_name="x"),
                                                  RuntimeError("boom"))
            with self.assertLogs("valheim-monitor.bot", "WARNING"):
                await tree.on_error(it, err)
        asyncio.run(run())
        return log

    def test_still_thinking_publicly(self):
        """After a public defer, Discord would show the first follow-up publicly: the
        "thinking…" reply goes first, then the private note."""
        self.assertEqual(self.run_error(True, loading=True), [("deleted thinking", None), ("followup", True)])

    def test_already_answered(self):
        self.assertEqual(self.run_error(True, loading=False), [("followup", True)])

    def test_not_answered_yet(self):
        self.assertEqual(self.run_error(False), [("response", True)])


class DeathMapTest(unittest.TestCase):
    def test_recording_hotspots_and_cards(self):
        import community
        import stats_db
        with tempfile.TemporaryDirectory() as d:
            st = stats_db.Store(os.path.join(d, "s.db"))
            graves = [{"owner": "Ingrid", "died": 700000.4, "x": -2010.2, "y": 0, "z": 1170.0},
                      {"owner": "Bjorn", "died": 700500.0, "x": -2050.0, "y": 0, "z": 1150.0},
                      {"owner": "Bjorn", "died": None, "x": 300.0, "y": 0, "z": 300.0}]
            self.assertEqual(community.record_death_spots(st.conn, graves), 3)
            self.assertEqual(community.record_death_spots(st.conn, graves[:2]), 0)      # seen again: once
            self.assertEqual(len(community.death_spots(st.conn)), 3)
            self.assertEqual(len(community.death_spots(st.conn, "bjorn")), 2)
            points = community.death_spots(st.conn)
            st.close()
        spots = wo.hotspots(points)
        self.assertEqual(spots[0][0], 2)                                    # the two near x -2030
        self.assertEqual(spots[0][3], {"Ingrid": 1, "Bjorn": 1})
        e = wo.render_deathmap(points, server_name="Alheim")
        self.assertEqual(e["title"], "💀 Where we die in Alheim")
        self.assertIn("**3** tombstones recorded.", e["description"])
        self.assertIn("☠️ **2** · x -2030, z 1160 · 2.3 km NW of the start", e["description"])
        self.assertIn("No tombstones recorded yet", wo.render_deathmap([], "Bjorn")["description"])

    def test_picture(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        self.assertIsNone(wo.death_map([]))
        pts = [{"owner": "Ingrid", "x": -2000, "z": 1170}, {"owner": "Bjorn", "x": -2010, "z": 1160}]
        png = wo.death_map(pts, [{"tag": "Elder", "x": -1741, "z": 1018}], "Where we die")
        self.assertTrue(png.startswith(b"\x89PNG"))

    @unittest.skipIf(discord is None, "discord.py not installed")
    def test_bot_records_tombstones_from_each_scan(self):
        import asyncio
        import admin_bot
        import community
        with tempfile.TemporaryDirectory() as d:
            world = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(world)
            with open(os.path.join(world, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 700000.0))
            with open(os.path.join(world, "a.chunk"), "wb") as f:
                f.write(b"\x29\x00" + TOMBSTONE)
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            b.attach(db_path=os.path.join(d, "s.db"))
            b._meta("touch", 1)

            async def scan():
                return await asyncio.to_thread(b.world_objects)
            asyncio.run(scan())
            self.assertEqual([p["owner"] for p in community.death_spots(b.db)], ["Geedorah"])
            with open(os.path.join(world, "a.chunk"), "wb") as f:      # emptied: gone from the save
                f.write(b"\x29\x00")
            with open(os.path.join(world, "_main.2.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 701800.0))
            asyncio.run(scan())
            self.assertEqual(len(community.death_spots(b.db)), 1)       # still on the map
