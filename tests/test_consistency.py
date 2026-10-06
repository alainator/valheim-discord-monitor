"""Everything that describes the bot to its users has to agree with what the bot actually does.

When a command, title, honor or stat channel is added, renamed or removed, these tests fail until
every place that mentions it is updated: the README, the channel topics and channel guide that
/odin setup posts, the welcome DM, the starter rules, the /valheim progress guide, the command
home channels and the stat channels. See CONTRIBUTING.md for the checklist.
"""
import asyncio
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import community  # noqa: E402
import fch_progress  # noqa: E402
import server_layout  # noqa: E402
import stat_channels  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def command_tree():
    """{group: {command or "sub command": description}} from the real command tree."""
    import admin_bot
    with tempfile.TemporaryDirectory() as d:
        bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")

        async def build():
            client = bot._build_client()
            bot._register_commands(client.tree)
            out = {}
            for group in client.tree.get_commands():
                cmds = {}
                for c in group.commands:
                    if hasattr(c, "commands"):
                        for leaf in c.commands:
                            cmds[f"{c.name} {leaf.name}"] = leaf.description
                    else:
                        cmds[c.name] = c.description
                out[group.name] = cmds
            return out
        return asyncio.run(build())


# "/muninn stats", "/odin honor give", "/valheim"; not paths like /home/valheim or /app/...
REF = re.compile(r"(?<![\w/.~-])/(valheim|muninn|warcouncil|odin)(?:\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?)?\b")


def refs(text):
    return [(m.group(1), m.group(2), m.group(3)) for m in REF.finditer(text)]


@unittest.skipIf(discord is None, "discord.py not installed")
class CommandsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = command_tree()

    def check_text(self, label, text):
        bad = []
        for group, word, word2 in refs(text):
            cmds = self.tree.get(group, {})
            if word is None or word in cmds or (word2 and f"{word} {word2}" in cmds) or \
                    any(c.startswith(word + " ") for c in cmds):
                continue
            # A plain English word after the group ("/muninn top board") is fine only if it
            # isn't trying to name a command, i.e. it's followed by text. Treat unknown words
            # as stale references: that's what this test is for.
            bad.append(f"/{group} {word}")
        self.assertEqual(sorted(set(bad)), [], f"{label} mentions commands that don't exist")

    def test_texts_only_mention_real_commands(self):
        import admin_bot
        texts = {
            "channel topics": " ".join(ch[3] or "" for c in server_layout.TEMPLATE for ch in c["channels"]),
            "command guide intros": " ".join(g[3] for g in admin_bot.GUIDES),
            "welcome DM": admin_bot.WELCOME_DM,
            "starter rules": admin_bot.DEFAULT_RULES,
            "progress guide": " ".join(g[2] for g in fch_progress.FIND_GUIDES.values()) + fch_progress.FIND_TIPS,
            "host/README.md": read("host/README.md"),
        }
        for label, text in texts.items():
            self.check_text(label, text)

    def test_readme_mentions_only_real_commands(self):
        # Some lines are about the old names on purpose ("/valheim stats ... are gone"):
        # those are skipped.
        old = ("renamed", "used to be", "are gone", "split into groups")
        lines = [ln for ln in read("README.md").splitlines() if not any(w in ln for w in old)]
        self.check_text("README.md", "\n".join(lines))

    def test_every_command_is_documented(self):
        readme = read("README.md")
        missing = []
        for group, cmds in self.tree.items():
            for name in cmds:
                if " " in name:                        # a subgroup command, e.g. "honor give"
                    sub, leaf = name.split()
                    ok = any(f"/{group} {sub}" in ln and leaf in ln for ln in readme.splitlines())
                else:
                    ok = f"/{group} {name}" in readme
                if not ok:
                    missing.append(f"/{group} {name}")
        self.assertEqual(missing, [], "commands missing from README.md")

    def test_every_group_has_a_pinned_guide(self):
        import admin_bot
        self.assertEqual(sorted(g[0] for g in admin_bot.GUIDES), sorted(self.tree))

    def test_public_commands_have_a_home_channel_named_in_its_topic(self):
        """/muninn and /warcouncil replies are public: each command belongs in its channel
        (COMMAND_PLACES) and that channel's topic tells people it's there."""
        import admin_bot
        topics = {ch[0]: ch[3] for c in server_layout.TEMPLATE for ch in c["channels"]}
        for group, slot in (("muninn", "bots"), ("warcouncil", "plans")):
            for name in self.tree[group]:
                self.assertEqual(admin_bot.COMMAND_PLACES.get(name), slot, f"/{group} {name} needs a home channel")
                self.assertIn(name, topics[slot], f"the #{slot} topic should mention /{group} {name}")
        for name in admin_bot.COMMAND_PLACES:
            self.assertTrue(name in self.tree["muninn"] or name in self.tree["warcouncil"],
                            f"COMMAND_PLACES has {name}, which isn't a command")


class RolesAndStatsTest(unittest.TestCase):
    def test_every_title_has_a_stat_channel(self):
        for cat, (name, _, _) in community.TITLES.items():
            key = f"title_{cat}"
            self.assertIn(key, stat_channels.STATS, f"no stat channel for the {name} title")
            self.assertIn(key, stat_channels.TITLE_LABELS, f"no label for the {name} stat channel")

    def test_every_stat_channel_is_documented(self):
        readme = read("README.md")
        for key in stat_channels.STATS:
            self.assertTrue(f"`{key}`" in readme, f"stat channel {key} isn't in the README's stat channel table")

    def test_every_role_is_in_roles_and_names(self):
        readme = read("README.md")
        section = readme[readme.index("### Roles & names"):]
        section = section[:section.index("\n### ", 5)]
        names = [n for n, _, _ in community.TITLES.values()] + [community.AWAY_ROLE[0], "Skadi", "Odin",
                                                                  "In Valheim", "Huginn", "Muninn"]
        for name in names:
            self.assertTrue(f"**{name}**" in section, f"{name} is missing from the README's Roles & names table")

    def test_every_honor_is_documented(self):
        readme = read("README.md")
        for name, *_ in community.HONORS:
            self.assertTrue(f"**{name}**" in readme, f"the {name} honor isn't in the README")

    def test_every_achievement_list_is_documented(self):
        readme = read("README.md")
        for title, emoji in fch_progress.SECTIONS.values():
            self.assertTrue(emoji in readme, f"the {title} list isn't in the README's progress table")


class LayoutTest(unittest.TestCase):
    def test_old_topics_are_old(self):
        """OLD_TOPICS lists wording to replace; the current topic must never be in it, or the
        bot would 'update' a channel to the same text forever."""
        for c in server_layout.TEMPLATE:
            for key, _kind, _name, topic, _aliases, _flags in c["channels"]:
                self.assertNotIn(topic, server_layout.OLD_TOPICS.get(key, ()), f"#{key}'s topic is in OLD_TOPICS")

    def test_topics_fit(self):
        for c in server_layout.TEMPLATE:
            for key, _kind, _name, topic, _aliases, _flags in c["channels"]:
                self.assertLessEqual(len(topic or ""), 1024, f"#{key}'s topic is too long for Discord")



class RefreshTest(unittest.TestCase):
    def test_new_stat_channels_and_titles(self):
        self.assertEqual(stat_channels.name_for("away", {}, titles={"away": ("Óðr", 2, 14)}),
                         "🛶 Óðr (away 14+ days): 2")
        self.assertEqual(stat_channels.name_for("bounty_hunter", {}, titles={"bounty_hunter": ("Skadi", ["Ingrid"])}),
                         "🎯 Skadi (bounty hunter): Ingrid")
        self.assertEqual(stat_channels.name_for("bounty_hunter", {}, titles={"bounty_hunter": ("Skadi", [])}),
                         "🎯 Skadi (bounty hunter): —")
        self.assertIsNone(stat_channels.name_for("away", {}, titles={}))          # feature off: left alone
        e = community.render_titles({}, hunters=("Skadi", ["42"]))
        self.assertIn("**Skadi**: <@42>", e["description"])

    @unittest.skipIf(discord is None, "discord.py not installed")
    def test_old_default_topics_are_updated_at_start_up(self):
        import admin_bot
        from types import SimpleNamespace

        class Channel:
            def __init__(self, cid, topic):
                self.id, self.topic, self.edits = cid, topic, []

            async def edit(self, topic=None, reason=None):
                self.topic = topic
                self.edits.append(topic)
        old = server_layout.OLD_TOPICS["bots"][-1]
        chans = {1: Channel(1, old), 2: Channel(2, "Our own words, keep them"), 3: Channel(3, "")}
        with tempfile.TemporaryDirectory() as d:
            b = admin_bot.AdminBot({"token": "x", "channel_id": "9", "admin_user_ids": [5], "save_dir": d}, "S")
            b.attach(db_path=os.path.join(d, "s.db"))
            for slot, cid in (("bots", 1), ("plans", 2), ("lore", 3)):
                b._meta(f"layout:ch:{slot}", cid)
            n = asyncio.run(b._refresh_topics(SimpleNamespace(get_channel=chans.get)))
        current = {ch[0]: ch[3] for c in server_layout.TEMPLATE for ch in c["channels"]}
        self.assertEqual(n, 1)
        self.assertEqual(chans[1].topic, current["bots"])                   # old default: updated
        self.assertEqual(chans[2].edits, [])                                # written by hand: kept
        self.assertEqual(chans[3].edits, [])                                # empty: /odin setup's job


if __name__ == "__main__":
    unittest.main()
