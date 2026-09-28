#!/usr/bin/env python3
"""
SQLite store for Valheim play sessions, deaths and server concurrency.

Free, open-source, lightweight — a single .db file via Python's built-in sqlite3,
no server process. The monitor records events here; stats_site.py renders the
public page from it.

Schema
------
play_sessions   one row per continuous play session (login -> logout)
    id, player, login_at, logout_at (NULL = still open), last_seen_at,
    deaths (count during the session), source,
    duration_seconds  -- GENERATED: seconds online, capped at last_seen_at while open
deaths          one row per death
    id, player, died_at, session_id
concurrency     one row per change in the online count (for "most online at once")
    id, at, count
meta            key/value (last_event_at, schema_version, …)

All timestamps are unix epoch seconds. Log timestamps are parsed as the server's
wall clock; durations are differences so the timezone cancels out.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Optional

SCHEMA_VERSION = 1


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # concurrent reader (the site generator) is fine
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS play_sessions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            player       TEXT    NOT NULL,
            login_at     INTEGER NOT NULL,
            logout_at    INTEGER,
            last_seen_at INTEGER NOT NULL,
            deaths       INTEGER NOT NULL DEFAULT 0,
            source       TEXT,
            duration_seconds INTEGER
                GENERATED ALWAYS AS (COALESCE(logout_at, last_seen_at) - login_at) VIRTUAL
        );
        CREATE INDEX IF NOT EXISTS ix_sessions_player ON play_sessions(player);
        CREATE INDEX IF NOT EXISTS ix_sessions_open   ON play_sessions(player, logout_at);

        CREATE TABLE IF NOT EXISTS deaths (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            player     TEXT    NOT NULL,
            died_at    INTEGER NOT NULL,
            session_id INTEGER REFERENCES play_sessions(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS ix_deaths_player ON deaths(player);

        CREATE TABLE IF NOT EXISTS concurrency (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            at    INTEGER NOT NULL,
            count INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_concurrency_at ON concurrency(at);

        -- raids and other server happenings, for the weekly recap
        CREATE TABLE IF NOT EXISTS server_events (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            at     INTEGER NOT NULL,
            kind   TEXT    NOT NULL,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_server_events_at ON server_events(at);

        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- Steam achievements ---------------------------------------------------
        -- character name (from the log) -> SteamID64 (from the log handshake line)
        CREATE TABLE IF NOT EXISTS player_steam (
            player     TEXT PRIMARY KEY,
            steam_id   TEXT NOT NULL,
            updated_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS ix_player_steam_id ON player_steam(steam_id);

        -- per-SteamID profile + achievement summary (filled by the Steam Web API)
        CREATE TABLE IF NOT EXISTS steam_profile (
            steam_id         TEXT PRIMARY KEY,
            persona          TEXT,
            avatar           TEXT,
            profile_url      TEXT,
            visibility       INTEGER,   -- 3 = public; anything else = we couldn't read stats
            unlocked         INTEGER,
            total            INTEGER,
            last_unlock_at   INTEGER,
            last_unlock_name TEXT,
            updated_at       INTEGER,
            error            TEXT
        );

        -- the game's achievement catalogue (names/descriptions/icons), fetched once
        CREATE TABLE IF NOT EXISTS steam_schema (
            apiname     TEXT PRIMARY KEY,
            name        TEXT,
            description TEXT,
            icon        TEXT,
            icongray    TEXT
        );

        -- unlocked achievements per player (for "recent unlocks")
        CREATE TABLE IF NOT EXISTS steam_unlock (
            steam_id   TEXT NOT NULL,
            apiname    TEXT NOT NULL,
            unlocktime INTEGER,
            PRIMARY KEY (steam_id, apiname)
        );
        """
    )
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()


class Store:
    """Event sink: turns login/logout/death/count events into rows."""

    def __init__(self, path: str, source: str = "nexus", reconcile: bool = False):
        self.conn = connect(path)
        self.source = source
        # Only the long-running monitor may close leftover sessions: a one-off tool
        # (--refresh-steam, steam.py, --backfill) run next to it would otherwise cut
        # short the sessions of everyone the monitor is tracking as online.
        if reconcile:
            self.reconcile_open_sessions()

    # -- meta --------------------------------------------------------------
    def _set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def set_meta(self, key: str, value) -> None:
        self._set_meta(key, value)
        self.conn.commit()

    def record_clock_offset(self, log_ts: int, real_now: Optional[float] = None) -> None:
        """Remember how far the game server's log clock is from real time.

        Log lines carry the SERVER's wall clock, which we store as if it were UTC, so
        comparing those timestamps against time.time() overstates every "x ago" by the
        server's UTC offset. We see both clocks here (a line is read seconds after it is
        written), so record the difference, rounded to a quarter hour to drop polling
        jitter, and let the page subtract it.
        """
        off = int(round(((real_now or time.time()) - log_ts) / 900.0) * 900)
        if str(off) != str(self.get_meta("log_clock_offset")):
            self.set_meta("log_clock_offset", off)

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _touch(self, ts: int) -> None:
        prev = self.get_meta("last_event_at")
        if prev is None or ts > int(prev):
            self._set_meta("last_event_at", ts)

    def reconcile_open_sessions(self) -> int:
        """On startup, close any sessions left open by a previous run so they don't
        grow forever. They are closed at their last_seen_at (the newest log line we
        had recorded for them), which bounds the play time to reality."""
        n = self.conn.execute(
            "UPDATE play_sessions SET logout_at = last_seen_at "
            "WHERE logout_at IS NULL AND last_seen_at > login_at"
        ).rowcount
        # Zero-length leftovers (login with no activity) — just close them too.
        self.conn.execute("UPDATE play_sessions SET logout_at = login_at WHERE logout_at IS NULL")
        self.conn.commit()
        return n

    # -- open-session lookup ----------------------------------------------
    def _open_session(self, player: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM play_sessions WHERE player=? AND logout_at IS NULL "
            "ORDER BY login_at DESC LIMIT 1", (player,)
        ).fetchone()

    # -- event handlers ----------------------------------------------------
    def login(self, player: str, ts: int) -> None:
        # Close any dangling open session for this player first (crash/rejoin safety).
        self.conn.execute(
            "UPDATE play_sessions SET logout_at = last_seen_at "
            "WHERE player=? AND logout_at IS NULL", (player,))
        self.conn.execute(
            "INSERT INTO play_sessions(player, login_at, last_seen_at, source) VALUES (?,?,?,?)",
            (player, ts, ts, self.source))
        self._touch(ts)
        self.conn.commit()

    def logout(self, player: str, ts: int) -> None:
        row = self._open_session(player)
        if row is None:
            # Player was online before the monitor started; no login recorded — skip.
            self._touch(ts)
            self.conn.commit()
            return
        self.conn.execute(
            "UPDATE play_sessions SET logout_at=?, last_seen_at=MAX(last_seen_at, ?) WHERE id=?",
            (ts, ts, row["id"]))
        self._touch(ts)
        self.conn.commit()

    def logout_stale(self, player: str, ts: int) -> None:
        """Close an open session left behind by a server shutdown/restart. Ends it at the
        last activity we saw (last_seen_at), so the downtime isn't counted as play time."""
        row = self._open_session(player)
        if row is None:
            self._touch(ts)
            self.conn.commit()
            return
        self.conn.execute("UPDATE play_sessions SET logout_at = last_seen_at WHERE id=?", (row["id"],))
        self._touch(ts)
        self.conn.commit()

    def death(self, player: str, ts: int) -> None:
        row = self._open_session(player)
        sid = row["id"] if row else None
        self.conn.execute("INSERT INTO deaths(player, died_at, session_id) VALUES (?,?,?)", (player, ts, sid))
        if sid is not None:
            self.conn.execute("UPDATE play_sessions SET deaths = deaths + 1, last_seen_at=MAX(last_seen_at, ?) "
                             "WHERE id=?", (ts, sid))
        self._touch(ts)
        self.conn.commit()

    def heartbeat(self, ts: int) -> None:
        """Advance last_seen_at on every open session so an open session's duration
        tracks the live log even between the player's own events."""
        self.conn.execute("UPDATE play_sessions SET last_seen_at=? WHERE logout_at IS NULL AND last_seen_at < ?",
                          (ts, ts))
        self._touch(ts)
        self.conn.commit()

    def link_steam(self, player: str, steam_id: str, ts: int) -> None:
        """Record the character name -> SteamID64 mapping seen in the log handshake."""
        self.conn.execute(
            "INSERT INTO player_steam(player, steam_id, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(player) DO UPDATE SET steam_id=excluded.steam_id, updated_at=excluded.updated_at",
            (player, steam_id, ts))
        self.conn.commit()

    # -- Steam Web API writers (called by steam.py) ------------------------
    def steam_ids_to_update(self, limit: Optional[int] = None):
        """SteamIDs we know about, most-recently-active first (by their players' play)."""
        sql = ("SELECT ps.steam_id, MAX(COALESCE(s.logout_at, s.last_seen_at, 0)) AS last_seen "
               "FROM player_steam ps LEFT JOIN play_sessions s ON s.player = ps.player "
               "GROUP BY ps.steam_id ORDER BY last_seen DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["steam_id"] for r in self.conn.execute(sql).fetchall()]

    def save_schema(self, achievements: list) -> None:
        for a in achievements:
            self.conn.execute(
                "INSERT INTO steam_schema(apiname, name, description, icon, icongray) VALUES (?,?,?,?,?) "
                "ON CONFLICT(apiname) DO UPDATE SET name=excluded.name, description=excluded.description, "
                "icon=excluded.icon, icongray=excluded.icongray",
                (a.get("name"), a.get("displayName"), a.get("description"), a.get("icon"), a.get("icongray")))
        self._set_meta("steam_schema_at", int(time.time()))
        self.conn.commit()

    def save_profile(self, steam_id: str, **f) -> None:
        """Upsert a Steam profile. Only the fields PASSED IN are written, so a partial
        refresh (e.g. the summaries call failed) can't blank out a persona or avatar
        that is already stored. Pass an explicit None to clear a field."""
        known = ("persona", "avatar", "profile_url", "visibility", "unlocked", "total",
                 "last_unlock_at", "last_unlock_name", "error")
        cols = tuple(c for c in known if c in f)
        if not cols:
            return
        vals = [f[c] for c in cols]
        self.conn.execute(
            f"INSERT INTO steam_profile(steam_id, {', '.join(cols)}, updated_at) "
            f"VALUES (?{', ?' * len(cols)}, ?) "
            f"ON CONFLICT(steam_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols) + ", updated_at=excluded.updated_at",
            (steam_id, *vals, int(time.time())))
        self.conn.commit()

    def save_unlocks(self, steam_id: str, unlocks: list) -> None:
        """unlocks: list of (apiname, unlocktime). Replaces this player's unlock set."""
        self.conn.execute("DELETE FROM steam_unlock WHERE steam_id=?", (steam_id,))
        self.conn.executemany(
            "INSERT OR REPLACE INTO steam_unlock(steam_id, apiname, unlocktime) VALUES (?,?,?)",
            [(steam_id, a, t) for a, t in unlocks])
        self.conn.commit()

    def concurrency(self, count: int, ts: int) -> None:
        last = self.conn.execute("SELECT count FROM concurrency ORDER BY at DESC, id DESC LIMIT 1").fetchone()
        if last is None or last["count"] != count:
            self.conn.execute("INSERT INTO concurrency(at, count) VALUES (?,?)", (ts, count))
        self.heartbeat(ts)

    def server_event(self, kind: str, detail: str, ts: int) -> None:
        self.conn.execute("INSERT INTO server_events(at, kind, detail) VALUES (?,?,?)", (ts, kind, detail))
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


# ---------------------------------------------------------------------------
# Read-side: everything the stats page needs, as plain dicts.
# ---------------------------------------------------------------------------
def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else {}


def player_leaderboard(conn, limit: int = 10):
    return _rows(conn, """
        SELECT s.player,
               SUM(s.duration_seconds)                     AS total_seconds,
               COUNT(*)                                     AS sessions,
               MAX(s.duration_seconds)                      AS longest_seconds,
               MIN(s.login_at)                              AS first_seen,
               MAX(COALESCE(s.logout_at, s.last_seen_at))   AS last_seen,
               COALESCE(d.deaths, 0)                        AS deaths
        FROM play_sessions s
        LEFT JOIN (SELECT player, COUNT(*) AS deaths FROM deaths GROUP BY player) d
               ON d.player = s.player
        GROUP BY s.player
        ORDER BY total_seconds DESC
        LIMIT ?""", (limit,))


def top_deaths(conn, limit: int = 10):
    return _rows(conn, """
        SELECT player, COUNT(*) AS deaths
        FROM deaths GROUP BY player ORDER BY deaths DESC LIMIT ?""", (limit,))


def top_sessions(conn, limit: int = 10):
    return _rows(conn, """
        SELECT player, COUNT(*) AS sessions
        FROM play_sessions GROUP BY player ORDER BY sessions DESC LIMIT ?""", (limit,))


def longest_single_sessions(conn, limit: int = 10):
    return _rows(conn, """
        SELECT player, duration_seconds, login_at
        FROM play_sessions WHERE duration_seconds IS NOT NULL
        ORDER BY duration_seconds DESC LIMIT ?""", (limit,))


def server_stats(conn):
    s = _one(conn, """
        SELECT COUNT(*)                          AS total_sessions,
               COUNT(DISTINCT player)            AS unique_players,
               COALESCE(SUM(duration_seconds),0) AS total_seconds,
               MIN(login_at)                     AS first_login
        FROM play_sessions""")
    s["total_deaths"] = _one(conn, "SELECT COUNT(*) AS c FROM deaths").get("c", 0)
    peak = _one(conn, "SELECT MAX(count) AS peak FROM concurrency")
    s["peak_online"] = peak.get("peak") or 0
    peak_at = _one(conn, "SELECT at FROM concurrency WHERE count=? ORDER BY at ASC LIMIT 1",
                   (s["peak_online"],))
    s["peak_online_at"] = peak_at.get("at")
    now_row = _one(conn, "SELECT count FROM concurrency ORDER BY at DESC, id DESC LIMIT 1")
    s["currently_online"] = now_row.get("count", 0)
    return s


def currently_online(conn):
    return _rows(conn, """
        SELECT player, login_at, last_seen_at
        FROM play_sessions WHERE logout_at IS NULL ORDER BY login_at ASC""")


def recent_activity(conn, limit: int = 15):
    return _rows(conn, """
        SELECT player, login_at AS at, 'login' AS kind FROM play_sessions
        UNION ALL SELECT player, logout_at AS at, 'logout' FROM play_sessions WHERE logout_at IS NOT NULL
        UNION ALL SELECT player, died_at AS at, 'death' FROM deaths
        ORDER BY at DESC LIMIT ?""", (limit,))


def achievements_leaderboard(conn, limit: int = 10):
    """One row per Steam-linked player, most achievements first. `names` groups every
    character name seen for that SteamID (players often reuse one account)."""
    rows = _rows(conn, """
        SELECT p.steam_id,
               p.persona, p.avatar, p.profile_url, p.visibility,
               p.unlocked, p.total, p.last_unlock_at, p.last_unlock_name, p.updated_at, p.error,
               (SELECT GROUP_CONCAT(DISTINCT player) FROM player_steam WHERE steam_id = p.steam_id) AS names
        FROM steam_profile p
        ORDER BY p.unlocked DESC, p.total DESC, p.updated_at DESC
        LIMIT ?""", (limit,))
    return rows


def recent_unlocks(conn, limit: int = 12):
    return _rows(conn, """
        SELECT u.steam_id, u.apiname, u.unlocktime,
               COALESCE(sc.name, u.apiname) AS name, sc.icon,
               (SELECT persona FROM steam_profile WHERE steam_id = u.steam_id) AS persona
        FROM steam_unlock u
        LEFT JOIN steam_schema sc ON sc.apiname = u.apiname
        WHERE u.unlocktime > 0
        ORDER BY u.unlocktime DESC LIMIT ?""", (limit,))


def player_totals(conn, player: str) -> dict:
    """All-time play time (s), sessions and deaths for one character."""
    t = _one(conn, "SELECT COALESCE(SUM(duration_seconds),0) AS seconds, COUNT(*) AS sessions "
                   "FROM play_sessions WHERE player=?", (player,))
    t["deaths"] = _one(conn, "SELECT COUNT(*) AS c FROM deaths WHERE player=?", (player,)).get("c", 0)
    return t


def period_summary(conn, since: int, until: int, limit: int = 5) -> dict:
    """Activity between two log-clock timestamps: play time clipped to the window."""
    clip = ("MAX(0, MIN(COALESCE(logout_at, last_seen_at), :until) - MAX(login_at, :since))")
    where = "COALESCE(logout_at, last_seen_at) > :since AND login_at < :until"
    p = {"since": since, "until": until, "limit": limit}
    players = _rows(conn, f"SELECT player, SUM({clip}) AS seconds, COUNT(*) AS sessions "
                          f"FROM play_sessions WHERE {where} GROUP BY player "
                          f"HAVING seconds > 0 ORDER BY seconds DESC", p)
    deaths = _rows(conn, "SELECT player, COUNT(*) AS deaths FROM deaths WHERE died_at >= :since "
                         "AND died_at < :until GROUP BY player ORDER BY deaths DESC LIMIT :limit", p)
    new = _rows(conn, "SELECT player FROM play_sessions GROUP BY player "
                      "HAVING MIN(login_at) >= :since AND MIN(login_at) < :until", p)
    raids = _rows(conn, "SELECT detail, COUNT(*) AS n FROM server_events WHERE kind='raid' AND at >= :since "
                        "AND at < :until GROUP BY detail ORDER BY n DESC", p)
    peak = _one(conn, "SELECT MAX(count) AS peak FROM concurrency WHERE at >= :since AND at < :until", p)
    return {"players": players, "top_players": players[:limit], "deaths": deaths,
            "total_seconds": sum(r["seconds"] or 0 for r in players),
            "total_deaths": sum(r["deaths"] for r in _rows(conn, "SELECT COUNT(*) AS deaths FROM deaths "
                                                                "WHERE died_at >= :since AND died_at < :until", p)),
            "new_players": [r["player"] for r in new], "raids": raids, "peak": peak.get("peak") or 0}
