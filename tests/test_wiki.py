import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wiki  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

# Trimmed from the real pages on valheim.fandom.com.
DRAUGR = """{{infobox creature
| image 0star  = Draugr.png
| trophy       = Draugr trophy
| id           = Draugr
| location     = [[Swamp]]
| drops        = [[Draugr trophy]]<br/>[[Entrails]]
| health 0star = 100
| damage 0star = * Axe: 48 Slash, 15 Chop
* Bow: 48 Pierce
| health 1star = 200
| health 2star = 300
| veryweak      =
| weak          =
| resistant     = Fire
| immune        = Poison
| faction       =Undead}}

'''Draugr''' are aggressive [[creatures]] found in [[Swamp|Swamps]] or in [[Draugr village|Draugr Villages]].<ref>x</ref> They are ancient undead Vikings.

==Drops==
{{drop table|{{drop row|item=Draugr trophy|0star=10%}}}}
"""
MEAD = """<div class="infobox-tabber"><tabber>
Mead=
{{infobox item
| description = Protects against the cold.
| type        = Mead
| source      = [[Fermenter]]
| weight      = 1.0
| stack       = 10
| teleport    = Yes
| materials   = * [[Mead base: Frost resistance]] x1
| duration    = 600
| effect      = Resistant (0.5x) VS [[Frost]]
}}
|-|
Mead base=
{{infobox item
| title       = Mead base: Frost resistance
}}
</tabber></div>
'''Frost resistance mead''' is a [[Swamp]]-tier consumable item.

== Recipe ==
"""
SWORD = """{{infobox weapon
| title          = Iron sword
| description    = The straight line between life and death runs along the edge of this blade.
| type           = Sword
| source         = [[Forge]]
| crafting level = 2
| slash          = 55
|materials 1=* [[Wood]] x2
* [[Iron]] x20
* [[Leather scraps]] x3|materials 2=* [[Wood]] x1|stamina=10|block armor=21}}
The '''iron sword''' is the second [[Swords|sword]].
"""
JAM = """{{infobox item
| type = Food
| source = [[Cauldron]]
| materials =
* {{Item link|Raspberries|8}}
* [[Blueberries]] x6
| health = 14
| duration = 1200
}}
A '''Queen's jam''' is a [[Black Forest]]-tier [[food]]."""


def fields(embed):
    return {f["name"]: f["value"] for f in embed["fields"]}


class ParseTest(unittest.TestCase):
    def test_infobox(self):
        kind, box = wiki.infobox(DRAUGR)
        self.assertEqual(kind, "creature")
        self.assertEqual(box["health 1star"], "200")
        self.assertEqual(box["drops"], "[[Draugr trophy]]<br/>[[Entrails]]")
        self.assertEqual(wiki.infobox("no box here"), ("", {}))
        self.assertEqual(wiki.infobox("{{Infobox_location\n|type=Dungeon}}")[0], "location")

    def test_clean(self):
        self.assertEqual(wiki.clean("[[Swamp|Swamps]] and [[Iron]]<br/>x"), "Swamps and Iron\nx")
        self.assertEqual(wiki.clean("{{Item link|Raspberries|8}}, {{other|thing}}"), "Raspberries x8,")
        self.assertEqual(wiki.clean("<nowiki>Fireball</nowiki>: '''10''' Fire"), "Fireball: 10 Fire")
        self.assertEqual(wiki.clean("'''bold'''", bold=True), "**bold**")

    def test_intro(self):
        self.assertEqual(wiki.intro(DRAUGR), "**Draugr** are aggressive creatures found in Swamps or in Draugr "
                                             "Villages. They are ancient undead Vikings.")
        self.assertEqual(wiki.intro(MEAD), "**Frost resistance mead** is a Swamp-tier consumable item.")
        long = "'''X''' " + "is very long. " * 60
        self.assertLessEqual(len(wiki.intro(long)), 350)
        self.assertTrue(wiki.intro(long).endswith("."))


class CardTest(unittest.TestCase):
    def card(self, text, title="T"):
        return wiki.card({"title": title, "url": f"https://valheim.fandom.com/wiki/{title}", "image": "img.png",
                          "wikitext": text})

    def test_creature(self):
        e = self.card(DRAUGR, "Draugr")
        self.assertEqual(e["title"], "📖 Draugr")
        self.assertEqual(e["thumbnail"], {"url": "img.png"})
        f = fields(e)
        self.assertEqual(f["❤️ Health"], "100 / 200 / 300 (0–2★)")
        self.assertEqual(f["⚔️ Weaknesses"], "Resistant to: Fire\nImmune to: Poison")
        self.assertEqual(f["🎁 Drops"], "Draugr trophy, Entrails")
        self.assertEqual(f["🗺️ Found in"], "Swamp")
        self.assertIn("[Read more on the Valheim Wiki](https://valheim.fandom.com/wiki/Draugr)", e["description"])
        self.assertIn("CC BY-SA", e["footer"]["text"])

    def test_items_weapons_and_food(self):
        e = self.card(MEAD)
        self.assertTrue(e["description"].startswith("> *Protects against the cold.*"))
        self.assertEqual(fields(e)["⏳ Duration"], "10 min")
        self.assertEqual(fields(e)["✨ Effect"], "Resistant (0.5x) VS Frost")
        f = fields(self.card(SWORD))
        self.assertEqual(f["⚔️ Damage"], "55 slash")
        self.assertEqual(f["🧱 Materials"], "Wood x2, Iron x20, Leather scraps x3")
        self.assertEqual(f["🛡️ Block"], "21")
        f = fields(self.card(JAM))
        self.assertEqual(f["🧱 Materials"], "Raspberries x8, Blueberries x6")
        self.assertEqual(f["⏳ Duration"], "20 min")

    def test_page_without_infobox(self):
        e = self.card("'''Pine''' is a type of tree.", "Pine")
        self.assertEqual(e["fields"], [])
        self.assertTrue(e["description"].startswith("**Pine** is a type of tree."))


class LookupTest(unittest.TestCase):
    def setUp(self):
        wiki._cache.clear()
        self.calls = []

    def fake(self, params, timeout=10.0):
        self.calls.append(params["action"])
        if params["action"] == "opensearch":
            return [params["search"], ["Draugr", "Draugr Elite"], [], []]
        if params["action"] == "parse":
            if params["page"].lower() != "draugr":
                return {"error": {"code": "missingtitle"}}
            return {"parse": {"title": "Draugr", "wikitext": {"*": DRAUGR}}}
        return {"query": {"pages": {"260": {"fullurl": "https://valheim.fandom.com/wiki/Draugr",
                                            "thumbnail": {"source": "img.png"}}}}}

    def test_exact_then_best_match_and_cache(self):
        with mock.patch.object(wiki, "_get", self.fake):
            self.assertEqual(wiki.lookup("draugr")["title"], "Draugr")
            self.assertEqual(self.calls, ["parse", "query"])
            wiki.lookup("Draugr")                                   # cached
            self.assertEqual(len(self.calls), 2)
            found = wiki.lookup("drau")                             # no such page: the search's best match
            self.assertEqual(found["title"], "Draugr")
            self.assertEqual(wiki.search("dr"), ["Draugr", "Draugr Elite"])
            self.assertEqual(wiki.search("  "), [])

    def test_nothing_found(self):
        def empty(params, timeout=10.0):
            return [params.get("search"), [], [], []] if params["action"] == "opensearch" else \
                {"error": {"code": "missingtitle"}}
        with mock.patch.object(wiki, "_get", empty):
            self.assertIsNone(wiki.lookup("zzzz"))


@unittest.skipIf(discord is None, "discord.py not installed")
class ConfigTest(unittest.TestCase):
    def test_on_by_default(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            base = {"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}
            self.assertTrue(admin_bot.AdminBot(base, "S").wiki_enabled)
            self.assertFalse(admin_bot.AdminBot({**base, "wiki": False}, "S").wiki_enabled)


if __name__ == "__main__":
    unittest.main()
