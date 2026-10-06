import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import patch_notes as pn  # noqa: E402

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

# Shaped like Steam's GetNewsForApp items, with text from Iron Gate's real posts.
PATCH = {"gid": "1844751498224366", "title": "Patch 1.0.16", "date": 1790338722, "appid": 892970,
         "feedname": "steam_community_announcements", "tags": ["patchnotes"],
         "contents": "Further tweaks for you today, vikings!\n\n[b]Due to some internal restrictions, the patch "
                     "will require a few days.[/b]\n\n[h3]Patch Notes:[/h3]\n* Fixed Child of Odin achievement "
                     "not unlocking correctly\n* Updated localization \n* Tessellation is now disabled in the "
                     "&ldquo;low&rdquo; graphics preset"}
HOTFIX = {"gid": "1843481262695240", "title": "Hotfix 1.0.10 & 1.0.12", "date": 1789132142, "appid": 892970,
          "tags": ["patchnotes", "mod_reviewed"],
          "contents": "[p]Hello vikings![/p][h2]Patch 1.0.10 – All Platforms[/h2][p]* The heavy armour can now be "
                      "repaired\n* Punching no longer removes snow\n[/p][p][i]Steam only:[/i] see "
                      "[url=https://valheim.com/support]support[/url].[/p][img]{STEAM_CLAN_IMAGE}/x.png[/img]"}
BLOG = {"gid": "1845383656377310", "title": "Word from the Devs: What a Month!", "date": 1790769511,
        "tags": None, "contents": "Greetings Vikings!"}
PTB = {"gid": "1846000000000001", "title": "Public Test Patch 1.1.2", "date": 1790900000,
       "tags": ["patchnotes"], "contents": "[list][*]Testing things[/list]"}
FEED = [PTB, BLOG, PATCH, HOTFIX]        # newest first, as Steam sends it


class PatchNotesTest(unittest.TestCase):
    def test_only_patches(self):
        self.assertEqual([i["title"] for i in FEED if pn.is_patch(i)], ["Patch 1.0.16", "Hotfix 1.0.10 & 1.0.12"])
        self.assertTrue(pn.is_patch(PTB, public_test=True))
        self.assertTrue(pn.is_patch({"title": "Hotfix 0.218.21", "tags": []}))    # untagged, but titled
        self.assertFalse(pn.is_patch({"title": "Valheim 1.0 Has Arrived!", "tags": None}))

    def test_markdown(self):
        md = pn.to_markdown(PATCH["contents"])
        self.assertIn("**Due to some internal restrictions, the patch will require a few days.**", md)
        self.assertIn("**Patch Notes:**\n• Fixed Child of Odin achievement not unlocking correctly\n"
                      "• Updated localization\n• Tessellation is now disabled in the “low” graphics preset", md)
        md = pn.to_markdown(HOTFIX["contents"])
        self.assertIn("**Patch 1.0.10 – All Platforms**", md)
        self.assertIn("• The heavy armour can now be repaired\n• Punching no longer removes snow", md)
        self.assertIn("*Steam only:* see [support](https://valheim.com/support).", md)
        self.assertNotIn("[", md.replace("[support]", ""))
        self.assertNotIn("STEAM_CLAN_IMAGE", md)
        self.assertEqual(pn.to_markdown(PTB["contents"]), "• Testing things")

    def test_embed(self):
        e = pn.embed(PATCH)
        self.assertEqual(e["title"], "🛠️ Patch 1.0.16")
        self.assertEqual(e["url"], "https://store.steampowered.com/news/app/892970/view/1844751498224366")
        self.assertTrue(e["description"].endswith(f"[On Steam]({e['url']})"))
        self.assertEqual(e["timestamp"][:10], "2026-09-25")
        self.assertTrue(pn.embed(PTB)["title"].startswith("🧪 "))
        long = dict(PATCH, contents="\n".join(f"* Fixed bug number {n} in the Deep North" for n in range(300)))
        e = pn.embed(long)
        self.assertLessEqual(len(e["description"]), pn.LIMIT + 120)
        self.assertIn("Read the full patch notes on Steam", e["description"])
        self.assertIn("in the Deep North\n\n[Read", e["description"])                # cut at a line break

    def test_new_patches_oldest_first(self):
        self.assertEqual([i["gid"] for i in pn.new_patches(FEED, set())], [HOTFIX["gid"], PATCH["gid"]])
        self.assertEqual(pn.new_patches(FEED, {HOTFIX["gid"], PATCH["gid"]}), [])
        self.assertEqual([i["gid"] for i in pn.new_patches(FEED, {HOTFIX["gid"], PATCH["gid"]}, True)], [PTB["gid"]])


@unittest.skipIf(discord is None, "discord.py not installed")
class PatchNotesBotTest(unittest.TestCase):
    def make_bot(self, d, extra=None):
        import admin_bot
        b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d,
                                **(extra or {})}, "S")
        b.attach(db_path=os.path.join(d, "s.db"))
        self.posts = []

        async def post(embed):
            self.posts.append(embed["title"])
            return True
        b._post_patch = post
        return b

    def test_first_check_remembers_then_posts_only_new_ones(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.make_bot(d)
            self.assertEqual(asyncio.run(b._check_patches([PATCH, HOTFIX])), [])     # installing posts nothing
            self.assertEqual(self.posts, [])
            newer = dict(PATCH, gid="1846500000000000", title="Patch 1.0.17", date=1791000000)
            posted = asyncio.run(b._check_patches([newer, PTB, BLOG, PATCH, HOTFIX]))
            self.assertEqual([i["title"] for i in posted], ["Patch 1.0.17"])         # not the public test
            self.assertEqual(self.posts, ["🛠️ Patch 1.0.17"])
            self.assertEqual(asyncio.run(b._check_patches([newer, PATCH])), [])      # once only
            self.assertIn("1846500000000000", json.loads(b._meta("patch:seen")))

    def test_failed_post_is_tried_again(self):
        with tempfile.TemporaryDirectory() as d:
            b = self.make_bot(d)
            asyncio.run(b._check_patches([HOTFIX]))
            calls = []

            async def down(embed):
                calls.append(embed["title"])
                return False
            b._post_patch = down
            self.assertEqual(asyncio.run(b._check_patches([PATCH, HOTFIX])), [])
            self.assertEqual(calls, ["🛠️ Patch 1.0.16"])
            b._post_patch = self.make_bot(d)._post_patch
            self.assertEqual([i["title"] for i in asyncio.run(b._check_patches([PATCH, HOTFIX]))], ["Patch 1.0.16"])

    def test_config(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(self.make_bot(d).patch_notes_on)                          # on by default
            self.assertFalse(self.make_bot(d, {"patch_notes": False}).patch_notes_on)
            b = self.make_bot(d, {"patch_notes": {"channel_id": "77", "public_test": True}})
            self.assertEqual((b.patch_notes_on, b.patch_channel, b.patch_public_test), (True, 77, True))

    def test_falls_back_to_huginns_feed(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            sent = []
            b.attach(db_path=os.path.join(d, "s.db"), post_embed=lambda e, kind="titles": sent.append(kind) or True)

            async def no_runestone():
                return None
            b._runestone = no_runestone
            self.assertTrue(asyncio.run(b._post_patch(pn.embed(PATCH))))
            self.assertEqual(sent, ["patch_notes"])


if __name__ == "__main__":
    unittest.main()
