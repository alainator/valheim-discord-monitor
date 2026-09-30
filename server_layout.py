"""
/odin setup: organise a Discord server into Valheim/Norse-themed categories and
channels.

This module only plans (plain functions, testable without Discord): given a snapshot
of the server's categories and channels, it decides which existing ones become which
template channel, what gets renamed or moved, and what has to be created.
admin_bot.AdminBot shows the plan, applies it after a Confirm click, and can undo it.

Rules:
  * nothing is ever deleted;
  * a channel is matched to a template slot by, in order: the ID remembered from the
    last run, the bot's own configuration (the admin channel, the webhook's channel),
    then its name (Discord's defaults like "general" or "General", and common names
    like "rules" or "memes");
  * channels that match nothing stay exactly where they are;
  * the bot's stat categories are left alone.
"""

from __future__ import annotations

import re
from typing import Optional

# (key, name, aliases, flags) per category; channels: (key, kind, name, topic, aliases, flags).
# flags: "private" = only admins and the bot see it; "readonly" = only admins post;
# "admin"/"feed" = the bot's admin channel / the webhook's channel.
TEMPLATE = [
    {"key": "gates", "name": "🚪 The Gates", "aliases": ["Information", "Info", "Welcome", "Start Here"],
     "channels": [
         ("rules", "text", "📜┃runestone", "Rules and announcements. Read before you set sail.",
          ["rules", "announcements", "rules-and-info", "info", "server-rules", "news"], {"readonly"}),
         ("welcome", "text", "🚪┃the-gates",
          "New here? /valheim join shows the join code and address. /valheim request-access <character> "
          "asks the admins to let you in.",
          ["welcome", "start-here", "new-members", "introductions", "intros", "join"], set()),
     ]},
    {"key": "mead", "name": "🍺 The Mead Hall", "aliases": ["Text Channels", "Text", "General", "Chat",
                                                           "Community"],
     "channels": [
         ("general", "text", "🍺┃mead-hall", "General chat. Pull up a bench by the fire.",
          ["general", "chat", "general-chat", "main", "lobby", "off-topic", "hangout"], set()),
         ("media", "text", "🎨┃skalds-corner", "Screenshots, clips and memes from your adventures.",
          ["media", "screenshots", "memes", "clips", "pics", "images", "videos"], set()),
         ("builds", "text", "🔨┃the-forge", "Builds, bases and base tours.",
          ["builds", "building", "bases", "base-tours", "creations"], set()),
         ("bots", "text", "🪶┃muninns-roost",
          "Ask Muninn: /muninn stats, top, titles and online. /valheim join, link, notify and map "
          "work in any channel.",
          ["bot-commands", "bots", "commands", "bot", "bot-spam", "botspam"], set()),
     ]},
    {"key": "wilds", "name": "⚔️ The Wilds", "aliases": ["Gaming", "Valheim", "Game", "Games"],
     "channels": [
         ("feed", "text", "🐦┃huginns-watch",
          "Huginn reports from the server: logins, deaths, raids, achievements and titles.",
          ["valheim", "server-feed", "server-status", "status", "game-feed", "valheim-feed", "server-log",
           "activity"], {"feed"}),
         ("plans", "text", "🗺️┃war-council", "Plan raids and game nights with /warcouncil plan.",
          ["lfg", "looking-for-group", "plans", "events", "game-nights", "planning", "raids"], set()),
         ("lore", "text", "🔮┃seers-stone", "Seeds, maps, tips and questions.",
          ["tips", "help", "guides", "questions", "seeds", "valheim-help", "strategy"], set()),
     ]},
    {"key": "longhouses", "name": "🔊 The Longhouses", "aliases": ["Voice Channels", "Voice", "Voice Chat"],
     "channels": [
         ("vc_main", "voice", "🍺 The Longhouse", None, ["General", "Lounge", "Lobby", "Hangout", "Chill"], set()),
         ("vc_raid", "voice", "⚔️ Raiding Party", None, ["Gaming", "Game", "Valheim", "Game Night", "Party",
                                                        "Gaming 1"], set()),
         ("vc_afk", "voice", "🎣 Fishing Hut (AFK)", None, ["AFK", "Away", "Idle"], set()),
     ]},
    {"key": "odin", "name": "🔒 Odin's Seat", "aliases": ["Admin", "Admins", "Staff", "Mods", "Moderators",
                                                        "Moderation"], "private": True,
     "channels": [
         ("admin", "text", "👁️┃odins-seat",
          "Refused joins with Permit/Ban, update checks and restarts. Only admins see this.",
          ["admin", "admins", "mod", "mods", "staff", "admin-chat", "valheim-admin", "mod-chat"],
          {"admin", "private"}),
     ]},
]


# Earlier default topics: a channel still showing one of these gets the current topic.
OLD_TOPICS = {
    "bots": ("Ask Muninn: /valheim stats, top, titles, link, notify, map and more.",),
    "plans": ("Plan raids and game nights with /valheim plan.",),
}


def norm(name: str) -> str:
    """Compare names without emoji, separators or case: "📜┃Run-estone" -> "runestone"."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _match(items: list, used: set, want_id=None, names=()) -> Optional[dict]:
    """The first unused item with this ID, else with one of these names (by position)."""
    if want_id:
        for it in items:
            if str(it["id"]) == str(want_id) and it["id"] not in used:
                return it
    wanted = {norm(n) for n in names if norm(n)}
    for it in sorted(items, key=lambda i: i.get("position", 0)):
        if it["id"] not in used and norm(it["name"]) in wanted:
            return it
    return None


def plan(snapshot: dict, known: Optional[dict] = None, remembered: Optional[dict] = None,
         exclude: Optional[set] = None) -> dict:
    """What /odin setup would do.

    snapshot: {"categories": [{"id", "name", "position"}],
               "channels": [{"id", "name", "kind": "text"|"voice", "category_id", "position", "topic"}]}
    known: {"admin": channel id, "feed": channel id} from the bot's configuration.
    remembered: {"cat:<key>" | "ch:<key>": id} from the last run.
    exclude: IDs never to touch (the stat channels and their categories).

    Returns {"categories": [...], "channels": [...], "notes": [...]}, one entry per template slot:
      categories: {"key", "name", "id" (None = create), "old_name", "private"}
      channels:   {"key", "kind", "name", "topic", "flags", "category", "id" (None = create),
                   "old_name", "old_category_id", "old_topic", "position"}
    """
    known, remembered, exclude = known or {}, remembered or {}, set(exclude or ())
    cats = [c for c in snapshot.get("categories", []) if c["id"] not in exclude]
    chans = [c for c in snapshot.get("channels", []) if c["id"] not in exclude]
    used_c, used_ch = set(), set()
    out = {"categories": [], "channels": [], "notes": []}

    # Channels first: a channel found through the bot's config claims its slot even if its
    # name would also fit another one.
    slots = [(cat, ch) for cat in TEMPLATE for ch in cat["channels"]]
    found, notes = {}, []
    # Huginn's webhook posting into the general chat or the admin channel: that channel keeps
    # its job, the feed gets a channel of its own, and the webhook is moved there on apply.
    general = next(ch for cat in TEMPLATE for ch in cat["channels"] if ch[0] == "general")
    feed_ch = next((c for c in chans if known.get("feed") and str(c["id"]) == str(known["feed"])), None)
    if feed_ch and not remembered.get("ch:feed") and (
            str(feed_ch["id"]) == str(known.get("admin"))
            or norm(feed_ch["name"]) in {norm(n) for n in [general[2]] + general[4]}):
        known = {**known, "feed": None}
        notes.append(f"Huginn's webhook posts in #{feed_ch['name']}, which keeps its job; the feed gets its "
                     f"own #huginns-watch.")
    for pass_ in ("id", "name"):
        for cat, (key, kind, name, topic, aliases, flags) in slots:
            if key in found:
                continue
            pool = [c for c in chans if c["kind"] == kind]
            if pass_ == "id":
                hit = _match(pool, used_ch, remembered.get(f"ch:{key}")) or \
                    next((_match(pool, used_ch, known.get(f)) for f in ("admin", "feed")
                          if f in flags and known.get(f)), None)
            else:
                hit = _match(pool, used_ch, names=[name] + aliases)
            if hit:
                found[key] = hit
                used_ch.add(hit["id"])

    for cat in TEMPLATE:
        # Prefer the category the matched channels already sit in, when it isn't claimed.
        hit = _match(cats, used_c, remembered.get(f"cat:{cat['key']}")) or \
            _match(cats, used_c, names=[cat["name"]] + cat["aliases"])
        if hit:
            used_c.add(hit["id"])
        out["categories"].append({"key": cat["key"], "name": cat["name"], "id": hit and hit["id"],
                                  "old_name": hit and hit["name"], "private": bool(cat.get("private"))})
        for pos, (key, kind, name, topic, aliases, flags) in enumerate(cat["channels"]):
            hit = found.get(key)
            out["channels"].append({
                "key": key, "kind": kind, "name": name, "topic": topic, "flags": set(flags),
                "category": cat["key"], "position": pos, "id": hit and hit["id"],
                "old_name": hit and hit["name"], "old_category_id": hit and hit.get("category_id"),
                "old_topic": hit and hit.get("topic")})
    out["notes"] = notes
    # Discord's own join greetings ("Yay you made it, …") go to the server's System Messages
    # Channel. Point them at the welcome channel when they'd otherwise go nowhere, or into a
    # channel this layout makes private, where only admins would see them.
    system = snapshot.get("system_channel_id")
    private = {c["id"] for c in out["channels"] if "private" in c["flags"] and c["id"]}
    out["system"] = {"current": system, "move": system is None or system in private,
                     "current_name": next((c["name"] for c in chans if c["id"] == system), None)}
    return out


def changes(p: dict) -> dict:
    """Counts for the summary line: created / renamed / moved / unchanged."""
    cat_ids = {c["key"]: c["id"] for c in p["categories"]}
    n = {"create": 0, "rename": 0, "move": 0, "same": 0}
    for c in p["categories"]:
        n["create" if c["id"] is None else "rename" if c["old_name"] != c["name"] else "same"] += 1
    for ch in p["channels"]:
        if ch["id"] is None:
            n["create"] += 1
            continue
        renamed = ch["old_name"] != _discord_name(ch)
        moved = cat_ids.get(ch["category"]) is None or ch["old_category_id"] != cat_ids[ch["category"]]
        n["rename"] += renamed
        n["move"] += moved and not renamed
        n["same"] += not (renamed or moved)
    return n


def _discord_name(ch: dict) -> str:
    """How Discord will store the name: text channels are lower-case with dashes."""
    return ch["name"].lower().replace(" ", "-") if ch["kind"] == "text" else ch["name"]


def render(p: dict, limit: int = 3900) -> str:
    """The preview: every category and channel, and what happens to it."""
    lines = []
    for cat in p["categories"]:
        if cat["id"] is None:
            lines.append(f"**{cat['name']}** ✨ new")
        elif cat["old_name"] != cat["name"]:
            lines.append(f"**{cat['name']}** ← was *{cat['old_name']}*")
        else:
            lines.append(f"**{cat['name']}**")
        for ch in (c for c in p["channels"] if c["category"] == cat["key"]):
            icon = "🔊" if ch["kind"] == "voice" else "#"
            shown = ch["name"] if ch["kind"] == "voice" else _discord_name(ch)
            if ch["id"] is None:
                note = "✨ new"
            elif ch["old_name"] != _discord_name(ch):
                note = f"← was {icon if ch['kind'] == 'text' else ''}{ch['old_name']}"
            else:
                note = "✓"
            extra = " 🔒" if "private" in ch["flags"] else " 📢" if "readonly" in ch["flags"] else ""
            lines.append(f"  {icon} {shown}{extra}  {note}")
    n = changes(p)
    lines.append("")
    sysmsg = p.get("system") or {}
    if sysmsg.get("move"):
        where = f"#{sysmsg['current_name']}, which only admins will see" if sysmsg.get("current_name") \
            else "no channel"
        lines.append(f"📣 Discord's join messages (\"Yay you made it…\") go to {where}; they'll go to "
                     f"#{_discord_name(next(c for c in p['channels'] if c['key'] == 'welcome'))}.")
    hook = p.get("webhook") or {}
    feed_name = _discord_name(next(c for c in p["channels"] if c["key"] == "feed"))
    if hook.get("action") == "create":
        lines.append(f"🐦 No webhook is set up yet: Huginn's webhook will be created in #{feed_name}.")
    elif hook.get("action") == "move":
        lines.append(f"🐦 Huginn's webhook posts in #{hook.get('from') or '?'}; it'll be moved to #{feed_name} "
                     "(same URL, nothing to change in your config).")
    for note in p.get("notes", []):
        lines.append(f"⚠️ {note}")
    lines.append(f"{n['create']} new · {n['rename']} renamed · {n['move']} moved · {n['same']} already right. "
                 "Nothing is deleted; channels not listed stay as they are.")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit - 1] + "…"
