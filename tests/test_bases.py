import gzip
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import world_objects as wo  # noqa: E402
from test_storage import box, inv, sign  # noqa: E402
from test_world_objects import portal  # noqa: E402


def piece(x, z, prefab, creator=None, owner=None):
    """A placed piece in the same layout as the others: its creator, and for a bed the
    owner's ID and character name."""
    out = b"\x3f\x88\x10\x92\xc7\x07\x00\x00\xf4\x11" + struct.pack("<3f", x, 30.0, z)
    out += struct.pack("<I", wo.stable_hash(prefab)) + b"\x19\x82"
    if creator is not None:
        out += b"\x01" + struct.pack("<I", wo.stable_hash("creator")) + struct.pack("<q", creator)
    if owner is not None:
        pid, name = owner
        raw = name.encode()
        out += b"\x01" + struct.pack("<I", wo.stable_hash("owner")) + struct.pack("<q", pid)
        out += b"\x01" + struct.pack("<I", wo.stable_hash("ownerName")) + bytes([len(raw)]) + raw
    return out + b"\x00" * 24


def _s(text):
    raw = text.encode()
    return bytes([len(raw)]) + raw


def map_data(pins, version=3, cells=64):
    """A cartography table's "data": the explored map, then the pins, gzipped."""
    body = struct.pack("<ii", version, cells) + bytes([1] * (cells // 4)) + bytes(cells - cells // 4)
    body += struct.pack("<i", len(pins))
    for name, icon, x, z in pins:
        body += struct.pack("<q", 81773579) + _s(name) + struct.pack("<3f", x, 30.0, z)
        body += struct.pack("<iB", icon, 0) + _s("Steam_76561190000000000")
    return gzip.compress(body)


def table(x, z, blob):
    out = b"\x3f\x88\x10\x92\xc7\x07\x00\x00\xf4\x11" + struct.pack("<3f", x, 30.0, z)
    out += struct.pack("<I", wo.stable_hash("piece_cartographytable")) + b"\x19\x82"
    out += b"\x01" + struct.pack("<I", wo.stable_hash("creator")) + struct.pack("<q", 111)
    out += b"\x01" + struct.pack("<I", wo.stable_hash("data")) + struct.pack("<i", len(blob)) + blob
    return out + b"\x00" * 24


HOUSE, FIRE = 1, 0
PINS = [("Longhouse", HOUSE, 1030, 1020), ("Hilltop", HOUSE, 1000, 1200), ("", HOUSE, 1005, 1000),
        ("Copper", 3, 1010, 1010), ("Shed", FIRE, -500, -500)]


def world():
    return (
        # Ingrid and Bjorn's house: two beds, a workbench, a forge, chests, a sign and a portal
        piece(1000, 1000, "bed", 111, (111, "Ingrid")) + piece(1004, 1000, "bed", 222, (222, "Bjorn"))
        + piece(1010, 1005, "piece_workbench", 111) + piece(1012, 1005, "piece_workbench", 111)
        + piece(1015, 1010, "forge", 222)
        + box(1008, 998, "piece_chest_wood", inv([("Wood", 10)])) + box(1009, 998, "piece_chest", inv([("Iron", 3)]))
        + sign(1001, 1002, "Caterpillar House") + portal(1050, 1050, "Home") + table(1006, 1004, map_data(PINS))
        # a workbench put down for a bridge: an outpost, not a base
        + piece(-3000, 500, "piece_workbench", 111)
        # a storage shed: three chests, nobody's bed, a sign too far away to name it
        + box(-500, -500, "piece_chest_wood", inv([("Stone", 50)])) + box(-502, -500, "piece_chest_wood", inv([]))
        + box(-504, -500, "piece_chest_barrel", inv([("Honey", 4)])) + sign(-560, -500, "Road")
        # a ward and a workbench chained 35 m apart, the ward placed by someone with no name yet
        + piece(2000, -2000, "guard_stone", 999) + piece(2035, -2000, "piece_workbench", 999)
    )


class BasesTest(unittest.TestCase):
    def setUp(self):
        self.found = wo.scan_bytes(world())
        self.found["names"].update({111: "Ingrid", 222: "Bjorn"})
        self.bases, self.outposts = wo.bases(self.found)

    def test_pieces_are_read(self):
        kinds = sorted(p["kind"] for p in self.found["pieces"])
        self.assertEqual(kinds.count("bed"), 2)
        self.assertEqual(kinds.count("chest"), 5)
        bed = next(p for p in self.found["pieces"] if p["kind"] == "bed")
        self.assertEqual((bed["owner"], bed["creator"]), ("Ingrid", 111))

    def test_grouping(self):
        self.assertEqual(len(self.bases), 3)
        self.assertEqual(self.outposts, 1)                             # the bridge workbench
        home = self.bases[0]
        self.assertEqual(home["pieces"], 8)
        self.assertEqual(home["beds"], ["Bjorn", "Ingrid"])
        self.assertEqual(home["kinds"], {"bed": 2, "workbench": 2, "forge": 1, "chest": 2, "cartography table": 1})
        # The named house pin within 50 m; not the sign, not the unnamed house, not "Hilltop" (200 m off)
        self.assertEqual((home["pin"], home["portal"]), ("Longhouse", "Home"))
        self.assertEqual(home["builders"], [("Ingrid", 4), ("Bjorn", 2)])
        shed = next(b for b in self.bases if b["kinds"].get("chest") == 3)
        self.assertIsNone(shed["pin"])                                 # a fire pin doesn't name a base

    def test_chained_pieces_are_one_base(self):
        ward = next(b for b in self.bases if "ward" in b["kinds"])
        self.assertEqual(ward["kinds"], {"ward": 1, "workbench": 1})

    def test_render(self):
        e = wo.render_bases(self.found, "Alheim")
        self.assertEqual(e["title"], "🏠 Bases in Alheim: 3")
        d = e["description"]
        self.assertIn("🏠 **Longhouse** · near the Home portal · x 1008, z 1002", d)
        self.assertIn("🛏️ Bjorn and Ingrid · 🔨 2 workbenches · 🗺️ cartography table · ⚒️ forge · 📦 2 chests", d)
        self.assertNotIn("built by Bjorn and Ingrid", d)                # same people as the beds
        self.assertIn("A base nobody's named", d)                       # the ward: no names known
        self.assertIn("📦 3 chests", d)
        self.assertIn("1 lone workbench not counted", e["footer"]["text"])

    def test_named_by_bed_or_builder(self):
        self.assertEqual(wo.base_name({"pin": None, "beds": ["Ingrid"], "builders": []}), "Ingrid's base")
        self.assertEqual(wo.base_name({"pin": None, "beds": [], "builders": [("Bjorn", 9)]}), "Bjorn's base")
        self.assertEqual(wo.base_name({"pin": None, "beds": ["A", "B", "C", "D"], "builders": []}),
                         "A, B and 2 more's base")
        self.assertEqual(wo.base_name({"pin": "*Big* house", "beds": ["Ingrid"], "builders": []}), "\\*Big\\* house")

    def test_nothing_yet(self):
        self.assertIn("No bases yet", wo.render_bases(wo.scan_bytes(b""))["description"])


class MapPinsTest(unittest.TestCase):
    def test_pins_are_read(self):
        found = wo.scan_bytes(table(0, 0, map_data(PINS)))
        self.assertEqual([(p["name"], p["icon"], round(p["x"]), round(p["z"])) for p in found["pins"]],
                         [("Longhouse", "house", 1030, 1020), ("Hilltop", "house", 1000, 1200), ("", "house", 1005, 1000),
                          ("Copper", "dot", 1010, 1010), ("Shed", "fire", -500, -500)])
        self.assertEqual(found["pins"][0]["owner"], 81773579)
        self.assertNotIn("Steam", repr(found["pins"]))               # the author's platform ID isn't kept
        self.assertEqual(found["pieces"][0]["kind"], "cartography table")

    def test_nothing_shared_or_not_understood(self):
        for blob in (map_data([], cells=8), map_data(PINS, version=4), b"not gzip at all",
                     map_data(PINS)[:-6] + gzip.compress(b"x")[-6:]):
            self.assertEqual(wo.scan_bytes(table(0, 0, blob))["pins"], [], blob[:12])
        # an extra byte after the last pin: the layout isn't what we think, so nothing is used
        extra = gzip.compress(gzip.decompress(map_data(PINS)) + b"\x00")
        self.assertEqual(wo.scan_bytes(table(0, 0, extra))["pins"], [])
        self.assertEqual(wo.scan_bytes(table(0, 0, b""))["pins"], [])

    def test_two_tables_share_their_pins_once(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(folder)
            with open(os.path.join(folder, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 700000.0))
            for n, x in enumerate((10, 900)):
                with open(os.path.join(folder, f"{n}.chunk"), "wb") as f:
                    f.write(b"\x29\x00" + table(x, x, map_data(PINS)))
            found = wo.scan(d)
        self.assertEqual(len(found["pins"]), 5)


class SignsTest(unittest.TestCase):
    def setUp(self):
        self.found = wo.scan_bytes(sign(10, 10, "<color=red>Ores</color>") + sign(500, 0, "This way to *Big Boy* Land")
                                   + sign(0, 3000, "   ") + portal(520, 0, "Swamp"))

    def test_rich_text_and_blank_signs(self):
        self.assertEqual(sorted(s["text"] for s in self.found["signs"]), ["Ores", "This way to *Big Boy* Land"])

    def test_render(self):
        e = wo.render_signs(self.found, server_name="Alheim")
        self.assertEqual(e["title"], "🪧 Signs in Alheim: 2")
        lines = e["description"].split("\n")
        self.assertTrue(lines[0].startswith("🪧 “Ores” · x 10, z 10"))
        self.assertIn("🪧 “This way to \\*Big Boy\\* Land” · near the Swamp portal", lines[1])

    def test_search(self):
        e = wo.render_signs(self.found, "big boy")
        self.assertEqual(e["title"], "🪧 Signs: 1 with “big boy”")
        self.assertIn("No sign says **gold**", wo.render_signs(self.found, "gold")["description"])
        self.assertIn("No signs yet", wo.render_signs(wo.scan_bytes(b""))["description"])

    def test_find_escapes_the_sign(self):
        found = wo.scan_bytes(box(0, 0, "piece_chest_wood", inv([("Iron", 5)])) + sign(2, 0, "_Iron_"))
        self.assertIn("by the sign “\\_Iron\\_”", wo.render_find(found, "Iron")["description"])


if __name__ == "__main__":
    unittest.main()
