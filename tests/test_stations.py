import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import world_objects as wo  # noqa: E402
from test_bases import piece  # noqa: E402

CLOCK = 945272.0


def k(name):
    return struct.pack("<I", wo.stable_hash(name))


def station(x, z, prefab, floats=(), ints=(), longs=(), strings=()):
    """A station laid out like the real ones: floats, ints, longs, then strings, each
    section a count and key/value pairs."""
    out = b"\x3f\x88\x10\x92\xc7\x07\x00\x00\xf4\x11" + struct.pack("<3f", x, 30.0, z) + k(prefab) + b"\x19\x82"
    out += b"\x01" + k("creator") + struct.pack("<q", 111)          # the creator is a long too, in real saves
    if floats:
        out += bytes([len(floats)]) + b"".join(k(a) + struct.pack("<f", v) for a, v in floats)
    if ints:
        out += bytes([len(ints)]) + b"".join(k(a) + struct.pack("<i", v) for a, v in ints)
    if longs:
        out += bytes([len(longs)]) + b"".join(k(a) + struct.pack("<q", v) for a, v in longs)
    if strings:
        out += bytes([len(strings)]) + b"".join(k(a) + bytes([len(v.encode())]) + v.encode() for a, v in strings)
    return out + b"\x00" * 24


def smelter(x, z, fuel, queue, prefab="smelter", bake=0.0, slots=12):
    """Like the real ones: every itemN key is there, the unused ones empty."""
    items = [(f"item{n}", queue[n] if n < len(queue) else "") for n in range(max(slots, len(queue)))]
    floats = ([("fuel", fuel)] if prefab != "charcoal_kiln" else []) + [("bakeTimer", bake), ("accTime", 0.5)]
    return station(x, z, prefab, floats, [("queued", len(queue))], [("StartTime", int(CLOCK * 1e7))], items)


def fermenter(x, z, content="", started=None):
    ticks = int(started * 1e7) if started is not None else -8998576875966038016   # what an empty one keeps
    return station(x, z, "fermenter", longs=[("StartTime", ticks)], strings=[("Content", content)])


def hive(x, z, level):
    return station(x, z, "piece_beehive", ints=[("level", level)])


def world():
    return (piece(580, 460, "bed", 111, (111, "Ingrid"))
            + smelter(592, 459, 0.0, ["IronScrap"])
            + smelter(594, 462, 0.0, ["CopperOre", "CopperOre", "TinOre"] + ["IronScrap"] * 9, bake=15.0)
            + smelter(595, 466, 3.0, [])
            + smelter(589, 451, 4.0, ["CopperOre", "CopperOre"])
            + smelter(584, 459, 0.0, [], prefab="charcoal_kiln")
            + smelter(596, 473, 6.0, [], prefab="blastfurnace", slots=6)
            + fermenter(585, 496, "MeadBaseHealthMinor", CLOCK - 3000)            # ready
            + fermenter(585, 493, "MeadBaseStaminaMinor", CLOCK - 1200)           # 20 minutes to go
            + fermenter(585, 489)                                                 # empty
            + hive(592, 498, 4) + hive(594, 500, 4) + hive(594, 502, 0)
            # far away, on its own: a smelter with ore and no coal, built by Ingrid too
            + smelter(-1743, 991, 0.0, ["CopperOre"] * 8 + ["TinOre"]))

THERE = "Ingrid's base (x -1743, z 991)"
HUB = "Ingrid's base (x 590, z 478)"


class StationStateTest(unittest.TestCase):
    def setUp(self):
        self.found = wo.scan_bytes(world())
        self.found["clock"] = CLOCK
        self.by = {(p["kind"], round(p["x"]), round(p["z"])): p for p in self.found["pieces"]}

    def test_smelters(self):
        s = self.by[("smelter", 594, 462)]
        self.assertEqual(s["queue"], ["CopperOre", "CopperOre", "TinOre"] + ["IronScrap"] * 9)
        self.assertEqual((s["fuel"], s["busy"]), (0.0, True))
        self.assertEqual(self.by[("smelter", 595, 466)]["queue"], [])          # the empty itemN keys aren't items
        self.assertEqual(self.by[("kiln", 584, 459)]["fuel"], 0.0)
        self.assertEqual(self.by[("blast furnace", 596, 473)]["fuel"], 6.0)

    def test_fermenters_and_hives(self):
        self.assertEqual(self.by[("fermenter", 585, 496)]["content"], "MeadBaseHealthMinor")
        self.assertAlmostEqual(self.by[("fermenter", 585, 496)]["started"], CLOCK - 3000, places=0)
        empty = [p for p in self.found["pieces"] if p["kind"] == "fermenter" and not p["content"]]
        self.assertEqual(len(empty), 1)
        self.assertIsNone(empty[0]["started"])                            # the leftover StartTime is ignored
        self.assertEqual(sorted(p["level"] for p in self.found["pieces"] if p["kind"] == "beehive"), [0, 4, 4])

    def test_a_long_queue(self):
        """A windmill holds 50 barley: its names run past the usual 512-byte window."""
        found = wo.scan_bytes(smelter(0, 0, 0.0, ["Barley"] * 50, prefab="windmill"))
        self.assertEqual(found["pieces"][0]["queue"], ["Barley"] * 50)

    def test_beehives_alone_arent_a_base(self):
        found = wo.scan_bytes(hive(0, 0, 4) + hive(3, 0, 4))
        self.assertEqual(wo.bases(found)[0], [])


class StationReportTest(unittest.TestCase):
    def setUp(self):
        self.found = wo.scan_bytes(world())
        self.found["clock"] = CLOCK

    def test_report(self):
        r = wo.station_report(self.found)
        self.assertEqual([place for place, _ in r["places"]], [HUB, THERE])          # same name: where, too
        self.assertEqual(r["no_fuel"], [(HUB, "smelter", "coal"), (HUB, "smelter", "coal"), (THERE, "smelter", "coal")])
        self.assertEqual(r["collect"], {"honey": (8, 2)})
        self.assertEqual([p["content"] for _, p in r["mead"]], ["MeadBaseHealthMinor"])
        self.assertEqual([(p["content"], round(left)) for _, p, left in r["brewing"]], [("MeadBaseStaminaMinor", 1200)])

    def test_render(self):
        e = wo.render_stations(self.found, "Alheim")
        d = e["description"]
        self.assertEqual(e["title"], "🏭 Stations in Alheim")
        self.assertIn(f"⚠️ **Needs coal:** 3 with something waiting and no coal ({HUB} ×2, {THERE})", d)
        self.assertIn("🍯 **Ready to collect:** 8 honey in 2 hives", d)
        self.assertIn(f"🍺 **Ready to tap:** 1 fermenter ({HUB})", d)
        self.assertIn(f"**{HUB}**\n🔥 4 smelters: 15 waiting (10 scrap iron, 4 copper ore, 1 tin ore) · "
                      "⚠️ 2 with no coal · 7 coal loaded\n🔥 Blast furnace: empty · 6 coal loaded\n🔥 Kiln: empty\n"
                      "🍺 3 fermenters: 1 ready (mead base: minor healing) · 1 fermenting (mead base: minor stamina), "
                      "the first ready in about 20 min\n🐝 3 beehives: 8 honey to collect", d)
        self.assertIn(f"**{THERE}**\n🔥 Smelter: 9 waiting (8 copper ore, 1 tin ore) · ⚠️ no coal", d)

    def test_without_the_clock(self):
        """scan_bytes alone has no clock: fermenters are "fermenting", never wrongly ready."""
        found = wo.scan_bytes(world())
        r = wo.station_report(found)
        self.assertEqual(r["mead"], [])
        self.assertEqual(len(r["brewing"]), 2)
        self.assertIn("2 fermenting", wo.render_stations(found)["description"])

    def test_nothing(self):
        self.assertIn("No smelters", wo.render_stations(wo.scan_bytes(b""))["description"])

    def test_whole_world_has_the_clock(self):
        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "worlds_local", "Alheim")
            os.makedirs(folder)
            with open(os.path.join(folder, "_main.1.db2"), "wb") as f:
                f.write(struct.pack("<id", 41, CLOCK))
            with open(os.path.join(folder, "0.chunk"), "wb") as f:
                f.write(b"\x29\x00" + world())
            found = wo.scan(d)
        self.assertEqual(found["clock"], CLOCK)
        self.assertEqual(len(wo.station_report(found)["mead"]), 1)



def read(data):
    found = wo.scan_bytes(data)
    found["clock"] = CLOCK
    return found


HOME = piece(580, 460, "bed", 111, (111, "Ingrid"))


class StationAlertsTest(unittest.TestCase):
    def test_the_first_read_is_a_baseline(self):
        embed, state = wo.station_alerts(read(world()), None)
        self.assertIsNone(embed)
        self.assertEqual(len(state["fuel"]), 3)
        self.assertIsNone(wo.station_alerts(read(world()), state)[0])        # nothing new: nothing posted

    def test_new_things_are_posted_once(self):
        quiet = HOME + smelter(592, 459, 4.0, []) + fermenter(585, 496) + hive(592, 498, 1)
        _, state = wo.station_alerts(read(quiet), None)
        busy = (HOME + smelter(592, 459, 0.0, ["CopperOre", "CopperOre"]) + fermenter(585, 496, "MeadBaseTasty", CLOCK - 2500)
                + hive(592, 498, 4))
        embed, state = wo.station_alerts(read(busy), state)
        self.assertEqual(embed["title"], "🏭 The stations need you")
        self.assertEqual(embed["description"].split("\n"), [
            "🍺 **Mead ready** at Ingrid's base: 1 fermenter (mead base: tasty)",
            "🍯 **Hives full** at Ingrid's base: 1 hive, 4 honey to collect",
            "⚠️ **Out of coal** at Ingrid's base: a smelter with 2 copper ore waiting"])
        self.assertIsNone(wo.station_alerts(read(busy), state)[0])

    def test_again_after_it_was_dealt_with(self):
        dry = HOME + smelter(592, 459, 0.0, ["TinOre"]) + fermenter(585, 496, "MeadBaseTasty", CLOCK - 2500) + hive(592, 498, 4)
        _, state = wo.station_alerts(read(dry), None)
        fixed = HOME + smelter(592, 459, 5.0, ["TinOre"]) + fermenter(585, 496) + hive(592, 498, 0)
        self.assertIsNone(wo.station_alerts(read(fixed), state)[0])
        _, state = wo.station_alerts(read(fixed), state)
        embed, _ = wo.station_alerts(read(dry.replace(struct.pack("<q", int((CLOCK - 2500) * 1e7)),
                                                      struct.pack("<q", int((CLOCK - 2600) * 1e7)))), state)
        self.assertEqual(len(embed["description"].split("\n")), 3)                 # all three again

    def test_a_new_batch_is_new(self):
        first = HOME + fermenter(585, 496, "MeadBaseTasty", CLOCK - 2500)
        _, state = wo.station_alerts(read(first), None)
        second = HOME + fermenter(585, 496, "MeadBaseTasty", CLOCK - 2450)       # tapped and refilled between saves
        self.assertIn("Mead ready", wo.station_alerts(read(second), state)[0]["description"])

    def test_only_what_is_wanted(self):
        _, state = wo.station_alerts(read(HOME), None)
        embed, state2 = wo.station_alerts(read(world()), state, wanted=("honey",))
        self.assertEqual([l.split("**")[1] for l in embed["description"].split("\n")], ["Hives full"])
        self.assertEqual(len(state2["fuel"]), 3)                  # still tracked: turning it on later isn't a flood
        self.assertIsNone(wo.station_alerts(read(world()), state, wanted=())[0])


try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


@unittest.skipIf(discord is None, "discord.py not installed")
class StationAlertsBotTest(unittest.TestCase):
    def bot(self, d, cfg=None, ok=True):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d, **(cfg or {})}, "S")
        self.posts = []
        b.attach(db_path=os.path.join(d, "s.db"),
                 post_embed=lambda e, kind="titles": self.posts.append((kind, e["description"])) or ok)
        return b

    def test_posts_once_after_a_baseline(self):
        import asyncio
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d)
            asyncio.run(b._station_alerts(read(HOME)))
            asyncio.run(b._station_alerts(read(world())))
            asyncio.run(b._station_alerts(read(world())))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0][0], "stations")
        self.assertIn("Out of coal", self.posts[0][1])

    def test_a_failed_post_is_tried_again(self):
        import asyncio
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d, ok=False)
            asyncio.run(b._station_alerts(read(HOME)))
            asyncio.run(b._station_alerts(read(world())))
            asyncio.run(b._station_alerts(read(world())))
        self.assertEqual(len(self.posts), 2)

    def test_turned_off(self):
        import asyncio
        with tempfile.TemporaryDirectory() as d:
            b = self.bot(d, {"station_alerts": False})
            self.assertEqual(b.station_alerts, ())
            asyncio.run(b._station_alerts(read(HOME)))
            asyncio.run(b._station_alerts(read(world())))
            b2 = self.bot(d, {"station_alerts": {"fuel": False}})
            self.assertEqual(b2.station_alerts, ("mead", "honey"))
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
