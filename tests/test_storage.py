import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import items  # noqa: E402
import world_objects as wo  # noqa: E402
from test_world_objects import portal  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

# The "items" of two real chests from an Alheim .chunk file (Valheim 1.0.16): the key's
# section count and hash, the length, then the inventory.
CHEST_1 = bytes.fromhex(
    "01c6100ac87e0000006d0000000700102700000101004 91e00619f3bc700102700000001004903007067 13a3001027000004"
    "0000417fc3902600102700000100004 91e00293c0769001027000000000049 1e00293c0769001027000004010069500 00bc4df04"
    "00000000084672616e6b65656dbb96eb98001027000002000049 0c00293c076900".replace(" ", ""))
CHEST_2 = bytes.fromhex(
    "01c6100ac85a0000006d00000006001027000000000049 1e00293c0769001027000001000049 1e00293c076900102700000401"
    "0041442e3ed300102700000300 0041fd7258f500102700000301004909 00293c0769001027000004000041791d1e8e00".replace(" ", ""))


def name(item_id):
    return wo.stable_hash(item_id)


def box(x, z, prefab, inventory_bytes, extra=b""):
    """A container: position, prefab, then its data with the items array."""
    out = b"\x3f\x88\x10\x92\xc7\x07\x00\x00\xf4\x11" + struct.pack("<3f", x, 30.0, z)
    return out + struct.pack("<I", wo.stable_hash(prefab)) + b"\x19\x82" + extra + inventory_bytes + b"\x00" * 16


def inv(entries):
    """An items array in the same layout, from [(item ID, stack)]."""
    body = struct.pack("<iH", 109, len(entries))
    for k, (item, n) in enumerate(entries):
        flags = 0x49 if n > 1 else 0x41
        body += struct.pack("<i", 10000) + bytes([k % 5, k // 5, 0, flags])
        body += (struct.pack("<H", n) if n > 1 else b"") + struct.pack("<I", wo.stable_hash(item)) + b"\x00"
    return b"\x01" + struct.pack("<I", wo.stable_hash("items")) + struct.pack("<i", len(body)) + body


def sign(x, z, text):
    raw = text.encode()
    out = b"\xf4\x11" + struct.pack("<3f", x, 30.0, z) + struct.pack("<I", wo.stable_hash("sign")) + b"\x19\x82"
    return out + b"\x01" + struct.pack("<I", wo.stable_hash("text")) + bytes([len(raw)]) + raw + b"\x00" * 8


class InventoryTest(unittest.TestCase):
    def test_real_chests(self):
        first = [(items.name_of(h), n, who) for h, n, who in wo.inventory(CHEST_1, 1)]
        self.assertEqual(first, [("Flint", 30, None), ("Cooked deer meat", 3, None), ("Frost Resistance Mead", 1, None),
                                 ("Black metal scrap", 30, None), ("Black metal scrap", 30, None),
                                 ("Bronze nails", 80, "Frankeem"), ("Black metal scrap", 12, None)])
        second = wo.inventory(CHEST_2, 1)
        self.assertEqual(len(second), 6)
        self.assertEqual(items.name_of(second[3][0]), "unknown item (fd7258f5)")   # not on the wiki's list

    def test_not_an_inventory(self):
        self.assertIsNone(wo.inventory(b"\x01" + b"\xc6\x10\x0a\xc8" + struct.pack("<i", 999999), 1))
        self.assertIsNone(wo.inventory(b"\x01\xc6\x10\x0a\xc8" + struct.pack("<i", 6) + struct.pack("<iH", 5, 1), 1))

    def test_an_unknown_field_stops_the_read(self):
        """A flags bit we haven't seen means a field of unknown size: stop, don't misread."""
        good = inv([("Wood", 50)])
        body = good[9:] + struct.pack("<i", 10000) + bytes([1, 0, 0, 0x49 | 0x02]) + b"\x01\x00" + b"\x00" * 9
        body = body[:4] + struct.pack("<H", 2) + body[6:]
        data = good[:5] + struct.pack("<i", len(body)) + body
        self.assertEqual([(items.name_of(h), n) for h, n, _ in wo.inventory(data, 1)], [("Wood", 50)])

    def test_names(self):
        self.assertEqual(items.name_of(wo.stable_hash("QueensJam")), "Queen's Jam")
        self.assertEqual(items.id_of(wo.stable_hash("BronzeNails")), "BronzeNails")
        self.assertIsNone(items.id_of(12345))


class FindAndStockTest(unittest.TestCase):
    def setUp(self):
        data = (box(-2511, -1133, "piece_chest_wood", CHEST_1) + sign(-2509, -1133, "Caterpillar House")
                + portal(-2523, -1127, "Longest")
                + box(100, 100, "piece_chest", inv([("Iron", 120), ("Wood", 50)]))
                + box(300, 300, "piece_chest_barrel", inv([("Iron", 20)]))
                + box(5, 5, "piece_chest_private", inv([("Iron", 999)]))                 # personal: never shown
                + box(900, 900, "Cart", inv([("Wood", 25)])))
        self.found = wo.scan_bytes(data)
        self.found["tombstones"] = []

    def test_containers_and_signs_are_read(self):
        kinds = sorted(c["kind"] for c in self.found["containers"])
        self.assertEqual(kinds, ["barrel", "cart", "chest", "personal chest", "reinforced chest"])   # cargo too
        self.assertEqual(self.found["signs"][0]["text"], "Caterpillar House")

    def test_find(self):
        results = wo.find_items(self.found, "iron")
        self.assertEqual([(items.name_of(i), total) for i, total, _ in results], [("Iron", 140)])
        e = wo.render_find(self.found, "Iron", "Alheim")
        self.assertIn("**Iron**: 140 in 2 places", e["description"])
        self.assertIn("📦 **120** in a reinforced chest · x 100, z 100", e["description"])
        self.assertNotIn("999", e["description"])                                            # personal chest
        e = wo.render_find(self.found, "nails")
        self.assertIn("📦 **80** in a chest · by the sign “Caterpillar House” · near the Longest portal", e["description"])
        self.assertIn("No **Silver**", wo.render_find(self.found, "Silver")["description"])

    def test_stock(self):
        totals = {items.name_of(i): n for i, n in wo.stock(self.found).items()}
        self.assertEqual(totals["Iron"], 140)
        self.assertEqual(totals["Black metal scrap"], 72)
        e = wo.render_stock(self.found)
        self.assertTrue(e["description"].startswith("`   140` Iron"))
        self.assertIn("in 4 chests, barrels, carts and ships", e["footer"]["text"])           # not the personal one

    def test_autocomplete_names(self):
        names = wo.item_names(self.found)
        self.assertIn("Bronze nails", names)
        self.assertNotIn("unknown item", " ".join(names))

    def test_tombstones_in_find_not_in_stock(self):
        from test_world_objects import TOMBSTONE
        found = wo.scan_bytes(TOMBSTONE)
        self.assertEqual(found["containers"], [])           # the sample's items are cut short: not misread
        found["containers"] = [{"kind": "tombstone", "owner": "Ingrid", "x": 0, "z": 0, "y": 0,
                                "items": [(wo.stable_hash("Iron"), 7, None)]}]
        self.assertIn("7** in Ingrid's tombstone", wo.render_find(found, "Iron")["description"])
        self.assertEqual(wo.stock(found), {})

    def test_whole_world(self):
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(folder)
            with open(os.path.join(folder, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, 700000.0))
            for n, x in enumerate((10, 20)):
                with open(os.path.join(folder, f"{n}.chunk"), "wb") as f:
                    f.write(b"\x29\x00" + box(x, x, "piece_chest_wood", inv([("Iron", 5)])))
            found = wo.scan(d)
        self.assertEqual({items.name_of(i): n for i, n in wo.stock(found).items()}, {"Iron": 10})


if __name__ == "__main__":
    unittest.main()
