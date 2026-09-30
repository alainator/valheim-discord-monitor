#!/usr/bin/env python3
"""
Valheim -> Discord event monitor (no mods required).

Two modes:

  * Log mode  — tails the vanilla Valheim dedicated-server console log (locally,
    over FTP/SFTP, via the LOW.MS panel API, or any HTTP endpoint that returns
    the raw log text) and posts named login / logout / death events.
  * Count mode (source type "a2s" or "steamapi") — polls the server's Steam
    query port (game port + 1), or Steam's master server via the Web API when
    that port is firewalled, and posts when the player count changes or the
    server goes down / comes back. Needs no file or panel access; no names or
    deaths.

Only the Python standard library is required for file / ftp / http / nexus
sources. SFTP needs `pip install paramiko`.

Usage:
    python valheim_discord_monitor.py --config config.json
    python valheim_discord_monitor.py --config config.json --discover     # list candidate log files on the FTP server
    python valheim_discord_monitor.py --config config.json --replay sample.log   # dry-run the parser on a file
    python valheim_discord_monitor.py --config config.json --test-webhook # send a test message to Discord
    python a2s_probe.py YOUR.SERVER.IP                                    # check the Steam query port answers
"""

from __future__ import annotations

import argparse
import calendar
import fnmatch
import io
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ftplib import FTP, FTP_TLS, error_perm
from typing import Iterator, Optional

log = logging.getLogger("valheim-monitor")

# ---------------------------------------------------------------------------
# Log line patterns (vanilla dedicated server, Steam and PlayFab/crossplay)
# ---------------------------------------------------------------------------
# Optional "MM/DD/YYYY HH:MM:SS: " prefix that the server prints on most lines.
_TS = r"^\s*(?:\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}:\s*)?"
RE_TS = re.compile(r"^\s*(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2}):")


def parse_log_ts(line: str) -> Optional[int]:
    """Unix epoch (UTC-normalised) from a 'MM/DD/YYYY HH:MM:SS:' log prefix, else None.
    Times are parsed consistently, so session durations are correct regardless of the
    server's actual timezone."""
    m = RE_TS.match(line)
    if not m:
        return None
    mo, d, y, hh, mm, ss = (int(x) for x in m.groups())
    try:
        return int(calendar.timegm((y, mo, d, hh, mm, ss, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None

# Character spawn / despawn. `owner` is the peer's ZDO owner id for this session.
RE_ZDOID = re.compile(_TS + r"Got character ZDOID from (?P<name>.+?) : (?P<owner>-?\d+):(?P<n>\d+)\s*$")
# Logout on PlayFab-relayed servers: the peer's non-persistent ZDOs get destroyed.
RE_ABANDONED = re.compile(_TS + r"Destroying abandoned non persistent zdo \S+ owner (?P<owner>-?\d+)")
# Logout on direct-Steam servers.
RE_CLOSE = re.compile(_TS + r"Closing socket (?P<id>\S+)")
RE_CONNECT_STEAM = re.compile(_TS + r"Got connection SteamID (?P<id>\S+)")
RE_CONNECT_PLAYFAB = re.compile(_TS + r"PlayFab listen socket child connected to remote player (?P<id>\S+)")
RE_PLATFORM_ID = re.compile(_TS + r"PlayFab socket with remote ID playfab/(?P<pf>\S+) received local Platform ID (?P<platform>\S+)")
# Any line that carries the server's authoritative player count.
RE_COUNT = re.compile(_TS + r"Player (?:joined|disconnected from|connection lost(?: server)?).*?(?:now|currently) (?P<count>\d+) player")
RE_CONNECTIONS = re.compile(_TS + r"Connections (?P<count>\d+) ZDOS")
RE_SERVERNAME = re.compile(r"server \"(?P<server>[^\"]*)\"")
# Crossplay join code: on "Player joined/disconnected … that has join code 034505" lines,
# and on the "Session "…" with join code … and IP a.b.c.d:2456" line where the server
# prints it. The code changes every time the server restarts.
RE_JOINCODE = re.compile(r"join code (?P<code>\d+)(?: and IP (?P<ip>\d{1,3}(?:\.\d{1,3}){3}:\d+))?")
RE_READY = re.compile(_TS + r"Game server connected")
# Any line that sounds like a connection ending. Used only together with a connection id we
# already paired with a character, so an unrelated line can't log anyone out.
RE_GONE = re.compile(r"disconnect|lost|clos|dispos|timed? ?out|timeout|kick", re.IGNORECASE)
RE_TIMEOUT = re.compile(_TS + r"ZRpc timeout detected")
# Server shutting down (scheduled restart, backup, update, crash). A graceful stop
# prints these but NOT per-player "Destroying" lines or "now 0 player(s)", so anyone
# online would otherwise stay stuck as online — we flush them on any of these.
RE_SHUTDOWN = re.compile(_TS + r"(?:Game - )?OnApplicationQuit|ZNet Shutdown|ZNet OnDestroy")
# A player turned away by bannedlist.txt / permittedlist.txt. The id is whatever the
# server compared against the lists: "V_7656…" (Steam) / "X_…" / "S_…" / "N_…" since 1.0,
# "Steam_7656…" / "Xbox_…" before that, a bare SteamID64 on old Steam-only builds — so it
# is exactly what belongs in the list files.
# Server-side happenings with no player name attached.
RE_RAID = re.compile(_TS + r"Random event set:\s*(?P<event>\S+)")
RE_NETVER = re.compile(_TS + r"Network version check, their:(?P<their>\d+), mine:(?P<mine>\d+)")
RE_SAVED = re.compile(_TS + r"World save \(\d+/\d+\) done(?:\. Total time \[(?P<ms>\d+)ms\])?")
RE_DISK = re.compile(_TS + r"Available space to current user: (?P<avail>\d+)\. Saving is blocked if below: "
                     r"(?P<block>\d+) bytes\. Warnings are given if below: (?P<warn>\d+)")
# A location being generated in a zone, which happens the first time anyone goes there.
RE_LOCATION = re.compile(_TS + r"Placed location (?P<loc>\S+) in zone (?P<zone>-?\d+,-?\d+)")
RE_BACKUP = re.compile(_TS + r"Backup created in (?P<name>\S+)")
RE_VERSION = re.compile(_TS + r"Valheim version: ?(?P<version>\S+)")
# Printed when everyone sleeps through the night: "Time 25920.5, day:15    nextm:27000 …".
RE_DAY = re.compile(_TS + r"Time [\d.]+, day:\s*(?P<day>\d+)")
RE_REFUSED = re.compile(_TS + r"Player (?P<name>.+?) : (?P<id>\S+) is blacklisted or not in whitelist")


# Random events ("raids") by their internal name. Unknown ones fall back to a tidied name.
RAIDS = {
    "army_eikthyr": "Eikthyr's army (boars and necks)",
    "army_theelder": "The Elder's army (greydwarves)",
    "army_bonemass": "Bonemass's army (draugr and skeletons)",
    "army_moder": "Moder's army (drakes)",
    "army_goblin": "Yagluth's army (fulings)",
    "army_seekers": "The Queen's army (seekers)",
    "army_gjall": "Gjall",
    "army_charred": "Fader's army (the Charred)",
    "foresttrolls": "Trolls",
    "skeletons": "Skeletons",
    "blobs": "Blobs",
    "wolves": "Wolves",
    "surtlings": "Surtlings",
    "bats": "Bats",
}


def raid_name(event: str) -> str:
    return RAIDS.get(event) or event.replace("army_", "").replace("_", " ").strip().capitalize()


@dataclass
class Event:
    kind: str                   # login | logout | death | respawn | count | join_refused | raid | ...
    player: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ParserState:
    online: dict = field(default_factory=dict)       # character name -> owner id
    owner_to_name: dict = field(default_factory=dict)
    dead: set = field(default_factory=set)
    pending_ids: list = field(default_factory=list)  # Steam connection ids not yet paired with a name
    id_to_name: dict = field(default_factory=dict)   # Steam connection id -> name
    id_to_steam: dict = field(default_factory=dict)  # connection id -> SteamID64 (crossplay handshake)
    id_to_platform: dict = field(default_factory=dict)  # connection id -> "Steam_…"/"Nintendo_…"/"Xbox_…"/…
    last_platform: Optional[str] = None              # the latest handshake's platform id (version checks)
    server_count: Optional[int] = None               # authoritative count from the server's own log lines
    down: bool = False                               # True after a shutdown, until the next boot
    join_code: Optional[str] = None                  # crossplay join code of this server session


class ValheimLogParser:
    """
    Emits named login / logout / death / respawn events from the vanilla console log.

    The player count shown in each message is the server's OWN count (parsed from the
    "now N player(s)" and "Connections N ZDOS" lines), not a tally of the events we've
    seen — so it stays correct even for players who were already online when the monitor
    started, and through crossplay reconnect churn.
    """

    def __init__(self):
        self.s = ParserState()
        self.last_ts: Optional[int] = None      # epoch of the most recent timestamped log line
        # Diagnostics for a leave we couldn't attribute (the server's count dropped below
        # the players we track): the lines around it go to the monitor's own log.
        self.recent: deque = deque(maxlen=12)
        self.watch: Optional[dict] = None
        self.last_diag = 0.0

    # Who's online and which owner/connection id is whose, saved between monitor restarts.
    # Without it, a player who leaves after a restart can't be recognised (their "abandoned
    # zdo" line only carries the owner id learned when they spawned) and stays "online".
    def state_dict(self) -> dict:
        s = self.s
        return {"online": s.online, "owner_to_name": s.owner_to_name, "dead": sorted(s.dead),
                "pending_ids": s.pending_ids, "id_to_name": s.id_to_name, "id_to_steam": s.id_to_steam,
                "id_to_platform": s.id_to_platform,
                "server_count": s.server_count, "down": s.down, "join_code": s.join_code}

    def load_state(self, d: dict) -> None:
        try:
            self.s = ParserState(online=dict(d.get("online") or {}),
                                 owner_to_name=dict(d.get("owner_to_name") or {}),
                                 dead=set(d.get("dead") or ()), pending_ids=list(d.get("pending_ids") or ()),
                                 id_to_name=dict(d.get("id_to_name") or {}),
                                 id_to_steam=dict(d.get("id_to_steam") or {}),
                                 id_to_platform=dict(d.get("id_to_platform") or {}),
                                 server_count=d.get("server_count"), down=bool(d.get("down")),
                                 join_code=d.get("join_code"))
        except (TypeError, ValueError) as e:
            log.warning("Ignoring the saved parser state: %s", e)
            self.s = ParserState()

    def _count(self) -> dict:
        return {"count": self.s.server_count} if self.s.server_count is not None else {}

    def feed(self, line: str) -> Iterator["Event"]:
        """Parse one line, stamping each emitted event with the log timestamp (epoch)."""
        ts = parse_log_ts(line)
        if ts is not None:
            self.last_ts = ts
        for ev in self._feed(line):
            # Log times are the server's wall clock; don't mix in a real-UTC time.time()
            # before the first timestamped line (record_event skips events without "ts").
            if self.last_ts is not None:
                ev.extra.setdefault("ts", self.last_ts)
            yield ev
        self._diagnose(line.rstrip("\r\n"))

    def _diagnose(self, line: str) -> None:
        """If the server says fewer players are on than we track, and 15 lines later that's
        still so, someone left without a line we recognise. Log the lines around it once
        (at most every 10 minutes), so the pattern can be added to the parser."""
        if self.watch is not None:
            self.watch["after"].append(line)
            if len(self.watch["after"]) >= 15:
                count, tracked = self.s.server_count, len(self.s.online)
                if count is not None and 0 < count < tracked and time.time() - self.last_diag > 600:
                    self.last_diag = time.time()
                    log.warning("Someone left but the log didn't say who: the server counts %d player(s), the "
                                "monitor still tracks %s. Their 'In Valheim' role and session stay until the "
                                "server is empty. Please report these log lines:\n%s", count,
                                ", ".join(sorted(self.s.online)),
                                "\n".join(self.watch["before"] + self.watch["after"]))
                self.watch = None
        elif line and self.s.server_count is not None and 0 < self.s.server_count < len(self.s.online) \
                and RE_COUNT.search(line):
            self.watch = {"before": list(self.recent), "after": []}
        if line:
            self.recent.append(line)

    def _logout(self, name: str) -> Event:
        owner = self.s.online.pop(name, None)
        self.s.owner_to_name.pop(owner, None)
        self.s.dead.discard(name)
        # The authoritative "connection lost ... now N" line follows this one, so our
        # server_count is still the pre-leave value here; reflect the leave now and let
        # the next count line reconcile.
        if self.s.server_count is not None:
            self.s.server_count = max(0, self.s.server_count - 1)
        return Event("logout", name, self._count())

    def _flush(self) -> Iterator["Event"]:
        """Log everyone out — used when the server shuts down or a new session starts,
        where the game never prints per-player disconnects. Sessions are closed at their
        last seen activity (stale=True), so downtime isn't counted as play time."""
        for name in list(self.s.online):
            owner = self.s.online.pop(name, None)
            self.s.owner_to_name.pop(owner, None)
            self.s.dead.discard(name)
            yield Event("logout", name, {"count": len(self.s.online), "stale": True})
        self.s.server_count = 0

    def _drop_pending(self, host_id: str) -> None:
        """A refused peer never spawns a character, so forget its connection id; otherwise
        the next real login would be paired with it (and linked to the wrong SteamID)."""
        bare = host_id.split("_", 1)[1] if "_" in host_id else host_id
        for cid in list(self.s.pending_ids):
            if cid in (host_id, bare) or self.s.id_to_steam.get(cid) == bare:
                self.s.pending_ids.remove(cid)
                self.s.id_to_steam.pop(cid, None)
                return

    def _feed(self, line: str) -> Iterator[Event]:
        line = line.rstrip("\r\n")
        if not line:
            return

        # Server shutting down: the game prints no per-player disconnects here, so
        # log everyone out now (scheduled restart / backup / update / crash) and post
        # one "restarting" line instead of a logout per player.
        if RE_SHUTDOWN.search(line):
            yield from self._flush()
            if not self.s.down:
                self.s.down = True
                yield Event("server_restart", None, {})
            self.s.server_count = 0
            return

        # The join code rides on count lines, so note it without consuming the line.
        m = RE_JOINCODE.search(line)
        if m and m.group("code") != self.s.join_code:
            self.s.join_code = m.group("code")
            yield Event("join_code", None, {"code": self.s.join_code, "ip": m.group("ip")})

        # Keep the authoritative count up to date from any line that carries it.
        m = RE_COUNT.search(line) or RE_CONNECTIONS.search(line)
        if m:
            self.s.server_count = int(m.group("count"))
            yield Event("count", None, {"count": self.s.server_count})
            # Steady-state truth: if the server says nobody is on, log out anyone we
            # still think is online (a stuck player whose disconnect we never saw).
            if self.s.server_count == 0 and self.s.online:
                yield from self._flush()
            return

        m = RE_RAID.search(line)
        if m:
            yield Event("raid", None, {"event": m.group("event"), "raid": raid_name(m.group("event"))})
            return
        m = RE_NETVER.search(line)
        if m:
            their, mine = int(m.group("their")), int(m.group("mine"))
            if their != mine:
                # The handshake just before names the player's platform id, if crossplay.
                yield Event("version_mismatch", None, {"their": their, "mine": mine, "newer": their > mine,
                                                       "platform_id": self.s.last_platform})
            self.s.last_platform = None
            return
        m = RE_SAVED.search(line)
        if m:
            yield Event("world_saved", None, {"ms": int(m.group("ms")) if m.group("ms") else None})
            return
        m = RE_DISK.search(line)
        if m:
            yield Event("disk_space", None, {k: int(m.group(k)) for k in ("avail", "block", "warn")})
            return
        m = RE_LOCATION.search(line)
        if m:
            yield Event("location", None, {"loc": m.group("loc"), "zone": m.group("zone")})
            return
        m = RE_BACKUP.search(line)
        if m:
            yield Event("backup_saved", None, {"name": m.group("name")})
            return
        m = RE_VERSION.search(line)
        if m:
            yield Event("server_version", None, {"version": m.group("version")})
            return
        m = RE_DAY.search(line)
        if m:
            yield Event("world_day", None, {"day": int(m.group("day"))})
            return

        m = RE_REFUSED.search(line)
        if m:
            name, host_id = m.group("name").strip(), m.group("id")
            self._drop_pending(host_id)
            yield Event("join_refused", name, {"host_id": host_id})
            return

        m = RE_ZDOID.search(line)
        if m:
            name, owner, n = m.group("name").strip(), m.group("owner"), m.group("n")
            if owner == "0" and n == "0":
                # A 0:0 ZDOID is a death — fire it even for players who were already
                # online when the monitor started (we never saw their login).
                if name not in self.s.dead:
                    self.s.dead.add(name)
                    yield Event("death", name)
                return
            if name in self.s.dead:
                self.s.dead.discard(name)
                # Register the owner id so a later logout can be matched, even for a
                # player who was already online when the monitor started.
                self.s.online[name] = owner
                self.s.owner_to_name[owner] = name
                yield Event("respawn", name)
                return
            if name in self.s.online:
                # Character re-spawn for an already-known player (portal, etc.); refresh
                # the owner id in case it changed this session.
                self.s.online[name] = owner
                self.s.owner_to_name[owner] = name
                return
            self.s.online[name] = owner
            self.s.owner_to_name[owner] = name
            steam_id = platform_id = None
            if self.s.pending_ids:
                cid = self.s.pending_ids.pop(0)
                self.s.id_to_name[cid] = name
                steam_id = self.s.id_to_steam.get(cid) or (cid if cid.startswith("7656") and cid.isdigit() else None)
                platform_id = self.s.id_to_platform.get(cid) or (f"Steam_{steam_id}" if steam_id else None)
            extra = self._count()
            if steam_id:
                extra["steam_id"] = steam_id
            if platform_id:
                extra["platform_id"] = platform_id
            yield Event("login", name, extra)
            return

        m = RE_ABANDONED.search(line)
        if m:
            name = self.s.owner_to_name.get(m.group("owner"))
            if name:
                yield self._logout(name)
            return

        # Crossplay handshake: maps a PlayFab connection id to the player's SteamID64.
        m = RE_PLATFORM_ID.search(line)
        if m:
            platform = m.group("platform")
            self.s.id_to_platform[m.group("pf")] = platform
            self.s.last_platform = platform
            for prefix in ("V_", "Steam_"):      # Steam: "V_" since Valheim 1.0, "Steam_" before
                if platform.startswith(prefix):
                    self.s.id_to_steam[m.group("pf")] = platform[len(prefix):]
                    break
            return

        m = RE_CONNECT_STEAM.search(line) or RE_CONNECT_PLAYFAB.search(line)
        if m:
            cid = m.group("id")
            if cid not in self.s.pending_ids:
                self.s.pending_ids.append(cid)
            return

        m = RE_CLOSE.search(line)
        if m:
            cid = m.group("id")
            if cid in self.s.pending_ids:
                self.s.pending_ids.remove(cid)
                return
            name = self.s.id_to_name.pop(cid, None)
            if name and name in self.s.online:
                yield self._logout(name)
            return

        # A connection we paired with a character is ending ("… socket <id> closed",
        # "… <id> disconnected", "… <id> timed out"): that character has left.
        if self.s.id_to_name and RE_GONE.search(line):
            for cid, name in list(self.s.id_to_name.items()):
                if len(cid) >= 8 and cid in line and name in self.s.online:
                    self.s.id_to_name.pop(cid, None)
                    yield self._logout(name)
                    return

        if RE_READY.search(line):
            # A new server session is starting.
            had_players = bool(self.s.online)
            was_down = self.s.down
            yield from self._flush()          # close anyone still tracked (stale, not posted)
            self.s = ParserState()
            # If players were still online and we never saw the shutdown, note the restart
            # now; then always announce the server is back up.
            if had_players and not was_down:
                yield Event("server_restart", None, {})
            yield Event("server_online", None, {})
            return


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------
class _SafeDict(dict):
    """format_map helper: a missing {placeholder} renders empty instead of raising,
    so a message template can reference {count} etc. even for events that lack it."""
    def __missing__(self, key):
        return ""


def _escape_md(text: Optional[str]) -> Optional[str]:
    """Escape Discord markdown so a name like "*Bob*" can't break the message formatting."""
    return re.sub(r"([\\*_~`|>])", r"\\\1", text) if text else text


class Discord:
    COLORS = {"login": 0x57F287, "logout": 0x95A5A6, "death": 0xED4245, "respawn": 0xFEE75C, "server_up": 0x5865F2,
              "player_joined": 0x57F287, "player_left": 0x95A5A6, "server_online": 0x57F287, "server_offline": 0xED4245,
              "server_restart": 0xE0A13C, "maintenance_start": 0x5865F2, "maintenance_done": 0x57F287,
              "maintenance_failed": 0xED4245, "maintenance_pending": 0xE0A13C, "join_refused": 0xE67E22,
              "raid": 0xED4245, "version_mismatch": 0xE0A13C, "logout_summary": 0x95A5A6, "welcome": 0x57F287,
              "milestone": 0xF1C40F, "weekly_recap": 0x5865F2, "update": 0x5865F2,
              "titles": 0xF1C40F, "achievement": 0xE67E22}
    EMOJI = {"login": "🟢", "logout": "🔴", "death": "💀", "respawn": "🔥", "server_up": "🛡️",
             "player_joined": "🟢", "player_left": "🔴", "server_online": "🟢", "server_offline": "🔴",
             "server_restart": "🔻", "maintenance_start": "🛠️", "maintenance_done": "✅",
             "maintenance_failed": "⚠️", "maintenance_pending": "🕑", "join_refused": "🚫",
             "raid": "⚔️", "version_mismatch": "⚠️", "logout_summary": "🔴", "welcome": "🎉", "milestone": "🏆"}
    DEFAULT_MESSAGES = {
        "login": "**{player}** has arrived in {server}.",
        "logout": "**{player}** has left {server}.",
        "death": "**{player}** has died. Odin is watching.",
        "respawn": "**{player}** has respawned.",
        "server_up": "{server} is online.",
        "server_restart": "**{server}** is restarting — all players have been disconnected.",
        "server_online": "**{server}** is back online!",
        "server_offline": "**{server}** is offline — it went down and hasn't come back.",
        # Extras (need the matching kind in `events`).
        "raid": "**Raid in {server}!** {raid} is attacking.",
        "version_mismatch": "{detail}",
        "logout_summary": "**{player}** left {server} after {duration}{deaths_text}.",
        "welcome": "**{player}** arrived in {server} for the first time. Welcome, viking!",
        "milestone": "**{player}** {detail}",
        "update": "{detail}",
        # Someone on the ban list, or not on the permitted list, tried to connect.
        "join_refused": "**{player}** tried to join {server} but isn't allowed in.",
        # Count-only events (a2s source): no names available.
        "player_joined": "{who} arrived in {server}. **{count}/{max}** online.",
        "player_left": "{who} left {server}. **{count}/{max}** online.",
        # Unattended maintenance (maintenance.py) — only runs while nobody is online.
        "maintenance_pending": "On **{server}**, {detail}.",
        "maintenance_start": "**{server}** is down for maintenance: {detail}.",
        "maintenance_done": "**{server}** maintenance finished: {detail}.",
        "maintenance_failed": "**{server}** maintenance had a problem: {detail}",
    }

    def __init__(self, webhook_url: str, username: str = "Valheim", show_count: bool = True,
                 use_embeds: bool = True, messages: Optional[dict] = None):
        self.url = webhook_url
        self.username = username
        self.show_count = show_count
        self.use_embeds = use_embeds
        self.messages = {**self.DEFAULT_MESSAGES, **(messages or {})}

    def post(self, ev: Event, server_name: str, event_filter: set) -> None:
        if ev.kind not in event_filter or ev.kind not in self.messages:
            return
        # Per-player logouts from a shutdown flush are summarised by one server_restart
        # line, so don't post them individually.
        if ev.kind == "logout" and ev.extra.get("stale"):
            return
        fields = _SafeDict({**ev.extra, "player": _escape_md(ev.player),
                            "server": server_name or ev.extra.get("server", "the server")})
        text = self.messages[ev.kind].format_map(fields)
        emoji = self.EMOJI.get(ev.kind, "")
        footer = f"{ev.extra['count']} player(s) online" if self.show_count and "count" in ev.extra else None
        if self.use_embeds:
            embed = {"description": f"{emoji} {text}".strip(), "color": self.COLORS.get(ev.kind, 0),
                     "timestamp": datetime.now(timezone.utc).isoformat()}
            if footer:
                embed["footer"] = {"text": footer}
            payload = {"username": self.username, "embeds": [embed]}
        else:
            payload = {"username": self.username,
                       "content": f"{emoji} {text}".strip() + (f"  ({footer})" if footer else "")}
        uid = ev.extra.get("mention")
        if uid and str(uid).isdigit():
            # A linked player's Discord account: ping them, and only them. (Mentions inside
            # an embed never notify, so the ping goes in the message text.)
            payload["content"] = (payload.get("content", "") + f" <@{uid}>").strip()
            payload["allowed_mentions"] = {"parse": [], "users": [str(uid)]}
        self.send(payload)

    def post_embed(self, kind: str, embed: dict, event_filter: set) -> None:
        """Post a ready-made embed (the weekly recap)."""
        if kind not in event_filter:
            return
        embed = {"color": self.COLORS.get(kind, 0), "timestamp": datetime.now(timezone.utc).isoformat(), **embed}
        self.send({"username": self.username, "embeds": [embed]})

    def send(self, payload: dict) -> None:
        if not self.url:
            return                                   # no webhook yet (the admin bot can create one)
        # Character names are chosen by players: never let one ping @everyone, a role or a user.
        payload.setdefault("allowed_mentions", {"parse": []})
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                  "User-Agent": "valheim-discord-monitor/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                return
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = float(e.headers.get("Retry-After", "2"))
                    log.warning("Discord rate limited; sleeping %.1fs", retry)
                    time.sleep(retry)
                    continue
                log.error("Discord HTTP %s: %s", e.code, e.read()[:200])
                return
            except Exception as e:
                log.warning("Discord post failed (%s), retrying", e)
                time.sleep(2 * (attempt + 1))
        log.error("Giving up on Discord post: %s", payload)


# ---------------------------------------------------------------------------
# Log sources
# ---------------------------------------------------------------------------
# Byte-offset sources implement size() and read_from(offset).
# Line-window sources implement fetch_lines() and are de-duplicated by overlap.

class LocalFileSource:
    def __init__(self, path: str):
        self.path = path

    def size(self) -> int:
        return os.path.getsize(self.path)

    def read_from(self, offset: int) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(offset)
            return f.read()

    def head(self, n: int) -> bytes:
        with open(self.path, "rb") as f:
            return f.read(n)


class FTPSource:
    def __init__(self, host: str, port: int, user: str, password: str, path: str, tls: bool = False, passive: bool = True):
        self.host, self.port, self.user, self.password, self.path = host, port, user, password, path
        self.tls, self.passive = tls, passive
        self._ftp: Optional[FTP] = None

    def _conn(self) -> FTP:
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except Exception:
                self._ftp = None
        ftp = FTP_TLS() if self.tls else FTP()
        ftp.connect(self.host, self.port, timeout=20)
        ftp.login(self.user, self.password)
        if self.tls:
            ftp.prot_p()  # type: ignore[attr-defined]
        ftp.set_pasv(self.passive)
        self._ftp = ftp
        return ftp

    def size(self) -> int:
        ftp = self._conn()
        ftp.voidcmd("TYPE I")
        return ftp.size(self.path) or 0

    def read_from(self, offset: int) -> bytes:
        ftp = self._conn()
        buf = io.BytesIO()
        ftp.voidcmd("TYPE I")
        ftp.retrbinary(f"RETR {self.path}", buf.write, rest=offset)
        return buf.getvalue()

    def discover(self, root: str = "/", patterns=("*.log", "*.txt", "*console*", "*output*"), max_depth: int = 5):
        """Walk the FTP tree and print files that look like logs, largest first."""
        ftp = self._conn()
        found: list = []

        def walk(d: str, depth: int):
            if depth > max_depth:
                return
            try:
                entries = list(ftp.mlsd(d))
                for name, facts in entries:
                    if name in (".", ".."):
                        continue
                    full = f"{d.rstrip('/')}/{name}"
                    if facts.get("type") == "dir":
                        walk(full, depth + 1)
                    elif any(fnmatch.fnmatch(name.lower(), p) for p in patterns):
                        found.append((int(facts.get("size", 0)), full))
                return
            except (error_perm, AttributeError):
                pass
            try:
                names = ftp.nlst(d)
            except error_perm:
                return
            for n in names:
                full = n if n.startswith("/") else f"{d.rstrip('/')}/{n}"
                base = full.rsplit("/", 1)[-1]
                if base in (".", ".."):
                    continue
                try:
                    ftp.cwd(full)
                    ftp.cwd("/")
                    walk(full, depth + 1)
                except error_perm:
                    if any(fnmatch.fnmatch(base.lower(), p) for p in patterns):
                        try:
                            ftp.voidcmd("TYPE I")
                            found.append((ftp.size(full) or 0, full))
                        except Exception:
                            found.append((0, full))

        walk(root, 0)
        found.sort(reverse=True)
        print("Candidate log files (size, path):")
        for size, path in found:
            print(f"  {size:>12}  {path}")
        if not found:
            print("  (none found — try --discover-root with a different directory)")


class SFTPSource:
    def __init__(self, host: str, port: int, user: str, password: str, path: str):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            sys.exit("SFTP source requires paramiko:  pip install paramiko")
        self.host, self.port, self.user, self.password, self.path = host, port, user, password, path
        self._sftp = None

    def _conn(self):
        import paramiko
        if self._sftp is not None:
            try:
                self._sftp.stat(self.path)
                return self._sftp
            except Exception:
                self._sftp = None
        t = paramiko.Transport((self.host, self.port))
        t.connect(username=self.user, password=self.password)
        self._sftp = paramiko.SFTPClient.from_transport(t)
        return self._sftp

    def size(self) -> int:
        return self._conn().stat(self.path).st_size

    def read_from(self, offset: int) -> bytes:
        with self._conn().open(self.path, "rb") as f:
            f.seek(offset)
            return f.read()


class HTTPSource:
    """Polls a URL that returns the raw log text. Uses Range requests when the server honours them."""

    def __init__(self, url: str, headers: Optional[dict] = None):
        self.url = url
        self.headers = headers or {}
        self._cache: bytes = b""

    def _fetch(self, offset: int = 0):
        hdrs = dict(self.headers)
        if offset:
            hdrs["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(self.url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), r.status == 206

    def size(self) -> int:
        self._cache, _ = self._fetch(0)
        return len(self._cache)

    def read_from(self, offset: int) -> bytes:
        data, partial = self._fetch(offset)
        return data if partial else data[offset:]


class NexusConsoleSource:
    """
    LOW.MS 'Nexus' panel console endpoint:
        GET https://api.prod.nexus.low.ms/user/servers/<server_id>/daemon/console?lines=N
        Authorization: Bearer <token>
    Returns the last N console lines ({"lines": [...]}). The token is the short-lived
    Auth0 session token the panel itself uses. Supply it directly (`token`) for a
    quick test, or give `token_cache` (nexus_login.TokenCache) so the monitor signs
    in with your panel account and renews the token itself. Line-window source.
    """

    def __init__(self, server_id: str, token: Optional[str] = None, lines: int = 300,
                 base_url: str = "https://api.prod.nexus.low.ms", token_cache=None):
        self.url = f"{base_url}/user/servers/{server_id}/daemon/console?lines={lines}"
        self.token = token
        self.token_cache = token_cache

    def _token(self) -> str:
        if self.token_cache is not None:
            return self.token_cache.get()
        if not self.token:
            raise RuntimeError("nexus source needs a token or login credentials")
        return self.token

    def _get(self) -> tuple[str, str]:
        req = urllib.request.Request(self.url, headers={"Authorization": f"Bearer {self._token()}",
                                                        "Accept": "application/json, text/plain"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", errors="replace"), r.headers.get("Content-Type", "")

    def fetch_lines(self) -> list[str]:
        try:
            raw, ctype = self._get()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and self.token_cache is not None:
                log.info("Panel token rejected (%s); signing in again", e.code)
                self.token_cache.invalidate()
                raw, ctype = self._get()
            else:
                raise
        if "json" in ctype:
            data = json.loads(raw)
            # Accept a few plausible shapes: ["line", ...], {"lines": [...]}, {"data": [...]}, {"data": {"lines": [...]}}
            for key in ("lines", "data", "output", "console"):
                if isinstance(data, dict) and key in data:
                    data = data[key]
                    if isinstance(data, dict) and "lines" in data:
                        data = data["lines"]
                    break
            if isinstance(data, str):
                return data.splitlines()
            if isinstance(data, list):
                return [x if isinstance(x, str) else (x.get("line") or x.get("message") or x.get("text") or json.dumps(x))
                        for x in data]
            raise ValueError(f"Unrecognised console JSON shape: {type(data).__name__}")
        return raw.splitlines()


class LowmsConsoleSource:
    """
    LOW.MS **public** API console endpoint:
        GET https://api.prod.nexus.low.ms/v1/servers/<id>/console?lines=N
        Authorization: Bearer lowms_...
    Returns {"lines": [...]} oldest first (max 500). Needs an API key with the
    `console:read` scope (Panel -> Account -> API Keys), which can be pinned to
    this one server.

    Preferred over the `nexus` source: same log lines, but a documented endpoint
    with a stable key, so there is no headless-browser sign-in to break when the
    panel's login page changes or the account gets an MFA/CAPTCHA challenge.
    Line-window source.
    """

    def __init__(self, server_id: str, api_key: str, lines: int = 300,
                 base_url: str = "https://api.prod.nexus.low.ms"):
        self.url = f"{base_url.rstrip('/')}/v1/servers/{server_id}/console?lines={min(max(int(lines), 1), 500)}"
        self.key = api_key

    def fetch_lines(self) -> list[str]:
        req = urllib.request.Request(self.url, headers={"Authorization": f"Bearer {self.key}",
                                                        "Accept": "application/json",
                                                        "User-Agent": "valheim-discord-monitor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            code = ""
            try:
                code = (json.loads(raw).get("error") or {}).get("code", "")
            except Exception:
                pass
            hint = {"missing_scope": " (the key needs the console:read scope)",
                    "not_found": " (wrong server id, or the key is pinned to another server)",
                    "invalid_key": " (bad or revoked LOWMS_API_KEY)",
                    "rate_limited": " (slow down: 60 reads/min per key)"}.get(code, "")
            raise RuntimeError(f"LOW.MS console {e.code} {code}{hint}") from None
        lines = data.get("lines") if isinstance(data, dict) else data
        if isinstance(lines, str):
            return lines.splitlines()
        if isinstance(lines, list):
            return [x if isinstance(x, str) else str(x) for x in lines]
        raise ValueError(f"Unrecognised console JSON shape: {type(lines).__name__}")



class A2SSource:
    """
    Steam server query (A2S_INFO) — works for any Valheim dedicated server, crossplay or not,
    with no log or panel access. Valheim answers on the game port + 1 (default 2457).
    Gives player COUNT only: names, logins and deaths are not available this way.
    """

    def __init__(self, host: str, port: int = 2457, timeout: float = 3.0, offline_after: int = 3):
        self.host, self.port, self.timeout = host, port, timeout
        self.offline_after = offline_after          # consecutive failed queries before "offline"
        self.failures = 0
        self.online: Optional[bool] = None          # None until the first successful/failed poll settles
        self.players: Optional[int] = None
        self.info: dict = {}

    def poll(self) -> Iterator[Event]:
        try:
            info = self._query()
        except Exception as e:
            self.failures += 1
            log.debug("A2S query failed (%d/%d): %s", self.failures, self.offline_after, e)
            if self.failures >= self.offline_after and self.online is not False:
                was_up = self.online
                self.online = False
                self.players = None
                if was_up:                           # don't announce "offline" on a cold start
                    yield Event("server_offline", None, {"server": self.info.get("name", "")})
            return

        self.failures = 0
        self.info = info
        count = info["players"]
        first = self.online is None
        if self.online is not True:
            self.online = True
            if not first:
                yield Event("server_online", None, {"count": count, "max": info["max_players"], "server": info["name"]})
        if self.players is not None and count != self.players:
            kind = "player_joined" if count > self.players else "player_left"
            delta = abs(count - self.players)
            yield Event(kind, None, {"count": count, "max": info["max_players"], "delta": delta,
                                     "who": "A viking" if delta == 1 else f"{delta} vikings",
                                     "server": info["name"]})
        self.players = count


    def _query(self) -> dict:
        from a2s_probe import a2s_info
        return a2s_info(self.host, self.port, self.timeout)


class SteamWebAPISource(A2SSource):
    """
    Same count-only events as A2SSource, but read from Steam's master server via the
    Web API instead of querying the game server directly. The game server heartbeats
    its player count to Steam OUTBOUND, so this works even when the host firewalls the
    query port. Requirements: the server is set Public (listed in the community
    browser) and a free Steam Web API key (https://steamcommunity.com/dev/apikey).
    """

    APP_ID = 892970  # Valheim

    def __init__(self, host: str, api_key: str, game_port: int = 2456, timeout: float = 10.0, offline_after: int = 3):
        super().__init__(host, game_port, timeout, offline_after)
        self.api_key = api_key

    def _query(self) -> dict:
        import socket
        ip = socket.gethostbyname(self.host)
        flt = f"\\appid\\{self.APP_ID}\\addr\\{ip}"
        url = ("https://api.steampowered.com/IGameServersService/GetServerList/v1/?"
               + urllib.parse.urlencode({"key": self.api_key, "filter": flt, "limit": 20}))
        req = urllib.request.Request(url, headers={"User-Agent": "valheim-discord-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.load(r)
        servers = data.get("response", {}).get("servers", [])
        match = [x for x in servers if int(x.get("gameport", 0)) == self.port] or servers
        if not match:
            raise LookupError(f"Steam master server has no entry for {ip}:{self.port} "
                              "(is the server set Public, and has it been up for a minute?)")
        x = match[0]
        return {"name": x.get("name", ""), "players": int(x.get("players", 0)),
                "max_players": int(x.get("max_players", 0)), "version": x.get("version", ""),
                "password": None, "map": x.get("map", "")}


# ---------------------------------------------------------------------------
# Tailers
# ---------------------------------------------------------------------------
class OffsetTailer:
    """Tails a byte-offset source, persisting the offset so restarts don't re-post.

    A server restart rewrites the log from scratch. Usually that shows up as the file
    shrinking, but if the monitor was down meanwhile the new log can already be longer
    than the old offset. Sources that can read the file's first bytes (local files) keep
    a fingerprint of them, so a replaced log is still noticed and read from the start."""

    HEAD = 256

    def __init__(self, source, state_path: str, start_at_end: bool = True):
        self.source, self.state_path = source, state_path
        self.buffer = b""
        self.head = ""
        self.offset = self._load()
        if self.offset is None:
            self.offset = self.source.size() if start_at_end else 0
            self._save()

    def _load(self) -> Optional[int]:
        try:
            with open(self.state_path) as f:
                state = json.load(f)
            self.head = state.get("head", "")
            return int(state["offset"])
        except Exception:
            return None

    def _save(self):
        if hasattr(self.source, "head") and len(bytes.fromhex(self.head)) < min(self.offset, self.HEAD):
            self.head = self.source.head(min(self.offset, self.HEAD)).hex()
        with open(self.state_path, "w") as f:
            json.dump({"offset": self.offset, "head": self.head, "saved": time.time()}, f)

    def _replaced(self) -> bool:
        if not (self.head and hasattr(self.source, "head")):
            return False
        known = bytes.fromhex(self.head)
        return self.source.head(len(known)) != known

    def poll(self) -> Iterator[str]:
        size = self.source.size()
        if size < self.offset or (self.offset and self._replaced()):
            log.info("Log rotated/replaced (size %d, offset %d); restarting from 0", size, self.offset)
            self.offset, self.buffer, self.head = 0, b"", ""
        if size == self.offset:
            return
        data = self.source.read_from(self.offset)
        if not data:
            return
        self.offset += len(data)
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b"\n")
        self._save()
        for raw in lines:
            yield raw.decode("utf-8", errors="replace")


class WindowTailer:
    """Tails a source that returns the last N lines, emitting only lines not seen before (overlap match)."""

    OVERLAP = 8

    def __init__(self, source, start_at_end: bool = True):
        self.source = source
        self.prev: list[str] = self.source.fetch_lines() if start_at_end else []

    def poll(self) -> Iterator[str]:
        cur = self.source.fetch_lines()
        if not cur:
            return
        new_start = 0
        if self.prev:
            k = min(self.OVERLAP, len(self.prev))
            tail = self.prev[-k:]
            # Find the last position in `cur` where the previous tail ends.
            for i in range(len(cur) - k, -1, -1):
                if cur[i:i + k] == tail:
                    new_start = i + k
                    break
            else:
                log.warning("No overlap with previous window — the log moved more than %d lines between polls; "
                            "some events may have been missed. Consider a shorter poll interval or more lines.",
                            len(cur))
        self.prev = cur
        for line in cur[new_start:]:
            yield line


def build_source(cfg: dict):
    src = cfg["source"]
    t = src["type"].lower()
    if t == "file":
        return LocalFileSource(src["path"])
    if t == "ftp":
        return FTPSource(src["host"], int(src.get("port", 21)), src["user"], src["password"], src["path"],
                         tls=bool(src.get("tls", False)), passive=bool(src.get("passive", True)))
    if t == "sftp":
        return SFTPSource(src["host"], int(src.get("port", 22)), src["user"], src["password"], src["path"])
    if t == "http":
        return HTTPSource(src["url"], src.get("headers"))
    if t == "nexus":
        cache = None
        login = dict(src.get("login") or {})
        login["email"] = os.environ.get("NEXUS_EMAIL", login.get("email"))
        login["password"] = os.environ.get("NEXUS_PASSWORD", login.get("password"))
        if login.get("email") and login.get("password"):
            from nexus_login import TokenCache
            cache = TokenCache(src["server_id"], login["email"], login["password"],
                               selectors=login.get("selectors"), headless=not login.get("headed", False),
                               login_timeout=float(login.get("timeout", 90)))
        token = src.get("token") or None
        if token and token.startswith("PASTE"):
            token = None
        return NexusConsoleSource(src["server_id"], token, int(src.get("lines", 300)),
                                  src.get("base_url", "https://api.prod.nexus.low.ms"), token_cache=cache)
    if t == "lowms":
        key = os.environ.get("LOWMS_API_KEY") or src.get("api_key") or (cfg.get("maintenance") or {}).get("api_key")
        if not key or key.startswith("YOUR"):
            sys.exit("The lowms source needs an API key: set LOWMS_API_KEY or source.api_key "
                     "(Panel -> Account -> API Keys, scope console:read)")
        return LowmsConsoleSource(src["server_id"], key, int(src.get("lines", 300)),
                                  src.get("base_url", "https://api.prod.nexus.low.ms"))
    if t == "a2s":
        return A2SSource(src["host"], int(src.get("port", 2457)), float(src.get("timeout", 3.0)),
                         int(src.get("offline_after", 3)))
    if t == "steamapi":
        return SteamWebAPISource(src["host"], src["api_key"], int(src.get("game_port", 2456)),
                                 float(src.get("timeout", 10.0)), int(src.get("offline_after", 3)))
    sys.exit(f"Unknown source type: {t}")


def build_maintenance(cfg: dict, source, discord: "Discord", server_name: str):
    """Unattended updates + nightly backups (maintenance.py). None when not configured."""
    m = cfg.get("maintenance") or {}
    if not m.get("enabled"):
        return None
    key = os.environ.get("LOWMS_API_KEY") or m.get("api_key")
    if not key or key.startswith("YOUR"):
        log.warning("maintenance.enabled but no LOW.MS API key (LOWMS_API_KEY or maintenance.api_key); disabled")
        return None
    server_id = m.get("server_id") or (cfg.get("source") or {}).get("server_id")
    if not server_id:
        log.warning("maintenance needs source.server_id (or maintenance.server_id); disabled")
        return None
    import maintenance
    base = m.get("base_url", maintenance.API_BASE)
    panel = None
    # Installing a game update is the one thing the public API cannot do, so it needs a
    # panel session. Reuse the log source's if it has one (the nexus source); otherwise
    # sign in on our own, so updates also work with the `lowms` source.
    tc = getattr(source, "token_cache", None)
    if tc is None and getattr(source, "token", None):
        tok = source.token
        tc = type("StaticToken", (), {"get": lambda self: tok, "invalidate": lambda self: None})()
    if tc is None and (m.get("update") or {}).get("enabled", True):
        login = dict(m.get("panel_login") or (cfg.get("source") or {}).get("login") or {})
        email = os.environ.get("NEXUS_EMAIL", login.get("email"))
        password = os.environ.get("NEXUS_PASSWORD", login.get("password"))
        if email and password:
            try:
                from nexus_login import TokenCache, check_credentials
                check_credentials(email, password)
                tc = TokenCache(server_id, email, password, selectors=login.get("selectors"),
                                headless=not login.get("headed", False),
                                login_timeout=float(login.get("timeout", 90)))
            except Exception as e:  # noqa: BLE001
                log.warning("maintenance: panel sign-in unavailable (%s); updates disabled", e)
        else:
            log.warning("maintenance: game updates need a panel login "
                        "(maintenance.panel_login, or NEXUS_EMAIL / NEXUS_PASSWORD); updates disabled")
    if tc is not None:
        panel = maintenance.PanelAPI(tc, server_id, base)

    def notify(kind: str, detail: str):
        log.info("MAINTENANCE %s: %s", kind, detail)
        if m.get("notify", True) and discord.url:
            try:
                discord.post(Event(kind, None, {"detail": detail}), server_name, {kind})
            except Exception as e:
                log.warning("Discord post failed: %s", e)

    return maintenance.Maintenance(m, maintenance.PublicAPI(key, server_id, base), panel, notify)


def prime_live_state(live, source) -> None:
    """The monitor starts at the end of the log, so fill the status board from what's
    already there: version, and when the server last booted, saved, backed up
    and was raided (local files only; one quick read at start-up).

    Log times are the server's wall clock. The file's modification time is the real time
    its last line was written, which gives the offset to convert them."""
    if not isinstance(source, LocalFileSource):
        return
    last_ts = boot = shutdown = save = backup = None
    raid = code = None
    day = None
    try:
        with open(source.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                ts = parse_log_ts(line)
                if ts is not None:
                    last_ts = ts
                if (m := RE_VERSION.search(line)):
                    live.version = m.group("version")
                elif RE_READY.search(line):
                    boot, code = last_ts, None       # a new session gets a new join code
                elif RE_SHUTDOWN.search(line):
                    shutdown, code = last_ts, None   # the old join code dies with the session
                elif RE_SAVED.search(line):
                    save = last_ts
                elif RE_BACKUP.search(line):
                    backup = last_ts
                elif (m := RE_RAID.search(line)):
                    raid = (raid_name(m.group("event")), last_ts)
                elif (m := RE_DAY.search(line)):
                    day = int(m.group("day"))
                if (m := RE_JOINCODE.search(line)):
                    code = (m.group("code"), m.group("ip") or (code[1] if code else None))
        mtime = os.path.getmtime(source.path)
    except OSError as e:
        log.debug("Couldn't pre-read the log: %s", e)
        return
    if last_ts is None:
        return
    # Round to a quarter hour: time zones are whole or quarter hours, the rest is lag.
    offset = round((mtime - last_ts) / 900.0) * 900

    def real(ts):
        return ts + offset if ts is not None else None
    if boot is not None and (shutdown is None or boot >= shutdown):
        live.up_since = real(boot)
    if code:
        live.join_code, live.server_ip = code
    if day is not None:
        live.day = day
    live.last_save, live.last_backup = real(save), real(backup)
    if raid and raid[1] is not None:
        live.last_raid = (raid[0], real(raid[1]))


def record_event(store, ev: "Event") -> None:
    """Write one parsed event into the stats database."""
    ts = ev.extra.get("ts")
    if ts is None:
        return
    if ev.kind == "login":
        store.login(ev.player, ts)
        if ev.extra.get("steam_id"):
            try:
                store.link_steam(ev.player, ev.extra["steam_id"], ts)
            except Exception as e:
                log.warning("steam link failed for %s: %s", ev.player, e)
        if ev.extra.get("platform_id"):
            try:
                store.link_platform(ev.player, ev.extra["platform_id"], ts)
            except Exception as e:
                log.warning("platform link failed for %s: %s", ev.player, e)
    elif ev.kind == "logout":
        (store.logout_stale if ev.extra.get("stale") else store.logout)(ev.player, ts)
    elif ev.kind == "death":
        store.death(ev.player, ts)
    elif ev.kind == "raid":
        store.server_event("raid", ev.extra.get("raid"), ts)
    elif ev.kind == "location":
        store.server_event("location", f"{ev.extra['loc']}|{ev.extra['zone']}", ts)
    elif ev.kind == "count" and ev.extra.get("count") is not None:
        store.concurrency(int(ev.extra["count"]), ts)


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("discord", {})
    cfg.setdefault("source", {})
    # Secrets may be supplied via environment variables instead of the file.
    for env, section, key in (("DISCORD_WEBHOOK_URL", "discord", "webhook_url"),
                              ("VALHEIM_LOG_USER", "source", "user"),
                              ("VALHEIM_LOG_PASSWORD", "source", "password"),
                              ("NEXUS_TOKEN", "source", "token"),
                              ("STEAM_API_KEY", "source", "api_key")):
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--discover", action="store_true", help="List candidate log files on the FTP server and exit")
    ap.add_argument("--discover-root", default="/", help="Directory to start --discover from")
    ap.add_argument("--replay", metavar="FILE", help="Parse a local log file and print events (no Discord posts unless --post)")
    ap.add_argument("--post", action="store_true", help="With --replay, actually post to Discord")
    ap.add_argument("--test-webhook", action="store_true", help="Send a test message to the Discord webhook and exit")
    ap.add_argument("--probe", action="store_true", help="Count mode: query the source once, print the result, and exit")
    ap.add_argument("--from-start", action="store_true", help="On first run, process the whole existing log instead of only new lines")
    ap.add_argument("--backfill", metavar="FILE", help="Load a whole log file into the stats database (no Discord posts), then exit")
    ap.add_argument("--render-site", action="store_true", help="Render the stats web page from the database once and exit")
    ap.add_argument("--refresh-steam", action="store_true", help="Fetch Steam achievements for known players once, then exit")
    ap.add_argument("--maintenance-check", action="store_true",
                    help="Read-only: verify the LOW.MS API key, list backups, show update status, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    # `docker stop` sends SIGTERM, which a PID-1 Python ignores by default (so Docker waits
    # 10 s and kills it). Treat it like Ctrl+C: stop cleanly and close the database.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    server_name = cfg.get("server_name", "the server")
    events = set(cfg.get("events", ["login", "logout", "death"]))
    d = cfg["discord"]
    # A webhook the admin bot created (/valheim setup) is kept in webhook.json, used when
    # neither DISCORD_WEBHOOK_URL nor discord.webhook_url is set.
    webhook_file = cfg.get("webhook_file") or os.path.join(
        os.path.dirname(cfg.get("state_file", "monitor_state.json")) or ".", "webhook.json")
    webhook_url = d.get("webhook_url", "")
    if not webhook_url or "XXXX" in webhook_url:
        try:
            with open(webhook_file) as f:
                webhook_url = json.load(f).get("url", "") or webhook_url
        except (OSError, ValueError):
            pass
    if "XXXX" in webhook_url:                    # the example config's placeholder
        webhook_url = ""
    discord = Discord(webhook_url, d.get("username", "Valheim"),
                      show_count=d.get("show_player_count", True), use_embeds=d.get("embeds", True),
                      messages=d.get("messages"))

    def set_webhook(url: str) -> None:
        """Called by the admin bot after it created Huginn's webhook: use it and keep it."""
        discord.url = url
        try:
            with open(webhook_file, "w") as f:
                json.dump({"url": url}, f)
            os.chmod(webhook_file, 0o600)
        except OSError as e:
            log.warning("Couldn't save %s: %s", webhook_file, e)

    db_cfg = cfg.get("database") or {}
    db_enabled = bool(db_cfg.get("path")) and db_cfg.get("enabled", True)
    site_cfg = cfg.get("stats_site") or {}

    def open_store(reconcile: bool = False):
        from stats_db import Store
        return Store(db_cfg["path"], source=cfg.get("source", {}).get("type", "log"), reconcile=reconcile)

    def render_site(reason=""):
        out = site_cfg.get("output")
        if not (db_enabled and out):
            return
        try:
            import stats_site
            stats_site.render(db_cfg["path"], out, cfg)
            log.info("Rendered stats page -> %s %s", out, reason)
        except Exception as e:
            log.warning("Stats page render failed: %s", e)

    if args.render_site:
        if not (db_enabled and site_cfg.get("output")):
            sys.exit("Configure database.path and stats_site.output first")
        render_site("(--render-site)")
        return

    if args.refresh_steam:
        if not db_enabled:
            sys.exit("Configure a database.path first")
        key = os.environ.get("STEAM_API_KEY") or (cfg.get("steam") or {}).get("api_key") \
            or cfg.get("source", {}).get("api_key")
        if not key:
            sys.exit("Set STEAM_API_KEY or steam.api_key")
        import steam
        store = open_store()
        n = steam.update_all(store, key, limit=int((cfg.get("steam") or {}).get("top_n", 25)))
        store.close()
        print(f"Refreshed {n} Steam profile(s)")
        render_site("(after steam refresh)")
        return

    if args.backfill:
        if not db_enabled:
            sys.exit("Configure a database.path to backfill into")
        store = open_store()
        parser = ValheimLogParser()
        n = 0
        with open(args.backfill, encoding="utf-8", errors="replace") as f:
            for line in f:
                for ev in parser.feed(line):
                    record_event(store, ev)
                    n += 1
        store.close()
        log.info("Backfilled %d events from %s", n, args.backfill)
        render_site("(after backfill)")
        return

    if args.replay:
        parser = ValheimLogParser()
        with open(args.replay, encoding="utf-8", errors="replace") as f:
            for line in f:
                for ev in parser.feed(line):
                    print(f"{ev.kind:12} {ev.player or '':24} {ev.extra}")
                    if args.post:
                        discord.post(ev, server_name, events)
        return

    if args.test_webhook:
        if not discord.url:
            sys.exit("No Discord webhook URL configured")
        discord.send({"username": discord.username, "content": f"✅ Valheim monitor connected for **{server_name}**."})
        print("Test message sent.")
        return

    source = build_source(cfg)
    if args.maintenance_check:
        mnt = build_maintenance(cfg, source, discord, server_name)
        if not mnt:
            sys.exit("Maintenance is not enabled/configured (see the maintenance block in config.example.json)")
        for line in mnt.report():
            print(line)
        return
    if args.discover:
        if not isinstance(source, FTPSource):
            sys.exit("--discover only works with the ftp source type")
        source.discover(args.discover_root)
        return

    if not discord.url:
        if not (cfg.get("admin_bot") or {}).get("enabled"):
            sys.exit("No Discord webhook URL configured (config discord.webhook_url or DISCORD_WEBHOOK_URL)")
        log.warning("No Discord webhook URL yet: public posts are skipped until you set DISCORD_WEBHOOK_URL, "
                    "or run /valheim setup apply and the bot creates Huginn's webhook")

    interval = float(cfg.get("poll_interval_seconds", 10))

    if isinstance(source, A2SSource) and args.probe:
        info = source._query()
        print(f"{info['name']}  —  {info['players']}/{info['max_players']} players  (v{info.get('version','?')})")
        return

    if isinstance(source, A2SSource):
        # Count-only mode: no log parsing, just diff the player count each poll.
        default_events = {"player_joined", "player_left", "server_online", "server_offline"}
        events = set(cfg.get("events") or default_events) & default_events or default_events
        log.info("Monitoring %s:%d via %s for %s; posting %s every %.0fs", source.host, source.port,
                 type(source).__name__, server_name, sorted(events), interval)
        while True:
            try:
                for ev in source.poll():
                    log.info("EVENT %-14s %s", ev.kind, ev.extra)
                    discord.post(ev, server_name or source.info.get("name", ""), events)
            except KeyboardInterrupt:
                log.info("Stopping")
                return
            except Exception as e:
                log.warning("Poll failed: %s", e)
            time.sleep(interval)

    if hasattr(source, "fetch_lines"):
        tailer = WindowTailer(source, start_at_end=not args.from_start)
    else:
        tailer = OffsetTailer(source, cfg.get("state_file", "monitor_state.json"), start_at_end=not args.from_start)
    parser = ValheimLogParser()
    # Restore who's online from before a restart (see ValheimLogParser.state_dict).
    parser_state_path = cfg.get("parser_state_file") or os.path.join(
        os.path.dirname(cfg.get("state_file", "monitor_state.json")) or ".", "parser_state.json")
    try:
        with open(parser_state_path) as f:
            parser.load_state(json.load(f))
        if parser.s.online:
            log.info("Restored %d player(s) online from before the restart: %s", len(parser.s.online),
                     ", ".join(sorted(parser.s.online)))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log.warning("Couldn't read %s: %s", parser_state_path, e)
    saved_parser_state = json.dumps(parser.state_dict(), sort_keys=True)

    def save_parser_state() -> None:
        nonlocal saved_parser_state
        now_state = json.dumps(parser.state_dict(), sort_keys=True)
        if now_state == saved_parser_state:
            return
        try:
            with open(parser_state_path + ".tmp", "w") as f:
                f.write(now_state)
            os.replace(parser_state_path + ".tmp", parser_state_path)
            saved_parser_state = now_state
        except OSError as e:
            log.warning("Couldn't save %s: %s", parser_state_path, e)
    extra_events = {"raid", "version_mismatch", "session_summary", "welcome", "milestone", "weekly_recap",
                    "update", "achievement"}
    log_events = {"login", "logout", "death", "respawn", "server_up",
                  "server_restart", "server_online", "server_offline", "join_refused"} | extra_events
    default_log_events = {"login", "logout", "death", "server_restart", "server_online", "server_offline"}
    events = set(cfg.get("events") or ()) & log_events or default_log_events

    store = open_store(reconcile=True) if db_enabled else None
    render_interval = float(site_cfg.get("render_interval_seconds", 60))
    last_render = 0.0

    # Steam achievements: refresh in the background on its own (slow) cadence.
    steam_cfg = cfg.get("steam") or {}
    steam_key = os.environ.get("STEAM_API_KEY") or steam_cfg.get("api_key") or cfg.get("source", {}).get("api_key")
    steam_enabled = bool(store) and steam_cfg.get("enabled", bool(steam_key)) and bool(steam_key)
    steam_interval = float(steam_cfg.get("refresh_seconds", 1800))
    steam_limit = int(steam_cfg.get("top_n", 25))
    last_steam = 0.0
    steam_thread: Optional[threading.Thread] = None
    steam_done = threading.Event()        # set by the thread when new data wants a re-render

    def refresh_steam():
        # Runs on its own thread with its own connection: a full refresh is ~25 slow API
        # calls (minutes if Steam is down), which must not hold up log tailing.
        try:
            import steam
            st = open_store()
            try:
                # "achievement" in events: post Steam achievements unlocked since the last refresh.
                fresh = [] if "achievement" in events else None
                if steam.update_all(st, steam_key, limit=steam_limit, announce=fresh):
                    steam_done.set()
                if fresh:
                    import community
                    for item in fresh:
                        discord.post_embed("achievement",
                                           community.render_unlocks(st.conn, item["steam_id"], item["unlocks"]),
                                           events)
            finally:
                st.close()
        except Exception as e:
            log.warning("Steam refresh failed: %s", e)

    # Refused joins go to a private admin channel with Permit / Ban buttons. They reach the
    # public webhook too only if "join_refused" is listed in `events`.
    admin = None
    if (cfg.get("admin_bot") or {}).get("enabled"):
        from admin_bot import build_admin_bot
        admin = build_admin_bot(cfg, server_name)

    import extras
    import stats_db
    live = extras.LiveState()
    prime_live_state(live, source)
    for name in parser.s.online:              # restored from before a restart; join time unknown
        live.online.setdefault(name, None)
    if parser.s.server_count is not None and parser.s.online:
        live.count = parser.s.server_count
    needs_db = events & {"welcome", "milestone", "weekly_recap"}
    if needs_db and not store:
        log.warning("%s need the stats database (database.path); they're off", ", ".join(sorted(needs_db)))
    recap = extras.WeeklyRecap(cfg.get("weekly_recap") or {}) if store and "weekly_recap" in events else None
    backups = None
    if (cfg.get("backups") or {}).get("dest_dir"):
        backups = extras.BackupCopier(cfg["backups"])
        if not os.path.isdir(backups.dest):
            # Don't create it: inside Docker that would quietly fill the container instead
            # of the disk you meant. The folder must exist (be mounted) already.
            log.warning("backups.dest_dir %s doesn't exist (mount it in docker-compose.yml); "
                        "backup copies are off", backups.dest)
            backups = None
        elif not os.path.isdir(backups.src):
            log.warning("backups.source_dir %s not found; world backups won't be copied", backups.src)
    if backups:
        backups.copy_in_background()          # catch up on anything made while we were down
    last_backup_check = 0.0
    mismatch_posted: dict = {}

    def post_update(text: str) -> None:
        discord.post(Event("update", None, {"detail": text}), server_name, events)

    # Host-side auto-updater (self-hosted): read its log, share the player count, and let
    # the bot ask it to check or restart. See host/README.md.
    upd = upd_tailer = None
    upd_cfg = cfg.get("updater") or {}
    if upd_cfg.get("log") or upd_cfg.get("bot_dir"):
        import updater
        upd = updater.UpdateWatcher(upd_cfg)
        if upd.bot_dir and not os.path.isdir(upd.bot_dir):
            log.warning("updater.bot_dir %s doesn't exist (mount it); status.json and requests are off", upd.bot_dir)
            upd.bot_dir = ""
    if admin:
        admin.attach(live=live, backups=backups, updater=upd, announce=post_update,
                     post_embed=lambda embed: discord.post_embed("titles", embed, {"titles"}),
                     db_path=db_cfg["path"] if db_enabled else None, webhook_url=discord.url,
                     set_webhook=set_webhook)

    health = extras.HealthWatch(cfg.get("health") or {})
    daily = extras.DailyRestart(cfg["daily_restart"]) if (cfg.get("daily_restart") or {}).get("time") else None
    if daily and not (upd and upd.bot_dir):
        log.warning("daily_restart needs the updater link (updater.bot_dir, host/README.md); it's off")
        daily = None
    if daily and store:
        daily.done_date = store.get_meta("daily_restart_date")   # survive a monitor restart

    def mention_for(player: str):
        """The Discord user linked to a character (or who asked for access as it), if any."""
        if not store:
            return None
        try:
            import community
            return community.linked_user(store.conn, player)
        except Exception:  # noqa: BLE001
            return None

    def announce(ev: "Event") -> None:
        """Post one event, upgraded where the extras apply: a first-ever login becomes a
        welcome, a logout becomes a session summary, and milestones follow."""
        first_visit = False
        if ev.kind == "login" and store and "welcome" in events:
            first_visit = stats_db.player_totals(store.conn, ev.player)["sessions"] == 0
        if store:
            try:
                record_event(store, ev)
            except Exception as e:
                log.warning("DB write failed for %s: %s", ev.kind, e)
        prev_version = live.version
        was_empty = live.count == 0 and not live.online
        summary = live.observe(ev)
        if admin and ev.kind == "login":
            admin.on_login(ev.player, was_empty)
        elif admin and ev.kind == "logout":
            admin.on_logout(ev.player)
        elif admin and ev.kind == "version_mismatch" and ev.extra.get("platform_id"):
            admin.notify_version(ev.extra)
        alert = health.observe(ev)
        if alert:
            log.warning("HEALTH %s", alert)
            if admin:
                admin.post_admin(alert)
        if ev.kind == "server_version" and prev_version and ev.extra.get("version") != prev_version:
            post_update(f"✅ Valheim updated: **{prev_version}** → **{ev.extra.get('version')}**. "
                        f"Players need the same version to join.")
        if maint and maint.suppressing(ev.kind):
            return
        if ev.kind == "logout" and summary and "session_summary" in events:
            discord.post(Event("logout_summary", ev.player, {**ev.extra, **summary}), server_name, {"logout_summary"})
        elif first_visit:
            discord.post(Event("welcome", ev.player, {**ev.extra, "mention": mention_for(ev.player)}),
                         server_name, {"welcome"})
        elif ev.kind == "version_mismatch":
            key = (ev.extra["their"], ev.extra["mine"])
            if time.time() - mismatch_posted.get(key, 0) < 3600:
                return
            mismatch_posted[key] = time.time()
            detail = (f"Someone tried to join **{server_name}** with a **newer** version of Valheim "
                      f"(network {ev.extra['their']}, server {ev.extra['mine']}): the server needs an update."
                      if ev.extra["newer"] else
                      f"Someone tried to join **{server_name}** with an **older** version of Valheim "
                      f"(network {ev.extra['their']}, server {ev.extra['mine']}): they need to update their game.")
            discord.post(Event("version_mismatch", None, {**ev.extra, "detail": detail}), server_name, events)
        else:
            discord.post(ev, server_name, events)
        if store and "milestone" in events and ev.kind in ("logout", "death") and not ev.extra.get("stale"):
            totals = stats_db.player_totals(store.conn, ev.player)
            detail = None
            if ev.kind == "logout" and summary:
                h = extras.hours_crossed(totals["seconds"] - summary["duration_seconds"], totals["seconds"])
                if h:
                    detail = f"has now spent **{h} hours** in {server_name}!"
            elif ev.kind == "death":
                n = extras.deaths_reached(totals["deaths"])
                if n:
                    detail = f"has died **{n} times** in {server_name}. Odin is keeping count."
            if detail:
                discord.post(Event("milestone", ev.player, {"detail": detail, "mention": mention_for(ev.player)}),
                             server_name, events)

    maint = build_maintenance(cfg, source, discord, server_name)
    if maint:
        log.info("Maintenance on: checks every %.0f min when empty; backup window %s %s; updates %s%s",
                 maint.interval / 60, (cfg.get("maintenance") or {}).get("backup", {}).get("window", "02:00-06:00"),
                 (cfg.get("maintenance") or {}).get("timezone", "America/Los_Angeles"),
                 "on" if maint.update_enabled else "off", " (DRY RUN)" if maint.dry_run else "")

    # If a shutdown isn't followed by a boot within this window, treat it as offline
    # (vs a quick restart that comes back) and post the offline message once.
    offline_grace = float(cfg.get("offline_grace_seconds", 300))
    down_since = None
    offline_posted = False
    log.info("Monitoring %s source for %s; posting %s every %.0fs%s%s", cfg["source"]["type"], server_name,
             sorted(events), interval, "; recording stats" if store else "",
             "; admin bot on" if admin else "")
    render_site("(startup)")

    # Valheim logs "Connections N" every 10 minutes, so a log silent for longer than this
    # means the server is down (crashed, or stopped without a shutdown line).
    stale_after = float(((cfg.get("admin_bot") or {}).get("status_channel") or {}).get("stale_after_seconds", 900))
    last_line_at = time.time()
    booted = False                         # saw a boot: until a count line, the server is empty

    backoff = interval
    while True:
        try:
            changed = False
            for line in tailer.poll():
                last_line_at = time.time()
                log.debug("LOG: %s", line)
                for ev in parser.feed(line):
                    if ev.kind != "count":
                        log.info("EVENT %-8s %s %s", ev.kind, ev.player or "", ev.extra)
                    changed = changed or bool(store)
                    if ev.kind == "join_refused" and admin:
                        admin.notify_refused(ev.player, ev.extra["host_id"], ev.extra.get("ts"))
                    if ev.kind == "backup_saved" and backups:
                        backups.copy_in_background()
                    if maint:
                        maint.observe(ev, len(parser.s.online))
                    announce(ev)
                    # Track down/up so we can tell a lingering outage from a quick restart.
                    if ev.kind == "server_restart":
                        down_since, offline_posted = time.time(), False
                    elif ev.kind in ("server_online", "login"):
                        down_since = None
                    if ev.kind == "server_online":
                        booted = True
            backoff = interval
            save_parser_state()
            now = time.time()
            count = parser.s.server_count
            if count is None and (parser.s.online or booted):
                count = len(parser.s.online)
            offline = parser.s.down or now - last_line_at > stale_after
            if admin:
                admin.set_status(offline, count)
            if upd:
                upd.write_status(count, offline)
                if upd.log_path and upd_tailer is None and os.path.exists(upd.log_path):
                    upd_tailer = OffsetTailer(LocalFileSource(upd.log_path),
                                              upd_cfg.get("state_file", "updater_state.json"))
                if upd_tailer:
                    try:
                        for uline in upd_tailer.poll():
                            for target, text in upd.handle(uline):
                                log.info("UPDATER %s: %s", target, text)
                                if target == "public":
                                    post_update(text)
                                elif admin:
                                    admin.post_admin(text)
                    except OSError as e:
                        log.debug("updater log not readable: %s", e)
            if store and changed and parser.last_ts is not None:
                try:
                    store.record_clock_offset(parser.last_ts, now)
                except Exception as e:
                    log.debug("clock offset not recorded: %s", e)
            # Our own maintenance can legitimately keep it down past the grace period: hold
            # the alert (don't drop it) until the maintenance quiet period is over.
            if (down_since and not offline_posted and now - down_since > offline_grace
                    and not (maint and maint.suppressing("server_offline"))):
                discord.post(Event("server_offline", None, {}), server_name, events)
                offline_posted = True
                down_since = None
            if changed and store and site_cfg.get("output") and now - last_render >= render_interval:
                render_site()
                last_render = now
            if steam_enabled and now - last_steam >= steam_interval \
                    and not (steam_thread and steam_thread.is_alive()):
                steam_thread = threading.Thread(target=refresh_steam, name="steam-refresh", daemon=True)
                steam_thread.start()
                last_steam = now
            if steam_done.is_set():
                steam_done.clear()
                render_site("(steam refresh)")
            if recap:
                try:
                    week = recap.due(store)
                    if week:
                        off = int(store.get_meta("log_clock_offset") or 0)
                        embed = extras.WeeklyRecap.build(store.conn, int(now) - off, server_name)
                        if embed:
                            discord.post_embed("weekly_recap", embed, events)
                        store.set_meta("weekly_recap_week", week)
                except Exception as e:
                    log.warning("Weekly recap failed: %s", e)
            if daily and daily.due(datetime.now().astimezone(), not offline and count == 0):
                err = upd.request("restart")
                log.info("Daily restart: %s", err or "requested (server empty)")
                if store and not err:
                    store.set_meta("daily_restart_date", daily.done_date)
                if admin:
                    admin.post_admin(f"🔄 Daily restart: {err}" if err else
                                     "🔄 Daily restart: the server was empty, so it's restarting now.")
            if backups and now - last_backup_check >= 600:
                last_backup_check = now
                msg = backups.stale_alert(not (parser.s.down or now - last_line_at > stale_after))
                if msg:
                    log.warning(msg)
                    if admin:
                        admin.post_admin("⚠️ " + msg)
            if maint:
                try:
                    maint.tick()
                except Exception as e:
                    log.warning("Maintenance check failed: %s", e)
        except KeyboardInterrupt:
            log.info("Stopping")
            if store:
                store.close()
            return
        except Exception as e:
            log.warning("Poll failed: %s (retrying in %.0fs)", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:          # Ctrl+C or docker stop while sleeping between polls
        log.info("Stopping")
