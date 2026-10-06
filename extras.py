"""
Extras built on the parsed log: live server state (for the status board and
/muninn online), session summaries, milestones, the weekly recap, and copying
Valheim's own world backups to a second disk.

Everything here is fed by the monitor's main loop and needs no mods: it only uses
lines vanilla Valheim already writes to its console log.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("valheim-monitor.extras")


def fmt_duration(seconds: float) -> str:
    """2h 14m / 45m / 30s."""
    seconds = int(max(0, seconds))
    h, m = seconds // 3600, seconds % 3600 // 60
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") or n >= 100 else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.0f} TB"


def deaths_text(n: int) -> str:
    return "" if not n else " and died once" if n == 1 else f" and died {n} times"


# ---------------------------------------------------------------------------
# Live state
# ---------------------------------------------------------------------------
@dataclass
class LiveState:
    """What's happening on the server right now. Updated on the monitor thread;
    snapshot() hands a copy to the bot thread. Times are real epochs (time.time()),
    because they're shown with Discord's own relative timestamps."""

    online: dict = field(default_factory=dict)         # name -> since (real epoch) or None if unknown
    session_start: dict = field(default_factory=dict)  # name -> login ts (log clock), for summaries
    session_deaths: dict = field(default_factory=dict)
    count: Optional[int] = None
    down: bool = False
    up_since: Optional[float] = None
    version: Optional[str] = None
    last_save: Optional[float] = None
    last_save_ms: Optional[int] = None
    disk_free: Optional[int] = None                    # bytes free for the save dir, from each save
    last_backup: Optional[float] = None
    last_raid: Optional[tuple] = None                  # (name, real epoch)
    booted: bool = False                               # saw a boot, so the count starts at 0
    join_code: Optional[str] = None                    # crossplay join code; new on every restart
    server_ip: Optional[str] = None                    # "a.b.c.d:2456", when the server logs it
    day: Optional[int] = None                          # in-game day, logged when everyone sleeps
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, ev, now: Optional[float] = None) -> Optional[dict]:
        """Apply one event. For a logout with a known start, returns the session
        summary fields ({"duration", "duration_seconds", "deaths", "deaths_text"})."""
        now = time.time() if now is None else now
        k, name, ts = ev.kind, ev.player, ev.extra.get("ts")
        with self.lock:
            if "count" in ev.extra and ev.extra["count"] is not None:
                self.count = int(ev.extra["count"])
            if k == "login":
                self.online[name] = now
                self.session_start[name] = ts
                self.session_deaths[name] = 0
                self.down = False
            elif k == "respawn":
                self.online.setdefault(name, None)
            elif k == "death":
                self.online.setdefault(name, None)
                if name in self.session_deaths:
                    self.session_deaths[name] += 1
            elif k == "logout":
                self.online.pop(name, None)
                start = self.session_start.pop(name, None)
                deaths = self.session_deaths.pop(name, 0)
                if start is not None and ts is not None and not ev.extra.get("stale"):
                    secs = ts - start
                    return {"duration": fmt_duration(secs), "duration_seconds": secs,
                            "deaths": deaths, "deaths_text": deaths_text(deaths)}
            elif k == "join_code":
                self.join_code = ev.extra.get("code")
                self.server_ip = ev.extra.get("ip") or self.server_ip
            elif k == "server_restart":
                self.down, self.up_since, self.join_code = True, None, None
                self.online.clear()
                self.session_start.clear()
                self.session_deaths.clear()
                self.count = 0
            elif k == "server_online":
                self.down, self.up_since, self.count, self.booted = False, now, 0, True
                self.join_code = None                  # a restart gets a new code
                self.online.clear()
            elif k == "server_version":
                self.version = ev.extra.get("version")
            elif k == "world_day":
                self.day = ev.extra.get("day")
            elif k == "world_saved":
                self.last_save = now
                self.last_save_ms = ev.extra.get("ms")
            elif k == "disk_space":
                self.disk_free = ev.extra.get("avail")
            elif k == "backup_saved":
                self.last_backup = now
            elif k == "raid":
                self.last_raid = (ev.extra.get("raid"), now)
        return None

    def snapshot(self) -> dict:
        with self.lock:
            names = sorted(self.online.items(), key=lambda kv: (kv[1] is None, kv[1] or 0))
            count = self.count if self.count is not None else len(names)
            return {"online": names, "count": max(count, len(names)), "down": self.down,
                    "up_since": self.up_since, "version": self.version, "last_save": self.last_save,
                    "last_save_ms": self.last_save_ms, "disk_free": self.disk_free,
                    "last_backup": self.last_backup, "last_raid": self.last_raid,
                    "join_code": self.join_code, "server_ip": self.server_ip, "day": self.day,
                    # up_since alone isn't enough: it can come from reading old log lines.
                    "known": self.count is not None or bool(names) or self.booted}


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------
HOUR_MARKS = (10, 25, 50, 100, 250, 500, 1000)
DEATH_MARKS = (10, 25, 50, 100, 250, 500, 1000)


def hours_crossed(before_seconds: float, after_seconds: float, marks=HOUR_MARKS) -> Optional[int]:
    """The highest hour mark passed between two play-time totals, if any."""
    hit = [h for h in marks if before_seconds < h * 3600 <= after_seconds]
    return max(hit) if hit else None


def deaths_reached(total: int, marks=DEATH_MARKS) -> Optional[int]:
    return total if total in marks else None


# ---------------------------------------------------------------------------
# Weekly recap
# ---------------------------------------------------------------------------
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class WeeklyRecap:
    """Once a week (default Sunday 18:00, container local time) summarise the last 7 days."""

    def __init__(self, cfg: dict):
        day = str(cfg.get("day", "sunday")).lower()
        self.weekday = DAYS.index(day) if day in DAYS else 6
        self.hour = int(cfg.get("hour", 18))
        self.world = cfg.get("world", True) is not False       # "This week in the world" from the save

    def due(self, store, now: Optional[_dt.datetime] = None) -> Optional[str]:
        """The ISO week key to post for, or None. Remembered in the database, so a
        restart doesn't post twice and a missed Sunday is not posted late on Tuesday."""
        now = now or _dt.datetime.now().astimezone()
        if now.weekday() != self.weekday or now.hour < self.hour:
            return None
        y, w, _ = now.isocalendar()
        key = f"{y}-W{w:02d}"
        return None if store.get_meta("weekly_recap_week") == key else key

    @staticmethod
    def build(conn, log_now: int, server_name: str) -> dict:
        """An embed dict for the last 7 days, or None if nobody played."""
        import stats_db
        s = stats_db.period_summary(conn, log_now - 7 * 86400, log_now)
        if not s["players"]:
            return None
        medals = ("🥇", "🥈", "🥉")
        top = "\n".join(f"{medals[i] if i < 3 else '•'} **{r['player']}**: {fmt_duration(r['seconds'])}"
                        for i, r in enumerate(s["top_players"]))
        fields = [{"name": "Most time in-game", "value": top, "inline": False}]
        if s["deaths"]:
            d = s["deaths"][0]
            fields.append({"name": "Most deaths", "value": f"💀 **{d['player']}**: {d['deaths']}", "inline": True})
        if s["raids"]:
            fields.append({"name": "Raids", "value": "\n".join(f"⚔️ {r['detail']}" + (f" ×{r['n']}" if r["n"] > 1 else "")
                                                              for r in s["raids"][:5]), "inline": True})
        if s["new_players"]:
            fields.append({"name": "New vikings", "value": ", ".join(s["new_players"][:10]), "inline": False})
        explored = exploration(s.get("locations") or [])
        if explored:
            fields.append({"name": "Exploration", "value": explored, "inline": False})
        summary = (f"**{len(s['players'])}** vikings played **{fmt_duration(s['total_seconds'])}** in total, "
                   f"died **{s['total_deaths']}** times"
                   + (f", and peaked at **{s['peak']}** online at once." if s["peak"] else "."))
        return {"title": f"📜 This week in {server_name}", "description": summary, "fields": fields}

    @staticmethod
    def hours_by_day(conn, log_now: int, offset: int = 0, days: int = 7) -> list:
        """[(date, hours played)] for the last `days` days, oldest first. Sessions count on
        the day they started (log timestamps + offset = real time, in the local zone)."""
        today = _dt.datetime.fromtimestamp(log_now + offset).astimezone().date()
        start = today - _dt.timedelta(days=days - 1)
        totals = {start + _dt.timedelta(days=i): 0.0 for i in range(days)}
        since = int(_dt.datetime.combine(start, _dt.time()).astimezone().timestamp()) - offset
        for login, secs in conn.execute("SELECT login_at, COALESCE(duration_seconds, 0) FROM play_sessions "
                                        "WHERE login_at >= ?", (since,)):
            day = _dt.datetime.fromtimestamp(login + offset).astimezone().date()
            if day in totals:
                totals[day] += max(secs, 0) / 3600
        return sorted(totals.items())

    @staticmethod
    def chart(hours: list) -> Optional[bytes]:
        """A PNG bar chart of hours played per day, or None without Pillow or data."""
        if not hours or not any(h for _, h in hours):
            return None
        try:
            from io import BytesIO
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return None
        w, h, pad, top, bottom = 800, 360, 40, 50, 60
        img = Image.new("RGB", (w, h), (43, 45, 49))              # Discord's dark background
        draw = ImageDraw.Draw(img)
        try:
            font, small = ImageFont.load_default(size=20), ImageFont.load_default(size=16)
        except TypeError:                                          # Pillow before 10.1
            font = small = ImageFont.load_default()
        draw.text((pad, 14), "Hours played per day", fill=(242, 243, 245), font=font)
        peak = max(v for _, v in hours)
        slot = (w - 2 * pad) / len(hours)
        for i, (day, v) in enumerate(hours):
            x0 = pad + i * slot + slot * 0.15
            x1 = pad + (i + 1) * slot - slot * 0.15
            bar = (h - top - bottom) * (v / peak) if peak else 0
            y0 = h - bottom - bar
            draw.rectangle([x0, y0, x1, h - bottom], fill=(230, 126, 34))
            label = "0" if v < 0.05 else f"{v:.1f}" if v < 10 else f"{v:.0f}"
            draw.text(((x0 + x1) / 2, y0 - 6), label, fill=(242, 243, 245), font=small, anchor="mb")
            draw.text(((x0 + x1) / 2, h - bottom + 10), day.strftime("%a"), fill=(181, 186, 193),
                      font=small, anchor="mt")
        out = BytesIO()
        img.save(out, "PNG")
        return out.getvalue()


def heatmap(grid: list, title: str) -> Optional[bytes]:
    """A PNG heatmap of a 7 × 24 grid (community.online_grid), or None without Pillow or data."""
    if not grid or not any(v for row in grid for v in row):
        return None
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    cell, left, top, pad = 30, 60, 56, 20
    w, h = left + 24 * cell + pad, top + 7 * cell + 40
    img = Image.new("RGB", (w, h), (43, 45, 49))                  # Discord's dark background
    draw = ImageDraw.Draw(img)
    try:
        font, small = ImageFont.load_default(size=20), ImageFont.load_default(size=14)
    except TypeError:                                              # Pillow before 10.1
        font = small = ImageFont.load_default()
    draw.text((pad, 16), title, fill=(242, 243, 245), font=font)
    peak = max(v for row in grid for v in row)
    low, high = (49, 51, 56), (230, 126, 34)
    for d, row in enumerate(grid):
        draw.text((pad, top + d * cell + cell / 2), ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d],
                  fill=(181, 186, 193), font=small, anchor="lm")
        for hr, v in enumerate(row):
            f = (v / peak) ** 0.7 if v > 0 else 0
            colour = tuple(round(lo + (hi - lo) * f) for lo, hi in zip(low, high))
            x, y = left + hr * cell, top + d * cell
            draw.rectangle([x + 1, y + 1, x + cell - 2, y + cell - 2], fill=colour)
    for hr in range(0, 24, 3):
        draw.text((left + hr * cell + cell / 2, top + 7 * cell + 8), f"{hr:02d}", fill=(181, 186, 193),
                  font=small, anchor="mt")
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
BACKUP_RE = re.compile(r"_backup_")
LEGACY_EXTS = (".db", ".fwl")


def world_digest(store, save_dir: str, world: Optional[str] = None) -> Optional[str]:
    """What changed in the world since the last recap (world_objects.digest), from the save.
    Remembers this week's world for next time; None without a save or on the first run."""
    import json
    import world_objects
    found = world_objects.scan(save_dir, world)
    if found is None:
        return None
    new = world_objects.snapshot(found)
    raw = store.get_meta("world_snapshot")
    store.set_meta("world_snapshot", json.dumps(new))
    try:
        old = json.loads(raw) if raw else None
    except ValueError:
        old = None
    if not old:
        return None
    lines = world_objects.digest(old, new)
    return "\n".join(lines)[:1024] if lines else "A quiet week: nothing built, sailed, tamed or lost."


def server_birthday(conn, now: _dt.datetime, offset: int, server_name: str) -> Optional[dict]:
    """On the day the server turns 100 days or a whole number of years old (counted from the
    first visit in the stats database), an embed looking back on everything so far; None on
    other days. now is local time; log times + offset = real time."""
    import community
    import stats_db
    first = conn.execute("SELECT MIN(login_at) FROM play_sessions").fetchone()[0]
    if not first:
        return None
    born = _dt.datetime.fromtimestamp(first + offset).astimezone()
    when = community.anniversary(born.date(), now.date())
    if not when:
        return None
    s = stats_db.period_summary(conn, 0, int(now.timestamp()) - offset + 1)
    first_viking = conn.execute("SELECT player FROM play_sessions ORDER BY login_at LIMIT 1").fetchone()[0]
    fields = [{"name": "Vikings", "value": str(len(s["players"])), "inline": True},
              {"name": "Time played", "value": fmt_duration(s["total_seconds"]), "inline": True},
              {"name": "Deaths", "value": str(s["total_deaths"]), "inline": True},
              {"name": "First to set sail", "value": f"**{first_viking}**, <t:{first + offset}:D>", "inline": False}]
    if s["top_players"]:
        r = s["top_players"][0]
        fields.append({"name": "Most time in-game", "value": f"**{r['player']}**: {fmt_duration(r['seconds'])}",
                       "inline": True})
    best = community.streak_board(conn, offset, 1)
    if best and best[0]["v"] > 1:
        fields.append({"name": "Longest streak", "value": f"**{best[0]['player']}**: {best[0]['v']} days in a row",
                       "inline": True})
    title = f"🎂 {server_name} is {when} old today!" if "year" in when else f"🎂 {when} of {server_name}!"
    return {"title": title, "description": "Skål to everyone who's set sail here. Here's the saga so far:",
            "fields": fields}


def _tree_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def list_backups(folder: str, sizes: bool = True) -> list:
    """[(mtime, size, name)] newest first. name is the folder name, or the file stem
    for an old .db/.fwl pair. sizes=False skips walking each backup folder (size 0)."""
    try:
        entries = os.listdir(folder)
    except FileNotFoundError:
        return []
    out, pairs = [], {}
    for n in entries:
        if n.startswith(".") or not BACKUP_RE.search(n):
            continue
        path = os.path.join(folder, n)
        try:
            if os.path.isdir(path):
                out.append((os.stat(path).st_mtime, _tree_size(path) if sizes else 0, n))
            elif n.endswith(LEGACY_EXTS):
                st = os.stat(path)
                stem = n.rsplit(".", 1)[0]
                m, size = pairs.get(stem, (0.0, 0))
                pairs[stem] = (max(m, st.st_mtime), size + st.st_size)
        except FileNotFoundError:
            continue
    out += [(m, size, stem) for stem, (m, size) in pairs.items()]
    return sorted(out, reverse=True)


class BackupCopier:
    """Copies Valheim's own world backups from worlds_local to a second folder, e.g.
    another disk, and keeps the newest `keep` there.

    A backup is either a folder (Valheim 1.0 saves a world as chunk files, so
    worlds_local/Alheim_backup_auto-20260928-170645/ is one backup) or, on older servers,
    a <world>_backup_….db + .fwl pair. Valheim writes each backup once ("Backup created in
    …" in the log) and never changes it, so copying after that line can't catch a
    half-written one."""

    def __init__(self, cfg: dict):
        self.src = cfg["source_dir"]
        self.dest = cfg["dest_dir"]
        self.keep = int(cfg.get("keep", 30))
        self.alert_after = float(cfg.get("alert_after_hours", 48)) * 3600
        self.alerted = False
        self.lock = threading.Lock()

    def _backups(self, folder: str) -> list:
        return list_backups(folder)

    def _paths(self, folder: str, name: str) -> list:
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            return [path]
        return [path + ext for ext in LEGACY_EXTS if os.path.exists(path + ext)]

    @staticmethod
    def _remove(path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def copy_new(self) -> int:
        """Copy the newest `keep` backups that dest doesn't have yet (or has only part
        of); prune dest. Returns how many backups were copied."""
        with self.lock:
            have = {name: size for _, size, name in self._backups(self.dest)}
            copied = 0
            # Only the newest `keep`: older ones would just be pruned again.
            for _, size, name in self._backups(self.src)[:self.keep]:
                if have.get(name) == size:
                    continue
                for src in self._paths(self.src, name):
                    dst = os.path.join(self.dest, os.path.basename(src))
                    tmp = os.path.join(self.dest, f".{os.path.basename(src)}.part")
                    self._remove(tmp)
                    if os.path.isdir(src):
                        shutil.copytree(src, tmp)
                    else:
                        shutil.copy2(src, tmp)
                    self._remove(dst)
                    os.replace(tmp, dst)
                copied += 1
            self._prune()
        if copied:
            log.info("Backups: copied %d backup(s) to %s", copied, self.dest)
        return copied

    def copy_in_background(self) -> None:
        def run():
            try:
                self.copy_new()
            except Exception as e:  # noqa: BLE001
                log.warning("Backup copy failed: %s", e)
        threading.Thread(target=run, name="backup-copy", daemon=True).start()

    def _prune(self) -> None:
        for _, _, name in self._backups(self.dest)[self.keep:]:
            for path in self._paths(self.dest, name):
                self._remove(path)

    def listing(self, limit: int = 10) -> list:
        """[(mtime, size, name)] of the newest copies in dest."""
        return self._backups(self.dest)[:limit]

    def stale_alert(self, server_up: bool, now: Optional[float] = None) -> Optional[str]:
        """A one-off warning when Valheim hasn't made a backup for a long time."""
        now = time.time() if now is None else now
        newest = self._backups(self.src)
        age = now - newest[0][0] if newest else None
        if age is not None and age < self.alert_after:
            self.alerted = False
            return None
        if self.alerted or not server_up or age is None:
            return None
        self.alerted = True
        return (f"Valheim hasn't made a world backup in {fmt_duration(age)} "
                f"(newest in {self.src}). Check the server's -backups settings and free disk space.")


# ---------------------------------------------------------------------------
# Log archive
# ---------------------------------------------------------------------------
class LogArchive:
    """A permanent copy of every log line the monitor reads, one file per day:
    logs_archive/valheim_console-2026-10-01.log, gzipped once the day is over.

    Valheim starts its log from scratch on every server start, so without this a
    restart takes the history with it; --backfill reads the archive too."""

    PREFIX = "valheim_console-"

    def __init__(self, folder: str, keep_days: int = 0, clock=time.time):
        self.folder, self.keep_days, self.clock = folder, int(keep_days or 0), clock
        self.day: Optional[str] = None
        self.pending: list = []
        self.warned = False

    def add(self, line: str) -> None:
        self.pending.append(line.rstrip("\r\n"))

    def flush(self) -> None:
        """Append the lines added since the last flush to today's file."""
        if not self.pending:
            return
        lines, self.pending = self.pending, []
        try:
            day = _dt.date.fromtimestamp(self.clock()).isoformat()
            if day != self.day:
                os.makedirs(self.folder, exist_ok=True)
                self._rollover(day)
            with open(os.path.join(self.folder, f"{self.PREFIX}{day}.log"), "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            if not self.warned:
                self.warned = True
                log.warning("Log archive: couldn't write to %s: %s", self.folder, e)

    def _rollover(self, today: str) -> None:
        """Gzip finished days, and delete days older than keep_days (0 = keep all)."""
        import gzip
        self.day = today
        for name in sorted(os.listdir(self.folder)):
            if not name.startswith(self.PREFIX):
                continue
            day = name[len(self.PREFIX):len(self.PREFIX) + 10]
            path = os.path.join(self.folder, name)
            if self.keep_days and day < (_dt.date.fromisoformat(today)
                                         - _dt.timedelta(days=self.keep_days)).isoformat():
                os.remove(path)
            elif name.endswith(".log") and day < today:
                with open(path, "rb") as src, gzip.open(path + ".gz.part", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(path + ".gz.part", path + ".gz")
                os.remove(path)

    def files(self) -> list:
        """The archive's files, oldest first."""
        try:
            names = sorted(n for n in os.listdir(self.folder)
                           if n.startswith(self.PREFIX) and n.endswith((".log", ".log.gz")))
        except FileNotFoundError:
            return []
        return [os.path.join(self.folder, n) for n in names]


# ---------------------------------------------------------------------------
# Status board
# ---------------------------------------------------------------------------
def _ago(t: Optional[float]) -> str:
    # Discord renders <t:…:R> as "5 minutes ago" in each reader's own time, and keeps it
    # current by itself, so the board only needs editing when something changes.
    return f"<t:{int(t)}:R>"


def player_lines(snap: dict, limit: int = 25) -> list:
    lines = [f"• **{name}**" + (f" · joined {_ago(since)}" if since else "") for name, since in snap["online"][:limit]]
    extra = snap["count"] - len(snap["online"])
    if extra > 0:
        lines.append(f"• and {extra} more (online since before the monitor started)")
    return lines


def render_board(snap: dict, server_name: str) -> dict:
    """The status board embed (title, description, colour, fields) for a LiveState snapshot."""
    if snap["down"]:
        title, color, desc = f"🔴 {server_name}: offline", 0xED4245, "The server is down or restarting."
    elif not snap["known"]:
        title, color, desc = f"⚪ {server_name}", 0x95A5A6, "Checking who's online… (known at the next join or leave, or within 10 minutes)"
    elif snap["count"] > 0:
        title, color = f"🟢 {server_name}: {snap['count']} online", 0x57F287
        desc = "\n".join(player_lines(snap))
    else:
        title, color, desc = f"🟢 {server_name}: empty", 0x57F287, "Nobody is playing right now."
    fields = []
    if snap.get("join_code") and not snap["down"]:
        fields.append({"name": "Join code", "value": f"`{snap['join_code']}`", "inline": True})
    if snap["up_since"] and not snap["down"]:
        fields.append({"name": "Up since", "value": _ago(snap["up_since"]), "inline": True})
    if snap["version"]:
        fields.append({"name": "Version", "value": snap["version"], "inline": True})
    if snap["last_save"]:
        took = f" ({snap['last_save_ms'] / 1000:.1f} s)" if snap.get("last_save_ms") else ""
        fields.append({"name": "Last world save", "value": _ago(snap["last_save"]) + took, "inline": True})
    if snap.get("disk_free") is not None:
        fields.append({"name": "Disk free", "value": fmt_bytes(snap["disk_free"]), "inline": True})
    if snap["last_backup"]:
        fields.append({"name": "Last backup", "value": _ago(snap["last_backup"]), "inline": True})
    if snap["last_raid"]:
        name, at = snap["last_raid"]
        fields.append({"name": "Last raid", "value": f"{name} {_ago(at)}", "inline": True})
    return {"title": title, "description": desc, "color": color, "fields": fields}



# ---------------------------------------------------------------------------
# Exploration (weekly recap)
# ---------------------------------------------------------------------------
# Locations worth naming, by the start of their internal name (trailing digits dropped).
NOTABLE = (
    ("SunkenCrypt", "sunken crypt"), ("Crypt", "burial chamber"), ("TrollCave", "troll cave"),
    ("MountainCave", "frost cave"), ("GoblinCamp", "fuling village"),
    ("Mistlands_DvergrTownEntrance", "infested mine"), ("CharredFortress", "charred fortress"),
    ("Vendor_BlackForest", "Haldor the trader"), ("Hildir_camp", "Hildir's camp"),
    ("BogWitch", "the Bog Witch"),
    ("Eikthyrnir", "boss altar"), ("GDKing", "boss altar"), ("Bonemass", "boss altar"),
    ("Dragonqueen", "boss altar"), ("GoblinKing", "boss altar"),
    ("Mistlands_DvergrBossEntrance", "boss altar"), ("FaderLocation", "boss altar"),
)


def location_kind(loc: str) -> Optional[str]:
    for prefix, name in NOTABLE:
        if loc.startswith(prefix):
            return name
    return None


def exploration(details: list) -> Optional[str]:
    """'12 new areas discovered: 3 sunken crypts, 1 fuling village' from 'Loc|x,y' rows."""
    zones, notable = set(), {}
    for d in details:
        loc, _, zone = d.partition("|")
        zones.add(zone)
        kind = location_kind(loc)
        if kind:
            notable[kind] = notable.get(kind, 0) + 1
    if not zones:
        return None
    parts = [f"{n} {k}{'s' if n > 1 and not k.startswith(('the ', 'Haldor', 'Hildir')) else ''}"
             for k, n in sorted(notable.items(), key=lambda kv: -kv[1])]
    text = f"🗺️ **{len(zones)}** new area{'s' if len(zones) != 1 else ''} discovered"
    return text + (": " + ", ".join(parts[:6]) if parts else "")


# ---------------------------------------------------------------------------
# Server health
# ---------------------------------------------------------------------------
class HealthWatch:
    """Warns before the save disk fills up (Valheim stops saving below its own limit)
    and when world saves get slow. Each warning repeats at most once a day."""

    def __init__(self, cfg: dict, clock=time.time):
        self.low_disk = float(cfg.get("low_disk_gb", 10)) * 1024 ** 3
        self.slow_save_ms = float(cfg.get("slow_save_seconds", 5)) * 1000
        self.clock = clock
        self.last: dict = {}

    def _once(self, key: str, text: str) -> Optional[str]:
        now = self.clock()
        if now - self.last.get(key, -1e12) < 86400:
            return None
        self.last[key] = now
        return text

    def observe(self, ev) -> Optional[str]:
        if ev.kind == "disk_space":
            avail, warn, block = ev.extra["avail"], ev.extra["warn"], ev.extra["block"]
            if avail < max(warn, block * 2):
                return self._once("disk_critical", f"🚨 Only **{fmt_bytes(avail)}** free on the save disk. "
                                                   f"Valheim stops saving the world below {fmt_bytes(block)}. "
                                                   "Free up space now.")
            if avail < self.low_disk:
                return self._once("disk_low", f"⚠️ The save disk is getting full: **{fmt_bytes(avail)}** free "
                                              f"(warning below {fmt_bytes(self.low_disk)}).")
        elif ev.kind == "world_saved" and ev.extra.get("ms") and ev.extra["ms"] > self.slow_save_ms:
            return self._once("slow_save", f"🐢 The last world save took **{ev.extra['ms'] / 1000:.1f} s** "
                                           f"(warning above {self.slow_save_ms / 1000:.0f} s). Players may notice "
                                           "a freeze while saving; a restart or faster disk can help.")
        return None


# ---------------------------------------------------------------------------
# Daily restart
# ---------------------------------------------------------------------------
class DailyRestart:
    """Restart once a day inside a window (default 05:00-07:00, container time zone),
    only while nobody is online. Skipped for the day if people play through the window."""

    def __init__(self, cfg: dict):
        hh, mm = (int(x) for x in str(cfg.get("time", "05:00")).split(":"))
        self.start = _dt.time(hh, mm)
        self.window = _dt.timedelta(minutes=int(cfg.get("window_minutes", 120)))
        self.done_date: Optional[str] = None

    def due(self, now: _dt.datetime, empty: bool) -> bool:
        today = now.date().isoformat()
        start = now.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        if self.done_date == today or not (start <= now < start + self.window) or not empty:
            return False
        self.done_date = today
        return True
