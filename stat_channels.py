"""
Stat channels: locked voice channels whose names show the server's numbers, e.g.
"🔑 Join code: 482913" or "💀 Deaths this week: 14". The bot creates them in their
own category and renames them; members can see them but not join.

This module only works out the names (plain functions, testable without Discord).
admin_bot.AdminBot creates the channels and applies the names within Discord's
rename limit (2 per channel per 10 minutes).
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Callable, Optional

# key -> (what it shows, for docs and logs). Order = order in the category.
STATS = {
    "players": "players online",
    "server": "online/offline, version, update waiting",
    "join_code": "the crossplay join code",
    "uptime": "time since the server started",
    "backup": "when Valheim last made a world backup",
    "peak_today": "most players online at once today",
    "hours_week": "hours played this week, all players together",
    "deaths_week": "deaths this week",
    "last_raid": "the most recent raid",
    "vikings": "characters that have ever played",
    "next_plan": "the next game night (/valheim plan)",
    "titles": "title holders, one at a time",
    "achievements": "Steam achievements unlocked, all players together",
}
NEEDS_DB = {"peak_today", "hours_week", "deaths_week", "vikings", "next_plan", "titles", "achievements"}


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
             offset: int = 0, update_waiting: bool = False, titles: Optional[list] = None) -> Optional[str]:
    """The channel name for one stat, or None when there's nothing to show yet (the
    channel keeps its current name).

    snap: extras.LiveState.snapshot(); conn: the stats database; offset: real time minus
    the log's clock (log timestamps + offset = real); titles: [(role, player)] of the
    current title holders, for the rotating titles channel."""
    now = now or _dt.datetime.now().astimezone()
    snap = snap or {}
    down, known = snap.get("down"), snap.get("known")
    if key == "players":
        if down:
            return "🔴 Valheim: offline"
        if not known:
            return None
        n = snap.get("count") or 0
        return f"🟢 Valheim: {n} online" if n else "🟢 Valheim: empty"
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
    if key == "uptime":
        if down:
            return "⏱ Down"
        if not snap.get("up_since"):
            return None
        return f"⏱ Up {uptime(now.timestamp() - snap['up_since'])}"
    if key == "backup":
        return f"💾 Backup: {when(snap['last_backup'], now)}" if snap.get("last_backup") else "💾 Backup: none yet"
    if key == "last_raid":
        if not snap.get("last_raid"):
            return None
        raid, at = snap["last_raid"]
        raid = raid.split(" (")[0]                      # "The Elder's army (greydwarves)" -> "The Elder's army"
        return f"⚔️ Last raid: {raid} ({when(at, now).split(' ')[0]})"
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
    if key == "vikings":
        n = _one(conn, "SELECT COUNT(DISTINCT player) FROM play_sessions") or 0
        return f"🧭 {n} Viking{'s have' if n != 1 else ' has'} visited"
    if key == "next_plan":
        r = conn.execute("SELECT title, at FROM plans WHERE at >= ? ORDER BY at LIMIT 1",
                         (int(now.timestamp()) - 1800,)).fetchone()   # plans store real time
        if not r:
            return "📅 No game night planned"
        return f"📅 {r[0][:60]} · {when(r[1], now)}"
    if key == "titles":
        held = [(role, player) for role, player in (titles or []) if player]
        if not held:
            return "👑 No titles yet"
        role, player = held[int(now.timestamp() // 600) % len(held)]   # a new one every 10 minutes
        return f"👑 {role}: {player}"
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
