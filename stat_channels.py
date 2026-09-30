"""
Stat channels: locked voice channels whose names show the server's numbers, e.g.
"🔑 Join code: 482913", "💀 Deaths this week: 14" or "⚡ Thor (longest session): Ingrid".
The bot creates them in three categories (live / this week / titles, or one with
layout "single") and renames them; members can see them but not join.

This module only works out the names (plain functions, testable without Discord).
admin_bot.AdminBot creates the channels and applies the names within Discord's
rename limit (2 per channel per 10 minutes).
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Callable, Optional

# The stat channels, in three groups (each its own category, or one category with
# layout "single"). key -> what it shows, for docs and logs. Order = order in Discord.
GROUPS = [
    ("watch", "🛡️ Heimdall's Watch · live", {
        "players": "who's online, by name",
        "server": "online/offline, version, update waiting",
        "join_code": "the crossplay join code",
        "uptime": "time since the server started",
        "day": "the in-game day (updated when everyone sleeps)",
        "saved": "the last world save and how long it took",
        "backup": "when Valheim last made a world backup",
        "disk": "free space on the save disk",
    }),
    ("saga", "📜 The Saga · this week", {
        "peak_today": "most players online at once today",
        "hours_week": "hours played this week, all players together",
        "deaths_week": "deaths this week",
        "uptime_week": "share of this week the server was up, and restarts",
        "last_raid": "the most recent raid",
        "next_plan": "the next game night (/warcouncil plan)",
        "vikings": "characters that have ever played",
        "achievements": "Steam achievements unlocked, all players together",
    }),
    ("hall", "👑 Hall of Champions · titles", {
        "title_owner": "Odin: the Discord server's owner",
        "title_time": "Heimdall: most time played",
        "title_deaths": "Hel: most deaths",
        "title_sessions": "Sleipnir: most visits",
        "title_longest": "Thor: longest single session",
        "title_achievements": "Bragi: most Steam achievements",
    }),
]
STATS = {k: v for _, _, keys in GROUPS for k, v in keys.items()}
GROUP_OF = {k: g for g, _, keys in GROUPS for k in keys}
# Older names still accepted in stat_channels.show.
ALIASES = {"titles": [k for k in STATS if k.startswith("title_")]}
NEEDS_DB = {"peak_today", "hours_week", "deaths_week", "uptime_week", "vikings", "next_plan", "achievements"} | \
    {k for k in STATS if k.startswith("title_") and k != "title_owner"}
# title_<category> -> (emoji, what it's for); the role names come from community.TITLES.
TITLE_LABELS = {
    "title_owner": ("👁️", "server owner"),
    "title_time": ("🛡️", "most hours"),
    "title_deaths": ("💀", "most deaths"),
    "title_sessions": ("🐎", "most visits"),
    "title_longest": ("⚡", "longest session"),
    "title_achievements": ("📜", "most achievements"),
}


def expand(show) -> list:
    """stat_channels.show with aliases expanded, unknown and repeated keys dropped."""
    out = []
    for k in show:
        for key in ALIASES.get(k, [k]):
            if key in STATS and key not in out:
                out.append(key)
    return out


def names_list(names: list, prefix: str, limit: int = 100) -> str:
    """"🟢 3 online: Ingrid, Bjorn, Sigrid", cut to fit Discord's 100 characters with "+N more"."""
    for shown in range(len(names), 0, -1):
        more = len(names) - shown
        text = prefix + ", ".join(names[:shown]) + (f" +{more} more" if more else "")
        if len(text) <= limit:
            return text
    return (prefix + f"{len(names)} players")[:limit]


def week_start(now: _dt.datetime) -> _dt.datetime:
    """Monday 00:00 of this week, in `now`'s time zone."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - _dt.timedelta(days=day.weekday())


def when(ts: float, now: _dt.datetime) -> str:
    """A short local time: "today 04:10", "yesterday 23:55", "Tue 20:00", "Sep 28"."""
    t = _dt.datetime.fromtimestamp(ts, now.tzinfo)
    days = (t.date() - now.date()).days
    if days == 0:
        return f"today {t:%H:%M}"
    if days == -1:
        return f"yesterday {t:%H:%M}"
    if days == 1:
        return f"tomorrow {t:%H:%M}"
    if -6 <= days <= 6:
        return f"{t:%a %H:%M}"
    return f"{t:%b} {t.day}"


def uptime(seconds: float) -> str:
    """Coarse on purpose, so the channel isn't renamed every minute: 3 d / 5 h / <1 h."""
    if seconds >= 48 * 3600:
        return f"{int(seconds // 86400)} d"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} h"
    return "<1 h"


def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None


def name_for(key: str, snap: Optional[dict], conn=None, now: Optional[_dt.datetime] = None,
             offset: int = 0, update_waiting: bool = False, titles: Optional[dict] = None) -> Optional[str]:
    """The channel name for one stat, or None when there's nothing to show yet (the
    channel keeps its current name).

    snap: extras.LiveState.snapshot(); conn: the stats database; offset: real time minus
    the log's clock (log timestamps + offset = real); titles: {"title_time": ("Heimdall",
    "Ingrid"), "title_owner": ("Odin", "Alain"), …}, holder None when nobody has it yet."""
    now = now or _dt.datetime.now().astimezone()
    snap = snap or {}
    down, known = snap.get("down"), snap.get("known")
    if key == "players":
        if down:
            return "🔴 Nobody online: server down"
        if not known:
            return None
        names = [n for n, _ in snap.get("online") or []]
        count = max(snap.get("count") or 0, len(names))
        if not count:
            return "⚫ Nobody online"
        if not names:
            return f"🟢 {count} online"
        unnamed = count - len(names)                 # online since before the monitor started
        return names_list(names + ([f"{unnamed} more"] if unnamed else []), f"🟢 {count} online: ")
    if key == "saved":
        if not snap.get("last_save"):
            return None
        ms = snap.get("last_save_ms")
        took = f" ({ms / 1000:.1f} s)" if ms else ""
        return f"💾 World saved {when(snap['last_save'], now)}{took}"
    if key == "disk":
        free = snap.get("disk_free")
        if not free:
            return None
        gb = free / 1024 ** 3
        return f"💽 Disk free: {gb:.0f} GB" if gb >= 10 else f"💽 Disk free: {gb:.1f} GB"
    if key == "server":
        if down:
            return "🔴 Server offline"
        if not known and not snap.get("version"):
            return None
        version = f" · {snap['version']}" if snap.get("version") else ""
        return f"⬆️ Update waiting{version}" if update_waiting else f"🟢 Server online{version}"
    if key == "join_code":
        if down:
            return "🔑 Join code: server offline"
        code = snap.get("join_code")
        return f"🔑 Join code: {code}" if code else "🔑 Join code: after next join"
    if key == "day":
        return f"☀️ Day {snap['day']}" if snap.get("day") else "☀️ Day: after the next sleep"
    if key == "uptime":
        if down:
            return "⏱ Down"
        if not snap.get("up_since"):
            return None
        return f"⏱ Up {uptime(now.timestamp() - snap['up_since'])}"
    if key == "backup":
        return f"🗄 Backup: {when(snap['last_backup'], now)}" if snap.get("last_backup") else "🗄 Backup: none yet"
    if key == "last_raid":
        if not snap.get("last_raid"):
            return None
        raid, at = snap["last_raid"]
        raid = raid.split(" (")[0]                      # "The Elder's army (greydwarves)" -> "The Elder's army"
        return f"⚔️ Last raid: {raid} ({when(at, now).split(' ')[0]})"
    if key.startswith("title_"):
        emoji, what = TITLE_LABELS[key]
        role, holder = (titles or {}).get(key, (None, None))
        if not role:
            return None
        return f"{emoji} {role} ({what}): {holder or '—'}"[:100]
    if conn is None:
        return None
    # The database stores log timestamps; log = real - offset.
    if key == "peak_today":
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - offset
        peak = _one(conn, "SELECT MAX(count) FROM concurrency WHERE at >= ?", (int(midnight),))
        return f"📈 Peak today: {max(peak or 0, snap.get('count') or 0)}"
    if key == "hours_week":
        since = week_start(now).timestamp() - offset
        secs = _one(conn, "SELECT COALESCE(SUM(duration_seconds), 0) FROM play_sessions WHERE login_at >= ?",
                    (int(since),)) or 0
        return f"⏳ This week: {int(secs // 3600)} h played"
    if key == "deaths_week":
        since = week_start(now).timestamp() - offset
        n = _one(conn, "SELECT COUNT(*) FROM deaths WHERE died_at >= ?", (int(since),)) or 0
        return f"💀 Deaths this week: {n}"
    if key == "uptime_week":
        import community
        since = int(week_start(now).timestamp() - offset)
        u = community.uptime(conn, since, max(int(now.timestamp() - offset), since + 1))
        pct = f"{u['fraction'] * 100:.1f}".rstrip("0").rstrip(".")
        restarts = f" · {u['restarts']} restart{'s' if u['restarts'] != 1 else ''}" if u["restarts"] else ""
        return f"📶 Up {pct}% this week{restarts}"
    if key == "vikings":
        n = _one(conn, "SELECT COUNT(DISTINCT player) FROM play_sessions") or 0
        return f"🧭 {n} Viking{'s have' if n != 1 else ' has'} visited"
    if key == "next_plan":
        r = conn.execute("SELECT title, at FROM plans WHERE at >= ? ORDER BY at LIMIT 1",
                         (int(now.timestamp()) - 1800,)).fetchone()   # plans store real time
        if not r:
            return "📅 No game night planned"
        return f"📅 {r[0][:60]} · {when(r[1], now)}"
    if key == "achievements":
        try:
            n = _one(conn, "SELECT COALESCE(SUM(unlocked), 0) FROM steam_profile") or 0
        except Exception:  # noqa: BLE001  (a database from before the Steam tables)
            return None
        return f"🏅 {n} achievements unlocked"
    return None


class Renamer:
    """Per-channel rename pacing: Discord allows 2 renames per channel per 10 minutes,
    so each channel is renamed at most every `min_interval` seconds, to its latest name."""

    def __init__(self, min_interval: float = 300.0, clock: Callable[[], float] = time.time):
        self.min_interval, self.clock = min_interval, clock
        self.current: dict = {}              # channel key -> name it has now
        self.last: dict = {}                 # channel key -> when it was last renamed

    def due(self, key: str, wanted: Optional[str]) -> bool:
        if not wanted or wanted == self.current.get(key):
            return False
        return self.clock() - self.last.get(key, 0) >= self.min_interval

    def renamed(self, key: str, name: str) -> None:
        self.current[key], self.last[key] = name, self.clock()
