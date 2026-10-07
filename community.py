"""
Community features for the Discord bot, stored in the stats database:

  * linked Discord accounts (character <-> Discord user), which the "In Valheim" role,
    /muninn stats with no name, and mentions in welcome/milestone posts use;
  * notifications: DM when the server gets its first player, or when a followed
    character joins;
  * access requests: "I'll join as Ingrid", so a refused-join notice can say who it is;
  * game-night plans with RSVPs and a reminder;
  * weekly title roles for the /muninn top leaders (Heimdall, Hel, Sleipnir, Thor);
  * reading the world seed for a map link.

Functions take an sqlite3 connection (stats_db.connect) so the monitor thread and the
bot thread can each use their own.
"""

from __future__ import annotations

import calendar
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


def crowd_thresholds(conn) -> dict:
    """{user_id: players} for "DM me when this many are online" (kept in meta)."""
    import json
    try:
        return {str(k): int(v) for k, v in json.loads(get_meta(conn, "notify_crowd") or "{}").items()}
    except (ValueError, TypeError):
        return {}


def set_notify_crowd(conn, user_id, players: Optional[int]) -> None:
    import json
    crowd = crowd_thresholds(conn)
    if players:
        crowd[str(user_id)] = int(players)
    else:
        crowd.pop(str(user_id), None)
    set_meta(conn, "notify_crowd", json.dumps(crowd))


def who_to_nudge(conn, before: int, now: int, online: list = ()) -> list:
    """Users whose "this many online" threshold the count just reached (before < n <= now),
    except those whose own character is one of the players online."""
    here = {linked_user(conn, p) for p in online}
    return [uid for uid, n in crowd_thresholds(conn).items() if before < n <= now and uid not in here]


def notify_off(conn, user_id) -> None:
    conn.execute("DELETE FROM notify_first WHERE user_id = ?", (str(user_id),))
    conn.execute("DELETE FROM follows WHERE user_id = ?", (str(user_id),))
    conn.commit()
    set_notify_crowd(conn, user_id, None)


def my_notifications(conn, user_id) -> dict:
    first = bool(_one(conn, "SELECT 1 AS x FROM notify_first WHERE user_id = ?", (str(user_id),)))
    follows = [r["player"] for r in _rows(conn, "SELECT player FROM follows WHERE user_id = ? ORDER BY player",
                                          (str(user_id),))]
    return {"first": first, "follows": follows, "crowd": crowd_thresholds(conn).get(str(user_id))}


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
    "streak": ("Longest play streak (days in a row)", None),     # worked out in Python: streak_board()
}


def top(conn, category: str, limit: int = 10) -> list:
    if category == "streak":
        return streak_board(conn, log_clock_offset(conn), limit)
    return _rows(conn, TOP[category][1], (limit,))


# ---------------------------------------------------------------------------
# Title roles: one Discord role per /muninn top category, held by its leader
# ---------------------------------------------------------------------------
# category -> (role name, why it fits, role colour)
TITLES = {
    "time": ("Heimdall", "never leaves his post: most time played", 0xF1C40F),
    "deaths": ("Hel", "keeper of the dead: most deaths", 0x71368A),
    "sessions": ("Sleipnir", "carries riders between the worlds and always comes back: most visits", 0x95A5A6),
    "longest": ("Thor", "drank from the sea and lowered it: longest single session", 0x3498DB),
    "achievements": ("Bragi", "sings the great deeds of heroes: most Steam achievements", 0xE67E22),
    "least": ("Hœnir", "the silent god who hardly lifts a finger: least time played", 0x7F8C8D),
    "progress": ("Mímir", "the wisest, who knows all things: most achievement progress (/muninn progress)",
                 0x1ABC9C),
    "builder": ("Völundr", "the master smith of legend: most pieces built (/muninn builders)", 0xA1887F),
}
# Hœnir only counts players seen in the last LEAST_ACTIVE_DAYS with at least
# LEAST_MIN_SECONDS played in all, so it doesn't stick to someone who quit, or who
# logged on for a few seconds to take a peek.
LEAST_ACTIVE_DAYS = 30
LEAST_MIN_SECONDS = 600
# The "away" role: every linked player who hasn't been on for this long.
AWAY_ROLE = ("Óðr", "Freyja's wandering husband, gone so long she wept gold for him: not seen for a while",
             0x546E7A)
# Earlier names of the title roles: a role the bot made under one of these is renamed.
TITLE_OLD_NAMES = {"sessions": ("Huginn",)}
_TITLE_SQL = {
    "time": "SELECT player, SUM(duration_seconds) AS v FROM play_sessions WHERE login_at >= ? GROUP BY player",
    "deaths": "SELECT player, COUNT(*) AS v FROM deaths WHERE died_at >= ? GROUP BY player",
    "sessions": "SELECT player, COUNT(*) AS v FROM play_sessions WHERE login_at >= ? GROUP BY player",
    "longest": "SELECT player, MAX(duration_seconds) AS v FROM play_sessions WHERE login_at >= ? GROUP BY player",
    "achievements": "SELECT player, COUNT(*) AS v FROM (" + _STEAM_UNLOCKS.format(since="?") + ") "
                    "WHERE player IS NOT NULL GROUP BY player",
    # A snapshot from the last upload, so it's the same whatever the period.
    "progress": "SELECT player, MAX(done) AS v FROM fch_progress WHERE ? IS NOT NULL GROUP BY player",
    # Pieces standing now (from the world save), so also the same whatever the period.
    "builder": "SELECT player, MAX(pieces) AS v FROM builders WHERE ? IS NOT NULL GROUP BY player",
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
    sql = _TITLE_SQL["time" if category == "least" else category].replace(
        "GROUP BY", "AND player = ? COLLATE NOCASE GROUP BY")
    r = _one(conn, sql, (since, player))
    return r["v"] if r else 0


def title_leaders(conn, since: int = 0) -> dict:
    """category -> {"player", "v"} of the leader since the log timestamp `since` (0 = all
    time), or None with no data. On a tie the current holder keeps the title, so it
    doesn't flip back and forth; otherwise the first to get there (by name) wins."""
    out = {}
    queries = dict(_TITLE_SQL, least=None)
    for cat, sql in queries.items():
        rows = least_candidates(conn, since) if cat == "least" else \
            [r for r in _rows(conn, sql, (since,)) if (r["v"] or 0) > 0]
        if not rows:
            out[cat] = None
            continue
        best = (min if cat == "least" else max)(r["v"] for r in rows)
        tied = sorted((r for r in rows if r["v"] == best), key=lambda r: r["player"].lower())
        held = (title_holder(conn, cat) or {}).get("player")
        keep = next((r for r in tied if held and r["player"].lower() == held.lower()), None)
        out[cat] = keep or tied[0]
    return out


def least_candidates(conn, since: int = 0) -> list:
    """Hœnir's candidates: [{"player", "v"}] of players seen in the last LEAST_ACTIVE_DAYS
    (counted back from the newest log activity) with at least LEAST_MIN_SECONDS played."""
    latest = _one(conn, "SELECT MAX(COALESCE(logout_at, last_seen_at)) AS t FROM play_sessions")
    if not latest or not latest["t"]:
        return []
    active_since = latest["t"] - LEAST_ACTIVE_DAYS * 86400
    rows = _rows(conn, "SELECT player, SUM(duration_seconds) AS v, COUNT(*) AS n, "
                       "MAX(COALESCE(logout_at, last_seen_at)) AS seen FROM play_sessions "
                       "WHERE login_at >= ? GROUP BY player", (since,))
    return [{"player": r["player"], "v": r["v"]} for r in rows
            if (r["v"] or 0) >= LEAST_MIN_SECONDS and (r["seen"] or 0) >= active_since]


def away_users(conn, cutoff: int) -> dict:
    """{user_id: last seen (log time)} for linked Discord users none of whose characters
    has been on since `cutoff` (a log timestamp)."""
    return {r["user_id"]: r["seen"] for r in _rows(
        conn, "SELECT d.user_id, MAX(COALESCE(s.logout_at, s.last_seen_at)) AS seen FROM discord_links d "
              "JOIN play_sessions s ON s.player = d.player COLLATE NOCASE GROUP BY d.user_id HAVING seen < ?",
        (int(cutoff),))}


def render_titles(holders: dict, changed: set = frozenset(), period: str = "all",
                  away: Optional[list] = None, away_days: float = 14,
                  hunters: Optional[tuple] = None) -> dict:
    """holders: category -> {"player", "v", "user_id"} or None; away: user ids holding
    the away role; hunters: (role name, [user ids]) holding the bounty role (None leaves
    those lines out)."""
    lines = []
    for cat, (role, why, _) in TITLES.items():
        h = holders.get(cat)
        if not h:
            lines.append(f"**{role}**: nobody yet\n*{why}*")
            continue
        value = str(h["v"]) if cat in ("deaths", "sessions", "achievements", "progress", "builder") else _dur(h["v"])
        who = f"<@{h['user_id']}> ({h['player']})" if h.get("user_id") else \
            f"**{h['player']}** (not linked: `/valheim link {h['player']}` to get the role)"
        new = " 🆕" if cat in changed else ""
        lines.append(f"**{role}**{new}: {who}, {value}\n*{why}*")
    if away is not None:
        name, why, _ = AWAY_ROLE
        who = ", ".join(f"<@{u}>" for u in sorted(away)[:20]) or "nobody, everyone's been around"
        lines.append(f"**{name}**: {who}\n*{why.split(':')[0]}: away {away_days:g}+ days*")
    if hunters is not None:
        role, uids = hunters
        who = ", ".join(f"<@{u}>" for u in uids[:20]) or "nobody this week"
        lines.append(f"**{role}**: {who}\n*goddess of the hunt: claimed a bounty this week (/warcouncil bounties)*")
    return {"title": "🏆 Titles of the realm", "color": 0xF1C40F,
            "description": "\n".join(lines),
            "footer": {"text": "Reassigned weekly · " + ("last 7 days" if period == "week" else "all time")
                               + " · /muninn top"}}


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
    days = play_days(conn, player, log_clock_offset(conn))
    s["streak"] = current_streak(days, _dt.date.today())
    s["best_streak"] = best_streak(days)
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
# Streaks, anniversaries, comparisons, uptime
# ---------------------------------------------------------------------------
STREAK_MARKS = (3, 5, 7, 10, 14, 21, 30, 50, 75, 100)


def play_days(conn, player: str, offset: int = 0) -> set:
    """The local dates a character started a session on (log time + offset = real time)."""
    return {_dt.datetime.fromtimestamp(r["login_at"] + offset).astimezone().date()
            for r in _rows(conn, "SELECT login_at FROM play_sessions WHERE player = ? COLLATE NOCASE", (player,))}


def streak(conn, player: str, today: _dt.date, offset: int = 0) -> int:
    """Days in a row, up to and including today, with at least one session."""
    days, n = play_days(conn, player, offset), 0
    while today - _dt.timedelta(days=n) in days:
        n += 1
    return n


def current_streak(days: set, today: _dt.date) -> int:
    """Days in a row ending today, or yesterday if they haven't played yet today (the
    streak is still alive until the day ends)."""
    day = today if today in days else today - _dt.timedelta(days=1)
    n = 0
    while day - _dt.timedelta(days=n) in days:
        n += 1
    return n


def best_streak(days: set) -> int:
    """The most days in a row ever played."""
    best = 0
    for d in days:
        if d - _dt.timedelta(days=1) in days:
            continue                          # not the start of a run
        n = 1
        while d + _dt.timedelta(days=n) in days:
            n += 1
        best = max(best, n)
    return best


def streak_board(conn, offset: int = 0, limit: int = 10) -> list:
    """[{"player", "v"}] by longest streak ever (days in a row), best first."""
    days: dict = {}
    for r in _rows(conn, "SELECT player, login_at FROM play_sessions"):
        days.setdefault(r["player"], set()).add(
            _dt.datetime.fromtimestamp(r["login_at"] + offset).astimezone().date())
    rows = [{"player": p, "v": best_streak(d)} for p, d in days.items()]
    rows.sort(key=lambda r: (-r["v"], r["player"].lower()))
    return rows[:limit]


def anniversary(first_seen: _dt.date, today: _dt.date) -> Optional[str]:
    """ "100 days" / "1 year" / "2 years" when today is that day since the first visit."""
    if (today - first_seen).days == 100:
        return "100 days"
    years = today.year - first_seen.year
    month_day = (first_seen.month, first_seen.day)
    if month_day == (2, 29) and not calendar.isleap(today.year):
        month_day = (2, 28)                   # a 29 February start is celebrated on the 28th
    if years >= 1 and (today.month, today.day) == month_day:
        return f"{years} year{'s' if years > 1 else ''}"
    return None


def login_milestone(conn, player: str, now: _dt.datetime, offset: int = 0) -> Optional[str]:
    """A streak or anniversary worth announcing at this login, at most once per player per
    day (remembered in meta), or None."""
    today = now.date()
    key = f"login_milestone:{player.lower()}"
    if get_meta(conn, key) == today.isoformat():
        return None
    detail = None
    n = streak(conn, player, today, offset)
    if n in STREAK_MARKS:
        detail = f"is on a **{n}-day streak** 🔥"
    first = _one(conn, "SELECT MIN(login_at) AS t FROM play_sessions WHERE player = ? COLLATE NOCASE", (player,))
    if not detail and first and first["t"]:
        when = anniversary(_dt.datetime.fromtimestamp(first["t"] + offset).astimezone().date(), today)
        if when:
            detail = f"first set sail here **{when} ago** today 🎂"
    if detail:
        set_meta(conn, key, today.isoformat())
    return detail


# ---------------------------------------------------------------------------
# When people play (/muninn when)
# ---------------------------------------------------------------------------
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SPARK = "·▁▂▃▄▅▆▇█"


def online_grid(conn, offset: int, weeks: int = 4, player: str = "", now: Optional[float] = None) -> list:
    """7 × 24 (Monday first, local hours): how many players were online on average in each
    hour of the week over the last `weeks` weeks (one player's share of the hour, for
    `player`). Log times + offset = real time."""
    now = time.time() if now is None else now
    since = now - weeks * 7 * 86400
    grid = [[0.0] * 24 for _ in range(7)]
    sql = ("SELECT login_at, COALESCE(logout_at, last_seen_at) AS until FROM play_sessions "
           "WHERE COALESCE(logout_at, last_seen_at) > ? AND login_at < ?")
    params = [since - offset, now - offset]
    if player:
        sql += " AND player = ? COLLATE NOCASE"
        params.append(player)
    for r in _rows(conn, sql, params):
        t, end = max(r["login_at"] + offset, since), min((r["until"] or r["login_at"]) + offset, now)
        while t < end:
            local = _dt.datetime.fromtimestamp(t).astimezone()
            hour_end = (local.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(hours=1)).timestamp()
            step = min(end, hour_end) - t
            grid[local.weekday()][local.hour] += step / 3600
            t += max(step, 1)
    return [[v / weeks for v in row] for row in grid]


def busiest(grid: list, span: int = 2) -> tuple:
    """(weekday, start hour, average online) of the busiest `span`-hour stretch."""
    best = (0, 0, 0.0)
    for d in range(7):
        for h in range(24):
            avg = sum(grid[(d + (h + i) // 24) % 7][(h + i) % 24] for i in range(span)) / span
            if avg > best[2]:
                best = (d, h, avg)
    return best


def render_when(grid: list, weeks: int, player: str = "", server_name: str = "") -> dict:
    """/muninn when: a text heatmap (2-hour columns), the busiest times and a suggestion."""
    cols = [[(row[h] + row[h + 1]) / 2 for h in range(0, 24, 2)] for row in grid]
    peak = max(max(r) for r in cols)
    if peak <= 0:
        who = f"**{player}** hasn't" if player else "Nobody has"
        return {"title": "🕰️ When people play", "color": 0x5865F2,
                "description": f"{who} played in the last {weeks} week{'s' if weeks != 1 else ''}."}
    lines = ["     " + " ".join(f"{h:02d}" for h in range(0, 24, 2))]
    for d, row in enumerate(cols):
        lines.append(f"{WEEKDAYS[d]}  " + " ".join(
            (SPARK[0] if v <= 0 else SPARK[1 + min(7, round(v / peak * 7))]) * 2 for v in row))
    hours = sorted(((grid[d][h], d, h) for d in range(7) for h in range(24)), reverse=True)[:3]
    d, h, avg = busiest(grid)
    unit = "of the time" if player else "online on average"
    if player:
        top = " · ".join(f"{WEEKDAYS[d_]} {h_:02d}:00 ({v:.0%})" for v, d_, h_ in hours if v > 0)
        tip = f"Most likely on: **{WEEKDAYS[d]} {h:02d}:00–{(h + 2) % 24:02d}:00** ({avg:.0%} {unit})."
    else:
        top = " · ".join(f"{WEEKDAYS[d_]} {h_:02d}:00 ({v:.1f})" for v, d_, h_ in hours if v > 0)
        tip = (f"Best time for a game night: **{WEEKDAYS[d]} {h:02d}:00–{(h + 2) % 24:02d}:00** "
               f"({avg:.1f} {unit}).")
    title = f"🕰️ When {player} plays" if player else f"🕰️ When people play{' on ' + server_name if server_name else ''}"
    return {"title": title, "color": 0x5865F2,
            "description": "```\n" + "\n".join(lines) + "\n```\n" + tip + f"\nBusiest hours: {top}",
            "footer": {"text": f"Last {weeks} week{'s' if weeks != 1 else ''} · server time · "
                               + ("share of each hour they were on" if player else "average players online per hour")}}


def render_compare(a: dict, b: dict) -> dict:
    """/muninn compare: two player_stats() side by side, with the leader of each row marked."""
    rows = [("Time played", "seconds", _dur), ("Visits", "sessions", str), ("Longest session", "longest", _dur),
            ("Deaths", "deaths", str)]
    lines = []
    for label, key, fmt in rows:
        x, y = a.get(key) or 0, b.get(key) or 0
        mark = (" ⬅️", "") if x > y else ("", " ➡️") if y > x else ("", "")
        lines.append(f"**{label}**: {fmt(x)}{mark[0]} · {fmt(y)}{mark[1]}")
    ax, bx = (a.get("steam") or {}).get("unlocked"), (b.get("steam") or {}).get("unlocked")
    if ax is not None or bx is not None:
        lines.append(f"**Achievements**: {ax if ax is not None else '-'} · {bx if bx is not None else '-'}")
    return {"title": f"⚔️ {a['player']} vs {b['player']}", "color": 0xC27C0E, "description": "\n".join(lines)}


def uptime(conn, since: int, until: int) -> dict:
    """Share of [since, until] the server was up, from the "up"/"down" server events
    (log timestamps). {"fraction", "restarts", "down_seconds"}."""
    state = _one(conn, "SELECT kind FROM server_events WHERE kind IN ('up', 'down') AND at < ? "
                       "ORDER BY at DESC, id DESC LIMIT 1", (since,))
    up, t, down, restarts = (state or {}).get("kind", "up") == "up", since, 0, 0
    for r in _rows(conn, "SELECT at, kind FROM server_events WHERE kind IN ('up', 'down') AND at >= ? AND at <= ? "
                         "ORDER BY at, id", (since, until)):
        if not up:
            down += r["at"] - t
        if r["kind"] == "down" and up:
            restarts += 1
        up, t = r["kind"] == "up", r["at"]
    if not up:
        down += until - t
    span = max(until - since, 1)
    return {"fraction": max(0.0, 1 - down / span), "restarts": restarts, "down_seconds": down}


# ---------------------------------------------------------------------------
# The progress board: achievement counts players share from /valheim progress
# ---------------------------------------------------------------------------
def save_builders(conn, counts: list, now: Optional[float] = None) -> None:
    """Replace the builders table with [(character, pieces)] from the latest world save; a
    character is stored under the log's spelling when it has played here."""
    when = int(now if now is not None else time.time())
    rows = {}
    for name, n in counts:
        player = known_player(conn, name) or name
        rows[player] = rows.get(player, 0) + int(n)
    conn.execute("DELETE FROM builders")
    conn.executemany("INSERT INTO builders(player, pieces, updated_at) VALUES (?,?,?)",
                     [(p, n, when) for p, n in rows.items()])
    conn.commit()


def record_death_spots(conn, tombstones: list, now: Optional[float] = None) -> int:
    """Remember tombstones from a world save (world_objects.scan); each only once. Returns
    how many were new."""
    when = int(now if now is not None else time.time())
    before = conn.total_changes
    for t in tombstones:
        died = round(t["died"] or 0)
        # A tombstone can drift (floating in water, dug ground): with a time of death, that
        # time says it's the same one, wherever it lies now.
        if died and _one(conn, "SELECT 1 AS x FROM death_spots WHERE owner = ? AND died = ?", (t["owner"], died)):
            continue
        conn.execute("INSERT OR IGNORE INTO death_spots(owner, died, x, z, seen_at) VALUES (?,?,?,?,?)",
                     (t["owner"], died, round(t["x"]), round(t["z"]), when))
    conn.commit()
    return conn.total_changes - before


def death_spots(conn, player: str = "") -> list:
    """[{"owner", "died", "x", "z"}] recorded so far, one character's with `player`."""
    if player:
        return _rows(conn, "SELECT owner, died, x, z FROM death_spots WHERE owner = ? COLLATE NOCASE", (player,))
    return _rows(conn, "SELECT owner, died, x, z FROM death_spots")


def progress_counts(sections: list) -> dict:
    """{list key: [done, total]} from fch_progress.report(); the counts only, no item names."""
    return {s["key"]: [len(s["done"]), s["total"]] for s in sections}


def progress_entry(conn, player: str) -> Optional[dict]:
    import json
    r = _one(conn, "SELECT * FROM fch_progress WHERE player = ? COLLATE NOCASE", (player,))
    if r:
        r["sections"] = json.loads(r["sections"] or "{}")
    return r


def save_progress(conn, player: str, user_id, counts: dict, now: Optional[float] = None,
                  force: bool = False) -> tuple:
    """Put a character's counts on the board, or update them. Returns (error or None, the
    list keys finished since the last upload). A first upload finishes nothing, so joining
    the board doesn't announce lists done long ago; nor does a list the last upload didn't
    have (one added in an update). force: take over an entry someone else uploaded (the
    character's linked player, or an admin)."""
    import json
    old = progress_entry(conn, player)
    if old and old["user_id"] != str(user_id) and not force:
        return (f"**{old['player']}** is on the board for <@{old['user_id']}>. If it's your character, link it "
                f"with `/valheim link` and upload again, or ask an admin."), []
    finished = [k for k, (done, total) in counts.items()
                if old and total and done >= total and k in old["sections"] and old["sections"][k][0] < total]
    done, total = sum(c[0] for c in counts.values()), sum(c[1] for c in counts.values())
    conn.execute("INSERT INTO fch_progress(player, user_id, done, total, sections, updated_at) VALUES (?,?,?,?,?,?) "
                 "ON CONFLICT(player) DO UPDATE SET user_id=excluded.user_id, done=excluded.done, "
                 "total=excluded.total, sections=excluded.sections, updated_at=excluded.updated_at",
                 (old["player"] if old else player, str(user_id), done, total, json.dumps(counts),
                  int(now if now is not None else time.time())))
    conn.commit()
    return None, finished


def drop_progress(conn, player: str, user_id, force: bool = False) -> bool:
    """Take a character off the board: its uploader can, and with force (the character's
    linked player, or an admin) anyone's entry."""
    if force:
        n = conn.execute("DELETE FROM fch_progress WHERE player = ? COLLATE NOCASE", (player,)).rowcount
    else:
        n = conn.execute("DELETE FROM fch_progress WHERE player = ? COLLATE NOCASE AND user_id = ?",
                         (player, str(user_id))).rowcount
    conn.commit()
    return n > 0


def progress_players(conn, prefix: str = "", limit: int = 25) -> list:
    """Characters on the progress board, for autocomplete."""
    return [r["player"] for r in _rows(conn, "SELECT player FROM fch_progress WHERE player LIKE ? "
                                             "ORDER BY player COLLATE NOCASE LIMIT ?", (f"%{prefix.strip()}%", limit))]


def progress_board(conn, key: str = "", limit: int = 15) -> list:
    """[{"player", "user_id", "done", "total", "updated_at"}], best first: by everything,
    or by one list (key)."""
    import json
    rows = _rows(conn, "SELECT * FROM fch_progress")
    out = []
    for r in rows:
        if key:
            done, total = (json.loads(r["sections"] or "{}").get(key) or [0, 0])
            if not total:
                continue
            r = dict(r, done=done, total=total)
        out.append(r)
    out.sort(key=lambda r: (-r["done"], r["player"].lower()))
    return out[:limit]


def render_progress_board(rows: list, key: str = "", title: str = "", emoji: str = "📜") -> dict:
    medals = ("🥇", "🥈", "🥉")
    lines = []
    for i, r in enumerate(rows):
        pct = round(100 * r["done"] / r["total"]) if r["total"] else 0
        mark = " ✅" if r["total"] and r["done"] >= r["total"] else ""
        lines.append(f"{medals[i] if i < 3 else f'`{i + 1:>2}.`'} **{r['player']}**: {r['done']}/{r['total']} "
                     f"({pct}%){mark} · <t:{r['updated_at']}:R>")
    return {"title": f"{emoji} Achievement progress" + (f": {title}" if key else ""), "color": 0x1ABC9C,
            "description": "\n".join(lines) or "Nobody's on the board yet. Upload your character with "
                                               "`/valheim progress` and `board:True` to join.",
            "footer": {"text": "From each player's last /valheim progress with board:True · counts only"}}


# ---------------------------------------------------------------------------
# Honors: roles for deeds the log can't see (/odin honor)
# ---------------------------------------------------------------------------
# (name, emoji, what it's for, why the name, colour). Admins add their own with
# /odin honor create.
HONORS = [
    ("Hermóðr", "⚰️", "crypt raider", "rode down to Hel's realm and came back", 0x546E7A),
    ("Andhrímnir", "🍲", "the cook", "the cook of Valhalla, who roasts the boar Sæhrímnir every night", 0xE67E22),
    ("Svaðilfari", "🏰", "the builder", "the giant stallion that hauled the stone for Asgard's wall", 0x95A5A6),
    ("Freyr", "🌾", "the farmer", "god of harvest and good seasons", 0x2ECC71),
    ("Gangleri", "🧭", "the explorer", "\"the Wanderer\", Odin's name when travelling in disguise", 0x1ABC9C),
    ("Brokkr", "⚒️", "smith and crafter", "the dwarf who forged Mjölnir", 0xA84300),
    ("Dvalinn", "⛏️", "the miner", "the dwarf master of stone and ore", 0x7F8C8D),
    ("Njörðr", "⛵", "sailor and captain", "god of the sea and ships", 0x3498DB),
    ("Rán", "🎣", "the fisher", "the sea goddess who catches sailors in her net", 0x206694),
    ("Ægir", "🍺", "the brewer", "the sea giant who brews ale for the gods", 0xC27C0E),
    ("Ullr", "🏹", "hunter and archer", "the bow-and-ski hunter god", 0x11806A),
    ("Angrboða", "🐺", "the tamer", "mother of Fenrir, mistress of beasts", 0x71368A),
    ("Eir", "🩹", "healer and support", "goddess of healing", 0xFF6B9D),
    ("Týr", "🛡️", "champion fighter", "god of courage, who gave his hand to bind Fenrir", 0x992D22),
    ("Fáfnir", "💰", "the hoarder", "the dragon who slept on his heap of gold", 0xF1C40F),
    ("Loki", "🃏", "agent of chaos", "the trickster everyone loves to blame", 0x9B59B6),
]


def honor_key(name: str) -> str:
    """"Hermóðr" -> "hermodr": how honors are looked up, ignoring case and accents."""
    import unicodedata
    folded = unicodedata.normalize("NFKD", name.replace("ð", "d").replace("Ð", "d").replace("æ", "ae")
                                   .replace("Æ", "ae").replace("ø", "o").replace("þ", "th"))
    return re.sub(r"[^a-z0-9]", "", folded.encode("ascii", "ignore").decode().lower())[:40]


def seed_honors(conn) -> None:
    """Add the built-in honors that aren't there yet (never overwrites an edited one)."""
    for name, emoji, what, why, color in HONORS:
        conn.execute("INSERT OR IGNORE INTO honors(key, name, emoji, description, color, builtin, created_at) "
                     "VALUES (?,?,?,?,?,1,?)", (honor_key(name), name, emoji, f"{what}: {why}", color,
                                                int(time.time())))
    conn.commit()


def honors(conn, prefix: str = "") -> list:
    """Every honor, with how many hold it: built-in ones first (in their order), then custom."""
    seed_honors(conn)
    order = {honor_key(h[0]): i for i, h in enumerate(HONORS)}
    rows = _rows(conn, "SELECT h.*, (SELECT COUNT(*) FROM honor_holders x WHERE x.honor_key = h.key) AS holders "
                       "FROM honors h")
    p = honor_key(prefix)
    rows = [r for r in rows if p in r["key"] or prefix.lower() in (r["description"] or "").lower()]
    return sorted(rows, key=lambda r: (order.get(r["key"], 999), r["name"].lower()))


def get_honor(conn, name_or_key: str) -> Optional[dict]:
    seed_honors(conn)
    return _one(conn, "SELECT * FROM honors WHERE key = ?", (honor_key(name_or_key),))


def create_honor(conn, name: str, emoji: str, description: str, color: int, created_by,
                 role_id=None) -> Optional[str]:
    """A new custom honor. Returns an error, or None."""
    key = honor_key(name)
    if not key:
        return "That name needs at least one letter or number."
    if get_honor(conn, key):
        return f"There's already an honor called **{get_honor(conn, key)['name']}**."
    conn.execute("INSERT INTO honors(key, name, emoji, description, color, role_id, builtin, created_by, created_at) "
                 "VALUES (?,?,?,?,?,?,0,?,?)", (key, name.strip()[:60], (emoji or "🏅").strip()[:8],
                                                (description or "").strip()[:200], color,
                                                str(role_id) if role_id else None, str(created_by), int(time.time())))
    conn.commit()
    return None


def delete_honor(conn, key: str) -> None:
    conn.execute("DELETE FROM honor_holders WHERE honor_key = ?", (key,))
    conn.execute("DELETE FROM honors WHERE key = ?", (key,))
    conn.commit()


def set_honor_role(conn, key: str, role_id) -> None:
    conn.execute("UPDATE honors SET role_id = ? WHERE key = ?", (str(role_id), key))
    conn.commit()


def give_honor(conn, key: str, user_id, given_by, note: str = "") -> bool:
    """False if they already hold it."""
    cur = conn.execute("INSERT OR IGNORE INTO honor_holders(honor_key, user_id, given_at, given_by, note) "
                       "VALUES (?,?,?,?,?)", (key, str(user_id), int(time.time()), str(given_by), note[:200] or None))
    conn.commit()
    return cur.rowcount == 1


def take_honor(conn, key: str, user_id) -> bool:
    cur = conn.execute("DELETE FROM honor_holders WHERE honor_key = ? AND user_id = ?", (key, str(user_id)))
    conn.commit()
    return cur.rowcount == 1


def honor_holders(conn, key: str) -> list:
    return _rows(conn, "SELECT * FROM honor_holders WHERE honor_key = ? ORDER BY given_at", (key,))


def user_honors(conn, user_id) -> list:
    """[{"name", "emoji", "note", "given_at"}] a Discord user holds."""
    return _rows(conn, "SELECT h.name, h.emoji, x.note, x.given_at FROM honor_holders x JOIN honors h "
                       "ON h.key = x.honor_key WHERE x.user_id = ? ORDER BY x.given_at", (str(user_id),))


def render_honor_given(h: dict, user_id, note: str = "") -> dict:
    what = (h.get("description") or "").split(":")[0]
    desc = f"<@{user_id}> is honored as **{h['name']}**" + (f", {what}" if what else "") + "."
    if note:
        desc += f"\n*{note}*"
    return {"title": f"{h.get('emoji') or '🏅'} A new {h['name']}!", "description": desc,
            "color": h.get("color") or 0xC27C0E}


def render_honors(conn, user_id=None) -> dict:
    """/muninn honors: every honor and its holders, or one member's honors."""
    if user_id is not None:
        mine = user_honors(conn, user_id)
        lines = [f"{h['emoji'] or '🏅'} **{h['name']}**" + (f": *{h['note']}*" if h["note"] else "") for h in mine]
        return {"title": "🏅 Honors", "color": 0xC27C0E,
                "description": f"<@{user_id}>\n" + ("\n".join(lines) or "No honors yet.")}
    lines = []
    for h in honors(conn):
        who = ", ".join(f"<@{x['user_id']}>" for x in honor_holders(conn, h["key"])[:10]) or "—"
        what = (h["description"] or "").split(":")[0]
        lines.append(f"{h['emoji'] or '🏅'} **{h['name']}** ({what or 'custom'}): {who}")
    return {"title": "🏅 Honors of the realm", "color": 0xC27C0E, "description": "\n".join(lines)[:4000],
            "footer": {"text": "Given by the admins: /odin honor give, vote, or a bounty"}}


# ---------------------------------------------------------------------------
# Bounties
# ---------------------------------------------------------------------------
def create_bounty(conn, title: str, reward: str, days: float, creator_id, now: Optional[float] = None) -> int:
    now = int(time.time() if now is None else now)
    cur = conn.execute("INSERT INTO bounties(title, reward, created_at, expires_at, creator_id) VALUES (?,?,?,?,?)",
                       (title, reward or None, now, now + int(days * 86400), str(creator_id)))
    conn.commit()
    return cur.lastrowid


def set_bounty_message(conn, bounty_id: int, channel_id, message_id) -> None:
    conn.execute("UPDATE bounties SET channel_id = ?, message_id = ? WHERE id = ?",
                 (str(channel_id), str(message_id), bounty_id))
    conn.commit()


def get_bounty(conn, bounty_id: int) -> Optional[dict]:
    return _one(conn, "SELECT * FROM bounties WHERE id = ?", (bounty_id,))


def open_bounties(conn, prefix: str = "") -> list:
    return _rows(conn, "SELECT * FROM bounties WHERE status = 'open' AND title LIKE ? ORDER BY expires_at",
                 (f"%{prefix.strip()}%",))


def finish_bounty(conn, bounty_id: int, status: str, winner_id=None, now: Optional[float] = None) -> bool:
    """Close an open bounty: 'done' (with a winner), 'expired' or 'closed'. False if it
    wasn't open any more (someone else got there first)."""
    cur = conn.execute("UPDATE bounties SET status = ?, winner_id = ?, done_at = ? WHERE id = ? AND status = 'open'",
                       (status, str(winner_id) if winner_id else None, int(time.time() if now is None else now),
                        bounty_id))
    conn.commit()
    return cur.rowcount == 1


def expired_bounties(conn, now: Optional[float] = None) -> list:
    return _rows(conn, "SELECT * FROM bounties WHERE status = 'open' AND expires_at <= ?",
                 (int(time.time() if now is None else now),))


def bounty_hunters(conn, since: int = 0) -> list:
    """[{"user_id", "n"}]: who claimed the most bounties since `since`."""
    return _rows(conn, "SELECT winner_id AS user_id, COUNT(*) AS n FROM bounties WHERE status = 'done' "
                       "AND done_at >= ? GROUP BY winner_id ORDER BY n DESC, MIN(done_at)", (since,))


def render_bounty(b: dict, honor: Optional[dict] = None) -> dict:
    """The bounty post: the challenge, reward (and honor), deadline and how it ended."""
    lines = []
    if b.get("reward"):
        lines.append(f"**Reward:** {b['reward']}")
    if honor:
        lines.append(f"**Honor:** {honor.get('emoji') or '🏅'} {honor['name']}, for good")
    if b["status"] == "open":
        lines.append(f"**Ends** <t:{b['expires_at']}:R>")
        lines.append("Done it? Press **🎯 I did it** and an admin will confirm.")
        color, head = 0xC27C0E, "🎯 Bounty"
    elif b["status"] == "done":
        lines.append(f"🏆 Claimed by <@{b['winner_id']}>")
        color, head = 0x57F287, "🏆 Bounty claimed"
    else:
        lines.append("⌛ Nobody claimed it in time." if b["status"] == "expired" else "Closed by an admin.")
        color, head = 0x95A5A6, "🎯 Bounty (closed)"
    return {"title": f"{head}: {b['title']}"[:256], "description": "\n".join(lines), "color": color,
            "footer": {"text": f"Bounty #{b['id']}"}}


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


def parse_whens(text: str, now: _dt.datetime) -> list:
    """Several times for a time poll: "sat 20:00, sun 18:00" or "8pm or tomorrow 8pm".
    Returns them sorted, without duplicates; raises ValueError on one it can't read."""
    parts = [p.strip() for p in re.split(r",|;|\bor\b", text, flags=re.IGNORECASE) if p.strip()]
    times = sorted({parse_when(p, now) for p in parts})
    return times


def poll_winner(results: list) -> Optional[int]:
    """results: [(votes, start time)] in the poll's order. The most votes wins, the earlier
    time on a tie; None when nobody voted."""
    voted = [(v, at) for v, at in results if v]
    if not voted:
        return None
    best = max(v for v, _ in voted)
    return min(at for v, at in voted if v == best)


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
def _days(n: int) -> str:
    return f"{n} day{'s' if n != 1 else ''}"


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
    """Embed for /muninn stats. Log times + offset = real time, for Discord timestamps."""
    fields = [
        {"name": "Time played", "value": _dur(s["seconds"]), "inline": True},
        {"name": "Rank", "value": f"#{s['rank']} of {s['players']}", "inline": True},
        {"name": "Visits", "value": str(s["sessions"]), "inline": True},
        {"name": "Longest session", "value": _dur(s["longest"]), "inline": True},
        {"name": "Deaths", "value": str(s["deaths"]), "inline": True},
        {"name": "Deaths per hour", "value": f"{s['deaths'] / max(s['seconds'] / 3600, 1e-9):.1f}"
                                              if s["seconds"] >= 600 else "-", "inline": True},
    ]
    if s.get("best_streak"):
        best = f"best {_days(s['best_streak'])}"
        fields.append({"name": "🔥 Play streak", "inline": True,
                       "value": f"{_days(s['streak'])} now · {best}" if s.get("streak") else best})
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
        value = str(r["v"]) if category in ("deaths", "sessions", "achievements") else \
            _days(r["v"]) if category == "streak" else _dur(r["v"])
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
