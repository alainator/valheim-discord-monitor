import hashlib
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fch_progress as fch  # noqa: E402


def s(text):
    """A .NET string: 7-bit length, then UTF-8."""
    b = text.encode()
    n, out = len(b), bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + b


def sdict(d):
    return struct.pack("<i", len(d)) + b"".join(s(k) + struct.pack("<f", v) for k, v in d.items())


def slist(items):
    return struct.pack("<i", len(items)) + b"".join(s(x) for x in items)


def stat_set(stats=None, kills=None, crafted=None, built=None, picked=None, pickables=None):
    vals = [0.0] * 205
    for i, v in (stats or {}).items():
        vals[i] = v
    out = struct.pack("<205f", *vals)
    out += sdict({"Alheim": 3600.0}) + sdict({}) + sdict({})
    out += struct.pack("<i", 5) + sdict(kills or {}) + b"".join(sdict({}) for _ in range(4))
    out += sdict(picked or {}) + sdict(crafted or {}) + sdict(pickables or {}) + sdict({}) + sdict(built or {})
    return out


def make_fch(**any_set):
    payload = struct.pack("<iii", 46, 205, 10)
    hard = {"kills": {"$enemy_eikthyr": 1.0}}
    for i in range(10):
        payload += stat_set(**(any_set if i in (1, 6) else hard if i == 7 else {}))
    payload += b"\x00" * 64                                      # world/map data
    payload += s("Ingrid") + struct.pack("<q", 12345)            # player blob start
    payload += slist([f"$item_recipe{i}" for i in range(120)])   # known recipes (>100)
    payload += struct.pack("<i", 1) + s("$piece_workbench") + struct.pack("<i", 3)
    payload += slist(["$item_wood"]) + slist([]) + slist([]) + slist(["TrophyBoar"])
    return struct.pack("<i", len(payload)) + payload + struct.pack("<i", 64) + hashlib.sha512(payload).digest()


class FchTest(unittest.TestCase):
    def setUp(self):
        self.raw = make_fch(
            stats={56: 3.0, 58: 1.0, 193: 1.0},                    # killed by enemies, a fall, a fir tree
            kills={"$enemy_greydwarf": 12.0, "$enemy_eikthyr": 1.0, "$enemy_goblin_hildir": 1.0},
            crafted={"$item_axe_flint": 1.0, "$item_club": 1.0, "$item_deerstew": 2.0},
            built={"$piece_workbench": 4.0, "$piece_mystery": 1.0},
            picked={"$item_trophy_boar": 1.0},
            pickables={"$animal_fish1": 2.0})
        self.save = fch.parse_bytes(self.raw)

    def test_parses_the_layout(self):
        self.assertEqual(self.save["version"], 46)
        self.assertEqual(len(self.save["sets"]), 10)
        self.assertEqual(len(self.save["recipes"]), 120)
        self.assertEqual(self.save["stations"], {"$piece_workbench": 3})
        self.assertEqual(self.save["trophies"], ["TrophyBoar"])
        self.assertEqual(fch.worlds(self.save), ["Alheim"])

    def test_report(self):
        r = {sec["key"]: sec for sec in fch.report(self.save)}
        self.assertEqual(list(r), list(fch.SECTIONS))
        self.assertEqual(r["crafted"]["done"], {"axe_flint": 1, "deerstew": 2})       # the club is hand-made
        self.assertEqual(r["weapons"]["done"], {"axe_flint": 1, "club": 1})
        self.assertIn("deerstew", r["cooked"]["done"])
        self.assertEqual(r["built"]["done"], {"workbench": 4})
        self.assertEqual(r["built"]["extra"], [])                  # unknown pieces aren't flagged (ignored)
        self.assertEqual(set(r["deaths"]["done"]), {"EnemyHit", "Fall"})
        self.assertEqual(len(r["deaths"]["missing"]), 6)
        self.assertEqual(r["tree-deaths"]["done"], {"Fir": 1})
        self.assertEqual(r["bosses"]["done"], {"eikthyr": 1})      # Normal set
        self.assertEqual(r["bosses-hard"]["done"], {"eikthyr": 1})  # Hard set
        self.assertEqual(r["enemies-hard"]["done"], {})
        self.assertEqual(r["minibosses"]["done"], {"goblin_hildir": 1})
        self.assertEqual(r["fishing"]["done"], {"Perch": 2})
        self.assertIn("Pufferfish", r["fishing"]["missing"])
        self.assertEqual(r["trophies"]["done"], {"trophy_boar": 1})
        self.assertEqual(r["bosses"]["total"], 8)

    def test_only_and_text(self):
        only = fch.report(self.save, ["fishing"])
        self.assertEqual([sec["key"] for sec in only], ["fishing"])
        text = fch.render_text(self.save, only, full=True, name="Ingrid")
        self.assertIn("== Fish caught: 1 / 12 ==", text)
        self.assertIn("[x] Perch", text)
        self.assertIn("[ ] Pike", text)

    def test_not_a_character_file(self):
        for junk in (b"", b"hello world, not a save", b"\xff" * 64):
            with self.assertRaises((ValueError, struct.error, IndexError, UnicodeDecodeError)):
                fch.parse_bytes(junk)

    def test_command_line(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Ingrid.fch")
            with open(path, "wb") as f:
                f.write(self.raw)
            out = io.StringIO()
            with redirect_stdout(out):
                fch.main([path, "-o", "bosses,fishing"])
        self.assertIn("== Bosses (Normal+): 1 / 8 ==", out.getvalue())
        self.assertIn("== Fish caught: 1 / 12 ==", out.getvalue())

    def test_embed(self):
        try:
            import admin_bot
        except ImportError:
            self.skipTest("discord.py not installed")
        sections = fch.report(self.save, ["bosses", "fishing"])
        e = admin_bot.progress_embed("Ingrid", sections, ["Alheim"])
        self.assertEqual(e["title"], "📜 Achievement progress: Ingrid")
        self.assertIn("**2/20**", e["description"])
        self.assertEqual(e["fields"][0]["name"], "👑 Bosses (Normal+): 1/8")
        self.assertTrue(e["fields"][0]["value"].startswith("Missing: "))
        self.assertIn("+", e["fields"][1]["value"])                     # more fish missing than shown
        public = admin_bot.progress_embed("Ingrid", sections, [], full_summary=False)
        self.assertNotIn("Missing", str(public["fields"]))               # the shared summary is counts only


    def test_where_to_find_it_guide(self):
        try:
            import asyncio
            import admin_bot
            from types import SimpleNamespace
        except ImportError:
            self.skipTest("discord.py not installed")
        overview = admin_bot.find_save_embed()
        self.assertIn("Pick your platform below", overview["description"])
        self.assertIn("characters_local", admin_bot.find_save_embed("windows")["description"])
        self.assertIn("~/.config/unity3d/IronGate/Valheim/characters_local/",
                      admin_bot.find_save_embed("linux")["description"])
        self.assertIn("/home/deck/.config", admin_bot.find_save_embed("deck")["description"])
        self.assertIn("Cmd+Shift+G", admin_bot.find_save_embed("mac")["description"])
        self.assertIn("only works on PC", admin_bot.find_save_embed("console")["description"])
        for key in fch.FIND_GUIDES:
            self.assertIn(".fch.old", admin_bot.find_save_embed(key)["description"])   # the tips come along
            self.assertLessEqual(len(admin_bot.find_save_embed(key)["description"]), 4096)

        async def press():
            view = admin_bot.find_save_view()
            self.assertEqual([b.label for b in view.children],
                             ["Windows", "Linux", "Steam Deck", "macOS", "Xbox / Game Pass"])
            edits = []

            async def edit_message(embed=None, view=None):
                edits.append((embed, view))
            await view.children[1].callback(SimpleNamespace(response=SimpleNamespace(edit_message=edit_message)))
            return edits
        (embed, view), = asyncio.run(press())
        self.assertEqual(embed.title, "🐧 Finding your character on Linux")
        self.assertEqual(view.children[1].style.name, "primary")                # the chosen one stands out


if __name__ == "__main__":
    unittest.main()
