"""
Community features for the Discord bot, stored in the stats database:

  * linked Discord accounts (character <-> Discord user), which the "In Valheim" role,
    /valheim stats with no name, and mentions in welcome/milestone posts use;
  * notifications: DM when the server gets its first player, or when a followed
    character joins;
  * access requests: "I'll join as Ingrid", so a refused-join notice can say who it is;
  * game-night plans with RSVPs and a reminder;
  * weekly title roles for the /valheim top leaders (Heimdall, Hel, Sleipnir, Thor);
  * reading the world seed for a map link.

Functions take an sqlite3 connection (stats_db.connect) so the monitor thread and the
bot thread can each use their own.
"""

from __future__ import annotations

import datetime as _dt
import glob
import os
import re
import time
from typing import Optional

ACCESS_REQUEST_DAYS = 14


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Players and linked accounts
# ---------------------------------------------------------------------------
def known_player(conn, name: str) -> Optional[str]:
    """The character's name as the log spells it, matched case-insensitively."""
    r = _one(conn, "SELECT player FROM play_sessions WHERE player = ? COLLATE NOCASE "
                   "ORDER BY login_at DESC LIMIT 1", (name.strip(),))
    return r["player"] if r else None


def player_names(conn, prefix: str = "", limit: int = 25) -> list:
    """For autocomplete: characters seen on the server, most recent first."""
    return [r["player"] for r in _rows(
        conn, "SELECT player, MAX(login_at) AS last FROM play_sessions WHERE player LIKE ? "
              "GROUP BY player ORDER BY last DESC LIMIT ?", (f"%{prefix.strip()}%", limit))]


def link_player(conn, player: str, user_id, force: bool = False) -> Optional[str]:
    """Link a character to a Discord user. Returns an error, or None on success."""
    cur = linked_user(conn, player)
    if cur and cur != str(user_id) and not force:
        return f"**{player}** is already linked to <@{cur}>. Ask an admin if that's wrong."
    conn.execute("INSERT INTO discord_links(player, user_id, linked_at) VALUES (?,?,?) "
                 "ON CONFLICT(player) DO UPDATE SET user_id=excluded.user_id, linked_at=excluded.linked_at",
                 (player, str(user_id), int(time.time())))
    conn.commit()
    return None


def unlink_player(conn, player: str) -> bool:
    n = conn.execute("DELETE FROM discord_links WHERE player = ?", (player,)).rowcount
    conn.commit()
    return n > 0


def linked_user(conn, player: str) -> Optional[str]:
    r = _one(conn, "SELECT user_id FROM discord_links WHERE player = ?", (player,))
    return r["user_id"] if r else None


def platform_characters(conn, platform_id: str) -> list:
    """Characters that have joined with this platform id (any prefix: V_/Steam_/N_/Nintendo_…)."""
    import stats_db
    try:
        return [r["player"] for r in _rows(conn, "SELECT player FROM player_platform WHERE platform_key = ? "
                                                 "ORDER BY updated_at DESC", (stats_db.platform_key(platform_id),))]
    except Exception:  # noqa: BLE001  (a database from before this table)
        return []


def who_is(conn, platform_id: Optional[str] = None, character: Optional[str] = None) -> dict:
    """What the bot knows about a player from a platform id and/or character name:
    {"characters": [...], "users": [Discord user ids, most likely first]}. Users come from
    /valheim link on any of the characters, then /valheim request-access for the name."""
    chars = platform_characters(conn, platform_id) if platform_id else []
    if character and character not in chars:
        chars = [character] + chars
    users = []
    for c in chars:
        u = linked_user(conn, c)
        if u and u not in users:
            users.append(u)
    if character:
        asked = access_request(conn, character)
        if asked and asked not in users:
            users.append(asked)
    return {"characters": chars, "users": users}


def linked_players(conn, user_id) -> list:
    return [r["player"] for r in _rows(conn, "SELECT player FROM discord_links WHERE user_id = ? "
                                             "ORDER BY linked_at", (str(user_id),))]


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def set_notify_first(conn, user_id, on: bool) -> None:
    if on:
        conn.execute("INSERT OR IGNORE INTO notify_first(user_id) VALUES (?)", (str(user_id),))
    else:
        conn.execute("DELETE FROM notify_first WHERE user_id = ?", (str(user_id),))
    conn.commit()


def set_follow(conn, user_id, player: str, on: bool) -> None:
    if on:
        conn.execute("INSERT OR IGNORE INTO follows(user_id, player) VALUES (?,?)", (str(user_id), player))
    else:
        conn.execute("DELETE FROM follows WHERE user_id = ? AND player = ?", (str(user_id), player))
    conn.commit()


def notify_off(conn, user_id) -> None:
    conn.execute("DELETE FROM notify_first WHERE user_id = ?", (str(user_id),))
    conn.execute("DELETE FROM follows WHERE user_id = ?", (str(user_id),))
    conn.commit()


def my_notifications(conn, user_id) -> dict:
    first = bool(_one(conn, "SELECT 1 AS x FROM notify_first WHERE user_id = ?", (str(user_id),)))
    follows = [r["player"] for r in _rows(conn, "SELECT player FROM follows WHERE user_id = ? ORDER BY player",
                                          (str(user_id),))]
    return {"first": first, "follows": follows}


def who_to_notify(conn, player: str, server_was_empty: bool) -> dict:
    """{user_id: reason} for a login. A player isn't told about their own login."""
    out = {}
    if server_was_empty:
        for r in _rows(conn, "SELECT user_id FROM notify_first"):
            out[r["user_id"]] = "first"
    for r in _rows(conn, "SELECT user_id FROM follows WHERE player = ?", (player,)):
        out[r["user_id"]] = "follow"
    me = linked_user(conn, player)
    out.pop(me, None)
    return out


# ---------------------------------------------------------------------------
# Access requests
# ---------------------------------------------------------------------------
def request_access(conn, player: str, user_id) -> None:
    conn.execute("INSERT INTO access_requests(player, user_id, requested_at) VALUES (?,?,?) "
                 "ON CONFLICT(player) DO UPDATE SET user_id=excluded.user_id, requested_at=excluded.requested_at",
                 (player.strip(), str(user_id), int(time.time())))
    conn.commit()


def access_request(conn, player: str, now: Optional[float] = None) -> Optional[str]:
    """The Discord user who asked to join as this character in the last two weeks."""
    now = time.time() if now is None else now
    r = _one(conn, "SELECT user_id, requested_at FROM access_requests WHERE player = ?", (player,))
    if r and now - (r["requested_at"] or 0) <= ACCESS_REQUEST_DAYS * 86400:
        return r["user_id"]
    return None


def clear_access_request(conn, player: str) -> None:
    conn.execute("DELETE FROM access_requests WHERE player = ?", (player,))
    conn.commit()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
# Each Steam unlock, attributed to the character its Steam account played most recently.
_STEAM_UNLOCKS = ("SELECT (SELECT ps.player FROM player_steam ps WHERE ps.steam_id = u.steam_id "
                  "ORDER BY ps.updated_at DESC LIMIT 1) AS player FROM steam_unlock u "
                  "WHERE u.unlocktime >= {since}")

TOP = {
    "time": ("Most time played", "SELECT player, SUM(duration_seconds) AS v FROM play_sessions "
                                 "GROUP BY player ORDER BY v DESC LIMIT ?"),
    "deaths": ("Most deaths", "SELECT player, COUNT(*) AS v FROM deaths GROUP BY player ORDER BY v DESC LIMIT ?"),
    "sessions": ("Most visits", "SELECT player, COUNT(*) AS v FROM play_sessions GROUP BY player "
                                "ORDER BY v DESC LIMIT ?"),
    "longest": ("Longest single session", "SELECT player, MAX(duration_seconds) AS v FROM play_sessions "
                                          "GROUP BY player ORDER BY v DESC LIMIT ?"),
    # Steam only: achievements belong to a Steam account, shown under the character that
    # account played most recently.
    "achievements": ("Most achievements (Steam)",
                     "SELECT player, COUNT(*) AS v FROM (" + _STEAM_UNLOCKS.format(since="0") + ") "
                     "WHERE player IS NOT NULL GROUP BY player ORDER BY v DESC LIMIT ?"),
}


def top(conn, category: str, limit: int = 10) -> list:
    return _rows(conn, TOP[category][1], (limit,))


# ---------------------------------------------------------------------------
# Title roles: one Discord role per /valheim top category, held by its leader
# ---------------------------------------------------------------------------
# category -> (role name, why it fits, role colour)
TITLES = {
    "time": ("Heimdall", "never leaves his post: most time played", 0xF1C40F),
    "deaths": ("Hel", "keeper of the dead: most deaths", 0x71368A),
    "sessions": ("Sleipnir", "carries riders between the worlds and always comes back: most visits", 0x95A5A6),
    "longest": ("Thor", "drank from the sea and lowered it: longest single session", 0x3498DB),
    "achievements": ("Bragi", "sings the great deeds of heroes: most Steam achievements", 0xE67E22),
}
# Earlier names of the title roles: a role the bot made under one of these is renamed.
TITLE_OLD_NAMES = {"sessions": ("Huginn",)}
_TITLE_SQL = {
    "time": "SELECT player, SUM(duration_seconds) AS v FROM play_sessions WHERE login_at >= ? GROUP BY player",
    "deaths": "SELECT player, COUNT(*) AS v FROM deaths WHERE died_at >= ? GROUP BY player",
    "sessions": "SELECT player, COUNT(*) AS v FROM play_sessions WHERE login_at >= ? GROUP BY player",
    "longest": "SELECT player, MAX(duration_seconds) AS v FROM play_sessions WHERE login_at >= ? GROUP BY player",
    "achievements": "SELECT player, COUNT(*) AS v FROM (" + _STEAM_UNLOCKS.format(since="?") + ") "
                    "WHERE player IS NOT NULL GROUP BY player",
}


def title_holder(conn, category: str) -> Optional[dict]:
    """{"player", "user_id"} stored for a title: the character that holds it, and the
    Discord user who was given the role (None when that character isn't linked)."""
    import json
    try:
        v = get_meta(conn, f"title:{category}")
        return json.loads(v) if v else None
    except ValueError:
        return None


def set_title_holder(conn, category: str, player: Optional[str], user_id: Optional[str]) -> None:
    import json
    set_meta(conn, f"title:{category}", json.dumps({"player": player, "user_id": user_id}))


def get_meta(conn, key: str) -> Optional[str]:
    r = _one(conn, "SELECT value FROM meta WHERE key = ?", (key,))
    return r["value"] if r else None


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, None if value is None else str(value)))
    conn.commit()


def title_value(conn, category: str, player: str, since: int = 0):
    """One character's score in a title category (for showing the holder's number)."""
    sql = _TITLE_SQL[category].replace("GROUP BY", "AND player = ? COLLATE NOCASE GROUP BY")
    r = _one(conn, sql, (since, player))
    return r["v"] if r else 0


def title_leaders(conn, since: int = 0) -> dict:
    """category -> {"player", "v"} of the leader since the log timestamp `since` (0 = all
    time), or None with no data. On a tie the current holder keeps the title, so it
    doesn't flip back and forth; otherwise the first to get there (by name) wins."""
    out = {}
    for cat, sql in _TITLE_SQL.items():
        rows = [r for r in _rows(conn, sql, (since,)) if (r["v"] or 0) > 0]
        if not rows:
            out[cat] = None
            continue
        best = max(r["v"] for r in rows)
        tied = sorted((r for r in rows if r["v"] == best), key=lambda r: r["player"].lower())
        held = (title_holder(conn, cat) or {}).get("player")
        keep = next((r for r in tied if held and r["player"].lower() == held.lower()), None)
        out[cat] = keep or tied[0]
    return out


def render_titles(holders: dict, changed: set = frozenset(), period: str = "all") -> dict:
    """holders: category -> {"player", "v", "user_id"} or None."""
    lines = []
    for cat, (role, why, _) in TITLES.items():
        h = holders.get(cat)
        if not h:
            lines.append(f"**{role}**: nobody yet\n*{why}*")
            continue
        value = str(h["v"]) if cat in ("deaths", "sessions", "achievements") else _dur(h["v"])
        who = f"<@{h['user_id']}> ({h['player']})" if h.get("user_id") else \
            f"**{h['player']}** (not linked: `/valheim link {h['player']}` to get the role)"
        new = " 🆕" if cat in changed else ""
        lines.append(f"**{role}**{new}: {who}, {value}\n*{why}*")
    return {"title": "🏆 Titles of the realm", "color": 0xF1C40F,
            "description": "\n".join(lines),
            "footer": {"text": "Reassigned weekly · " + ("last 7 days" if period == "week" else "all time")
                               + " · /valheim top"}}


def player_stats(conn, player: str) -> Optional[dict]:
    s = _one(conn, "SELECT player, COALESCE(SUM(duration_seconds),0) AS seconds, COUNT(*) AS sessions, "
                   "MAX(duration_seconds) AS longest, MIN(login_at) AS first_seen, "
                   "MAX(COALESCE(logout_at, last_seen_at)) AS last_seen, "
                   "SUM(CASE WHEN logout_at IS NULL THEN 1 ELSE 0 END) AS open "
                   "FROM play_sessions WHERE player = ? COLLATE NOCASE", (player,))
    if not s or not s["sessions"]:
        return None
    s["deaths"] = _one(conn, "SELECT COUNT(*) AS c FROM deaths WHERE player = ? COLLATE NOCASE", (player,))["c"]
    s["rank"] = 1 + _one(conn, "SELECT COUNT(*) AS c FROM (SELECT SUM(duration_seconds) AS t FROM play_sessions "
                               "GROUP BY player) WHERE t > ?", (s["seconds"],))["c"]
    s["players"] = _one(conn, "SELECT COUNT(DISTINCT player) AS c FROM play_sessions")["c"]
    s["steam"] = steam_achievements(conn, player)
    return s


def steam_achievements(conn, player: str) -> Optional[dict]:
    """{"unlocked", "total", "last_unlock_name", "error"} for a character's Steam account,
    or None when the character isn't linked to Steam (console players, or not seen yet)."""
    try:
        return _one(conn, "SELECT p.unlocked, p.total, p.last_unlock_name, p.error FROM player_steam ps "
                          "JOIN steam_profile p ON p.steam_id = ps.steam_id WHERE ps.player = ? COLLATE NOCASE",
                    (player,))
    except Exception:  # noqa: BLE001  (an old database without the Steam tables)
        return None


def render_unlocks(conn, steam_id: str, unlocks: list) -> dict:
    """Embed for new Steam achievements: "🏅 Ingrid unlocked Bonemass slayer"."""
    who = _one(conn, "SELECT player FROM player_steam WHERE steam_id = ? ORDER BY updated_at DESC LIMIT 1",
               (steam_id,))
    name = who["player"] if who else (_one(conn, "SELECT persona FROM steam_profile WHERE steam_id = ?",
                                           (steam_id,)) or {}).get("persona") or "Someone"
    user = linked_user(conn, name) if who else None
    info = {r["apiname"]: r for r in _rows(conn, "SELECT apiname, name, description, icon FROM steam_schema")}
    prof = _one(conn, "SELECT unlocked, total FROM steam_profile WHERE steam_id = ?", (steam_id,)) or {}
    items = [info.get(a) or {"apiname": a, "name": a, "description": "", "icon": None} for a, _ in unlocks]
    if len(items) == 1:
        a = items[0]
        title = f"🏅 {name} unlocked {a['name']}"
        desc = a.get("description") or ""
    else:
        title = f"🏅 {name} unlocked {len(items)} achievements"
        desc = "\n".join(f"**{a['name']}**" + (f": {a['description']}" if a.get("description") else "")
                         for a in items[:10]) + (f"\n… and {len(items) - 10} more" if len(items) > 10 else "")
    if user:
        desc = f"<@{user}>" + (f"\n{desc}" if desc else "")
    out = {"title": title[:256], "description": desc[:4000] or None}
    if prof.get("total"):
        out["footer"] = {"text": f"{prof.get('unlocked') or 0}/{prof['total']} Valheim achievements on Steam"}
    icon = next((a["icon"] for a in reversed(items) if a.get("icon")), None)   # the latest with an icon
    if icon:
        out["thumbnail"] = {"url": icon}
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------------------
# Game-night plans
# ---------------------------------------------------------------------------
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_REL = re.compile(r"^in\s+(?:(\d+)\s*(?:h|hr|hrs|hours?))?\s*(?:(\d+)\s*(?:m|min|mins|minutes?))?$")
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


def parse_when(text: str, now: _dt.datetime) -> _dt.datetime:
    """'in 2h', 'in 90m', '20:00', '8pm', '8:30 pm', 'tomorrow 8pm', 'sat 20:00',
    '2026-10-03 20:00'. Times are in now's time zone; a time already past today
    means tomorrow. Raises ValueError with a hint."""
    t = " ".join(text.lower().replace(",", " ").split())
    m = _REL.match(t)
    if m and (m.group(1) or m.group(2)):
        return now + _dt.timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    try:
        return _dt.datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=now.tzinfo)
    except ValueError:
        pass
    day_offset, force_day = None, False
    words = t.split(" ", 1)
    if words[0] in ("today", "tonight"):
        day_offset, t = 0, (words[1] if len(words) > 1 else "20:00")
    elif words[0] == "tomorrow":
        day_offset, force_day, t = 1, True, (words[1] if len(words) > 1 else "20:00")
    else:
        day = next((i for i, d in enumerate(_DAYS) if len(words[0]) >= 3 and d.startswith(words[0])), None)
        if day is not None:
            day_offset, force_day = (day - now.weekday()) % 7, True
            t = words[1] if len(words) > 1 else "20:00"
    m = _CLOCK.match(t.replace(" ", ""))
    if not m:
        raise ValueError("Couldn't read that time. Try `20:00`, `8pm`, `tomorrow 8pm`, `sat 20:00` or `in 2h`.")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError("With am/pm the hour must be 1-12.")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ValueError("That isn't a valid time of day.")
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    when += _dt.timedelta(days=day_offset or 0)
    if when <= now and not force_day:
        when += _dt.timedelta(days=1)
    elif when <= now and force_day and day_offset == 0:
        when += _dt.timedelta(days=7)            # "sat 8pm" said on Saturday at 9pm = next week
    return when


def create_plan(conn, title: str, at: int, channel_id, creator_id) -> int:
    cur = conn.execute("INSERT INTO plans(title, at, channel_id, creator_id) VALUES (?,?,?,?)",
                       (title, int(at), str(channel_id), str(creator_id)))
    conn.commit()
    return cur.lastrowid


def set_plan_message(conn, plan_id: int, message_id) -> None:
    conn.execute("UPDATE plans SET message_id = ? WHERE id = ?", (str(message_id), plan_id))
    conn.commit()


def rsvp(conn, plan_id: int, user_id, choice: str) -> None:
    if choice not in ("going", "maybe", "no"):
        raise ValueError(choice)
    conn.execute("INSERT INTO rsvps(plan_id, user_id, choice) VALUES (?,?,?) "
                 "ON CONFLICT(plan_id, user_id) DO UPDATE SET choice=excluded.choice",
                 (plan_id, str(user_id), choice))
    conn.commit()


def get_plan(conn, plan_id: int) -> Optional[dict]:
    p = _one(conn, "SELECT * FROM plans WHERE id = ?", (plan_id,))
    if p:
        p["rsvps"] = {c: [r["user_id"] for r in _rows(conn, "SELECT user_id FROM rsvps WHERE plan_id = ? AND choice = ? "
                                                            "ORDER BY rowid", (plan_id, c))]
                      for c in ("going", "maybe", "no")}
    return p


def due_reminders(conn, now: float, lead_seconds: int) -> list:
    """Plans starting within lead_seconds that haven't been reminded (and aren't long past)."""
    return [get_plan(conn, r["id"]) for r in _rows(
        conn, "SELECT id FROM plans WHERE reminded = 0 AND at - ? <= ? AND at > ? - 600",
        (int(now), lead_seconds, int(now)))]


def mark_reminded(conn, plan_id: int) -> None:
    conn.execute("UPDATE plans SET reminded = 1 WHERE id = ?", (plan_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# World seed
# ---------------------------------------------------------------------------
def _string_at(data: bytes, pos: int):
    """A .NET/protobuf-style string at pos: 7-bit encoded length, then UTF-8."""
    n, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
        if shift > 28:
            raise ValueError("bad length")
    if pos + n > len(data):
        raise IndexError("short string")
    return data[pos:pos + n].decode("utf-8"), pos + n


def _looks_like_seed(seed: str) -> bool:
    return 0 < len(seed) <= 32 and seed.isprintable() and " " not in seed


def _decompress(data: bytes) -> bytes:
    import gzip
    import zlib
    try:
        if data[:2] == b"\x1f\x8b":
            return gzip.decompress(data)
        if data[:1] == b"\x78":
            return zlib.decompress(data)
    except (OSError, zlib.error, EOFError):
        pass
    return data


def _read_fwl(path: str, world: Optional[str] = None) -> Optional[tuple]:
    """(world name, seed) from world metadata.

    - Pre-1.0 `<world>.fwl`: a length-prefixed package of int32 version, then the name
      and seed as .NET strings (7-bit encoded length + UTF-8).
    - 1.0 `<world>/_main.<N>.fwl2`: the layout isn't documented, so look for the world
      name (the folder name) as a length-prefixed string and take the next
      length-prefixed string after it (allowing a few bytes of field tags between)."""
    with open(path, "rb") as f:
        data = _decompress(f.read(1 << 20))

    for start in (8, 4):                          # classic .fwl, with/without package length
        try:
            name, pos = _string_at(data, start)
            seed, _ = _string_at(data, pos)
        except (IndexError, UnicodeDecodeError, ValueError):
            continue
        if name and (world is None or name == world) and _looks_like_seed(seed):
            return name, seed

    if not world:
        return None
    needle = world.encode("utf-8")
    idx = data.find(needle)
    while idx != -1:
        if idx > 0 and data[idx - 1] == len(needle):
            after = idx + len(needle)
            for skip in range(0, 9):              # field tag / padding bytes before the seed
                try:
                    seed, _ = _string_at(data, after + skip)
                except (IndexError, UnicodeDecodeError, ValueError):
                    continue
                if _looks_like_seed(seed) and seed.isascii() and len(seed) >= 3:
                    return world, seed
        idx = data.find(needle, idx + 1)
    return None


def _counter(path: str) -> int:
    m = re.search(r"_main\.(\d+)\.fwl2$", path)
    return int(m.group(1)) if m else -1


def world_seed(save_dir: str, world: Optional[str] = None) -> Optional[tuple]:
    """(world name, seed) of the live world in <save_dir>/worlds_local, not a backup.
    Handles 1.0 world folders (`<world>/_main.<N>.fwl2`, newest save first) and
    pre-1.0 `<world>.fwl` files."""
    base = os.path.join(save_dir, "worlds_local")
    found = []
    for path in glob.glob(os.path.join(base, "*", "_main.*.fwl2")):
        folder = os.path.basename(os.path.dirname(path))
        if "_backup_" in folder or (world and folder != world):
            continue
        found.append((0, _counter(path), _mtime(path), path, folder))
    for path in glob.glob(os.path.join(base, "*.fwl")) + glob.glob(os.path.join(base, "*", "*.fwl")):
        if "_backup_" in path:
            continue
        name = os.path.splitext(os.path.basename(path))[0]
        if world and name != world:
            continue
        found.append((1, 0, _mtime(path), path, name))
    # 1.0 files first, then highest save counter, then newest.
    for _, _, _, path, name in sorted(found, key=lambda c: (c[0], -c[1], -c[2])):
        try:
            result = _read_fwl(path, name)
        except OSError:
            continue
        if result:
            return result
    return None


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def map_url(seed: str) -> str:
    from urllib.parse import quote
    return f"https://valheim-map.world/?seed={quote(seed)}&offset=0%2C0&zoom=0.077&view=0"


# ---------------------------------------------------------------------------
# Rendering (plain dicts for discord.Embed.from_dict, so they're testable)
# ---------------------------------------------------------------------------
def _dur(seconds) -> str:
    from extras import fmt_duration
    return fmt_duration(seconds or 0)


def log_clock_offset(conn) -> int:
    r = _one(conn, "SELECT value FROM meta WHERE key='log_clock_offset'")
    try:
        return int(r["value"]) if r and r["value"] else 0
    except (TypeError, ValueError):
        return 0


def render_stats(s: dict, offset: int, linked: Optional[str] = None) -> dict:
    """Embed for /valheim stats. Log times + offset = real time, for Discord timestamps."""
    fields = [
        {"name": "Time played", "value": _dur(s["seconds"]), "inline": True},
        {"name": "Rank", "value": f"#{s['rank']} of {s['players']}", "inline": True},
        {"name": "Visits", "value": str(s["sessions"]), "inline": True},
        {"name": "Longest session", "value": _dur(s["longest"]), "inline": True},
        {"name": "Deaths", "value": str(s["deaths"]), "inline": True},
        {"name": "Deaths per hour", "value": f"{s['deaths'] / max(s['seconds'] / 3600, 1e-9):.1f}"
                                              if s["seconds"] >= 600 else "-", "inline": True},
    ]
    st = s.get("steam")
    if st:
        if st.get("total"):
            value = f"{st.get('unlocked') or 0}/{st['total']}"
            if st.get("last_unlock_name"):
                value += f" · latest: {st['last_unlock_name']}"
        elif st.get("error") == "private":
            value = "🔒 Steam profile or game details are private"
        else:
            value = "Not fetched yet"
        fields.append({"name": "🏅 Achievements (Steam)", "value": value[:1024], "inline": False})
    if s.get("first_seen"):
        fields.append({"name": "First seen", "value": f"<t:{int(s['first_seen']) + offset}:D>", "inline": True})
    if s.get("open"):
        fields.append({"name": "Last seen", "value": "🟢 Playing now", "inline": True})
    elif s.get("last_seen"):
        fields.append({"name": "Last seen", "value": f"<t:{int(s['last_seen']) + offset}:R>", "inline": True})
    desc = f"Linked to <@{linked}>" if linked else None
    out = {"title": f"📊 {s['player']}", "color": 0x5865F2, "fields": fields}
    if desc:
        out["description"] = desc
    return out


def render_top(category: str, rows: list) -> dict:
    title = TOP[category][0]
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for i, r in enumerate(rows):
        value = str(r["v"]) if category in ("deaths", "sessions", "achievements") else _dur(r["v"])
        lines.append(f"{medals[i] if i < 3 else f'`{i + 1:>2}.`'} **{r['player']}**: {value}")
    return {"title": f"🏆 {title}", "color": 0xF1C40F, "description": "\n".join(lines) or "No data yet."}


def render_plan(p: dict, server_name: str) -> dict:
    def names(ids):
        return ", ".join(f"<@{u}>" for u in ids) or "-"
    r = p["rsvps"]
    return {
        "title": f"📅 {p['title']}",
        "color": 0x57F287,
        "description": f"<t:{p['at']}:F> (<t:{p['at']}:R>) on **{server_name}**\n"
                       f"Planned by <@{p['creator_id']}>. Everyone signed up as going or maybe is "
                       f"pinged shortly before it starts.",
        "fields": [{"name": f"✅ Going ({len(r['going'])})", "value": names(r["going"])[:1024], "inline": True},
                   {"name": f"❔ Maybe ({len(r['maybe'])})", "value": names(r["maybe"])[:1024], "inline": True},
                   {"name": f"❌ Can't ({len(r['no'])})", "value": names(r["no"])[:1024], "inline": True}],
    }
