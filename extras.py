"""
Extras built on the parsed log: live server state (for the status board and
/valheim online), session summaries, milestones, the weekly recap, and copying
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
    last_backup: Optional[float] = None
    portals: Optional[int] = None
    last_raid: Optional[tuple] = None                  # (name, real epoch)
    booted: bool = False                               # saw a boot, so the count starts at 0
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
            elif k == "server_restart":
                self.down, self.up_since = True, None
                self.online.clear()
                self.session_start.clear()
                self.session_deaths.clear()
                self.count = 0
            elif k == "server_online":
                self.down, self.up_since, self.count, self.booted = False, now, 0, True
                self.online.clear()
            elif k == "server_version":
                self.version = ev.extra.get("version")
            elif k == "world_saved":
                self.last_save = now
            elif k == "backup_saved":
                self.last_backup = now
            elif k == "portals":
                self.portals = ev.extra.get("portals")
            elif k == "raid":
                self.last_raid = (ev.extra.get("raid"), now)
        return None

    def snapshot(self) -> dict:
        with self.lock:
            names = sorted(self.online.items(), key=lambda kv: (kv[1] is None, kv[1] or 0))
            count = self.count if self.count is not None else len(names)
            return {"online": names, "count": max(count, len(names)), "down": self.down,
                    "up_since": self.up_since, "version": self.version, "last_save": self.last_save,
                    "last_backup": self.last_backup, "portals": self.portals, "last_raid": self.last_raid,
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
        summary = (f"**{len(s['players'])}** vikings played **{fmt_duration(s['total_seconds'])}** in total, "
                   f"died **{s['total_deaths']}** times"
                   + (f", and peaked at **{s['peak']}** online at once." if s["peak"] else "."))
        return {"title": f"📜 This week in {server_name}", "description": summary, "fields": fields}


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
BACKUP_RE = re.compile(r"_backup_")
LEGACY_EXTS = (".db", ".fwl")


def _tree_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


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
        """[(mtime, size, name)] newest first. name is the folder name, or the file stem
        for an old .db/.fwl pair."""
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
                    out.append((os.stat(path).st_mtime, _tree_size(path), n))
                elif n.endswith(LEGACY_EXTS):
                    st = os.stat(path)
                    stem = n.rsplit(".", 1)[0]
                    m, size = pairs.get(stem, (0.0, 0))
                    pairs[stem] = (max(m, st.st_mtime), size + st.st_size)
            except FileNotFoundError:
                continue
        out += [(m, size, stem) for stem, (m, size) in pairs.items()]
        return sorted(out, reverse=True)

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
    if snap["up_since"] and not snap["down"]:
        fields.append({"name": "Up since", "value": _ago(snap["up_since"]), "inline": True})
    if snap["version"]:
        fields.append({"name": "Version", "value": snap["version"], "inline": True})
    if snap["portals"] is not None:
        fields.append({"name": "Portals", "value": str(snap["portals"]), "inline": True})
    if snap["last_save"]:
        fields.append({"name": "Last world save", "value": _ago(snap["last_save"]), "inline": True})
    if snap["last_backup"]:
        fields.append({"name": "Last backup", "value": _ago(snap["last_backup"]), "inline": True})
    if snap["last_raid"]:
        name, at = snap["last_raid"]
        fields.append({"name": "Last raid", "value": f"{name} {_ago(at)}", "inline": True})
    return {"title": title, "description": desc, "color": color, "fields": fields}
