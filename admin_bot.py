#!/usr/bin/env python3
"""
Discord admin bot: act on refused join attempts from Discord.

When someone on bannedlist.txt, or missing from permittedlist.txt, tries to join,
Valheim logs
    Player <name> : <id> is blacklisted or not in whitelist.
The monitor turns that into a `join_refused` event. This bot posts it to a private
admin channel with buttons, and edits the server's list files when an admin clicks:

    Permit  - take the id off bannedlist.txt and, if the server uses a permitted
              list, add it to permittedlist.txt
    Ban     - add the id to bannedlist.txt (and take it off permittedlist.txt)
    Ignore  - just close the notice

The same actions are available as slash commands (/valheim permit|unpermit|ban|
unban|lists), for ids you already know.

Optionally it also keeps a (locked) voice channel's name showing the server's status,
e.g. "🟢 Valheim: 3 online" / "🟢 Valheim: empty" / "🔴 Valheim: offline"
(`admin_bot.status_channel`), and a live status-board message listing who's on, the
version, last save/backup and last raid (`admin_bot.status_board`). Anyone can use
/valheim online; /valheim backups is for admins.

It needs write access to the server's save dir (where the list files live), so it
suits the self-hosted `file` source, where the monitor runs next to the server.
Webhooks are one-way, so this is a real bot: `pip install discord.py`, a bot token,
and it joins the Discord server with the `applications.commands` + `bot` scopes.
Only the users / roles in `admin_user_ids` / `admin_role_ids` can press the buttons.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Optional

# Commands with public replies, and the /valheim setup channel they belong in. Others (their
# replies are private) work anywhere; admins can run anything anywhere.
COMMAND_PLACES = {"stats": "bots", "top": "bots", "titles": "bots", "online": "bots", "plan": "plans"}

# Huginn's posts the bot reacts to, so people can react along.
REACTIONS = {"raid": "⚔️", "achievement": "🏅", "milestone": "🏆", "welcome": "👋", "titles": "👑",
             "weekly_recap": "📜", "version_mismatch": "⚠️"}

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

log = logging.getLogger("valheim-monitor.bot")

LIST_FILES = {"permitted": "permittedlist.txt", "banned": "bannedlist.txt", "admin": "adminlist.txt"}
STEAM_PREFIXES = ("V_", "Steam_")      # Steam ids: "V_" since Valheim 1.0, "Steam_" before
# A platform id: "V_7656…", "Steam_7656…", "Xbox_…", "PlayStation_…", or a bare SteamID64.
VALID_ID = re.compile(r"^(?:[A-Za-z]+_[A-Za-z0-9]+|\d{5,20})$")
BTN_PREFIX = "vdm"          # custom_id = "vdm:<action>:<id>", so buttons survive a bot restart


# ---------------------------------------------------------------------------
# The list files
# ---------------------------------------------------------------------------
class ServerLists:
    """Reads and edits Valheim's permittedlist / bannedlist / adminlist.txt.

    One id per line; lines starting with // are comments (the server writes one as a
    header) and are preserved. Writes go to a temp file that is then swapped in, so the
    server never reads a half-written list; it keeps the file's owner and mode."""

    def __init__(self, save_dir: str):
        self.save_dir = save_dir
        self.lock = threading.Lock()

    def path(self, which: str) -> str:
        return os.path.join(self.save_dir, LIST_FILES[which])

    def _read_raw(self, which: str) -> list[str]:
        try:
            with open(self.path(which), encoding="utf-8-sig") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def ids(self, which: str) -> list[str]:
        return [ln.strip() for ln in self._read_raw(which) if ln.strip() and not ln.strip().startswith("//")]

    @staticmethod
    def _variants(pid: str) -> set[str]:
        """An id as the server may print or store it: V_7656…, Steam_7656… and a bare
        7656… are the same Steam player."""
        pid = pid.strip()
        sid = steam64(pid)
        return {pid} | ({sid} | {p + sid for p in STEAM_PREFIXES} if sid else set())

    def contains(self, which: str, pid: str) -> bool:
        return bool(self._variants(pid) & set(self.ids(which)))

    def styled(self, pid: str) -> str:
        """Write a Steam id the way the list files already do. The log may say
        "Steam_7656…" while a Valheim 1.0 server's lists say "V_7656…"; copy the files."""
        sid = steam64(pid.strip())
        if not sid:
            return pid.strip()
        counts = {"V_": 0, "Steam_": 0, "": 0}
        for which in LIST_FILES:
            for x in self.ids(which):
                if steam64(x):
                    counts[next((p for p in STEAM_PREFIXES if x.startswith(p)), "")] += 1
        prefix = max(counts, key=counts.get) if any(counts.values()) else None
        return pid.strip() if prefix is None else prefix + sid

    def add(self, which: str, pid: str) -> bool:
        pid = self.styled(pid)
        with self.lock:
            if self.contains(which, pid):
                return False
            lines = self._read_raw(which) or [f"// List {which} players ID  ONE per line"]
            while lines and not lines[-1].strip():
                lines.pop()
            self._write(which, lines + [pid.strip()])
            return True

    def remove(self, which: str, pid: str) -> bool:
        with self.lock:
            v = self._variants(pid)
            lines = self._read_raw(which)
            kept = [ln for ln in lines if ln.strip() not in v]
            if len(kept) == len(lines):
                return False
            self._write(which, kept)
            return True

    def _write(self, which: str, lines: list[str]) -> None:
        path = self.path(which)
        try:
            st = os.stat(path)
            mode, uid, gid = st.st_mode & 0o777, st.st_uid, st.st_gid
        except FileNotFoundError:
            # New file: give it the save dir's owner (the server's user), not root's.
            st = os.stat(self.save_dir)
            mode, uid, gid = 0o644, st.st_uid, st.st_gid
        fd, tmp = tempfile.mkstemp(prefix=f".{LIST_FILES[which]}.", dir=self.save_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
            os.chmod(tmp, mode)
            try:
                os.chown(tmp, uid, gid)
            except PermissionError:
                pass                      # not root: the file stays ours, which is fine
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def refusal_reason(self, pid: str) -> str:
        if self.contains("banned", pid):
            return "on the ban list"
        if self.ids("permitted") and not self.contains("permitted", pid):
            return "not on the permitted list"
        return "refused by the server"

    # -- the two admin actions ------------------------------------------------
    def permit(self, pid: str) -> list[str]:
        done = []
        if self.remove("banned", pid):
            done.append("removed from bannedlist.txt")
        # Adding the first id to an empty permitted list would lock everyone else out,
        # so only add when the server already runs a permitted list.
        if self.ids("permitted"):
            if self.add("permitted", pid):
                done.append("added to permittedlist.txt")
        return done

    def ban(self, pid: str) -> list[str]:
        done = []
        if self.add("banned", pid):
            done.append("added to bannedlist.txt")
        if self.remove("permitted", pid):
            done.append("removed from permittedlist.txt")
        return done


def steam64(pid: str) -> Optional[str]:
    """The SteamID64 inside a Steam platform id (V_…, Steam_… or bare), else None."""
    for p in STEAM_PREFIXES:
        if pid.startswith(p):
            pid = pid[len(p):]
            break
    return pid if pid.isdigit() and pid.startswith("7656") else None


def steam_profile(pid: str) -> Optional[str]:
    sid = steam64(pid)
    return f"https://steamcommunity.com/profiles/{sid}" if sid else None


# ---------------------------------------------------------------------------
# Status channel: a voice channel whose name shows who's on
# ---------------------------------------------------------------------------
class StatusChannel:
    """Decides what the status channel should be called and when it may be renamed.

    Discord lets a bot rename a channel only twice per 10 minutes, so renames are spaced
    at least `min_interval_seconds` apart and only the latest wanted name is applied:
    a burst of joins and leaves costs one rename, not ten."""

    DEFAULTS = {"online": "🟢 Valheim: {count} online", "empty": "🟢 Valheim: empty",
                "offline": "🔴 Valheim: offline"}

    def __init__(self, cfg: dict, server_name: str, clock=time.time):
        self.channel_id = int(cfg["channel_id"])
        self.templates = {k: cfg.get(k, v) for k, v in self.DEFAULTS.items()}
        self.min_interval = max(float(cfg.get("min_interval_seconds", 300)), 300.0)
        self.server_name = server_name
        self.clock = clock
        self.wanted: Optional[str] = None
        self.current: Optional[str] = None
        self.last_rename = 0.0

    def set_state(self, offline: bool, count: Optional[int]) -> None:
        """Called from the monitor thread. count=None while not offline means "don't know
        yet" (e.g. just started): keep whatever the channel says."""
        if offline:
            key = "offline"
        elif count is None:
            return
        else:
            key = "online" if count > 0 else "empty"
        name = self.templates[key].format_map({"count": count or 0, "server": self.server_name})
        self.wanted = name.strip()[:100] or None

    def due(self) -> Optional[str]:
        """The name to rename to now, or None (nothing to change, or too soon)."""
        if not self.wanted or self.wanted == self.current:
            return None
        if self.clock() - self.last_rename < self.min_interval:
            return None
        return self.wanted

    def waiting(self) -> Optional[tuple]:
        """(name, when) if a rename is wanted but held back by the rate limit."""
        if not self.wanted or self.wanted == self.current:
            return None
        when = self.last_rename + self.min_interval
        return (self.wanted, when) if self.clock() < when else None

    def renamed(self, name: str) -> None:
        self.current, self.last_rename = name, self.clock()


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------
def norm_name(name: str) -> str:
    """Channel names compared the way Discord stores text channels (case, spaces)."""
    return (name or "").lower().replace(" ", "-")


def _assignable_role(guild, name: str):
    """A role with this name that can be handed out. Skips roles Discord manages itself,
    like the role a bot gets when it joins, which is named after the bot (a bot called
    "Odin" has a role "Odin" that nobody else can be given)."""
    return next((r for r in guild.roles if r.name == name and not getattr(r, "managed", False)), None)


class AdminBot:
    """Runs a discord.py client on its own thread; the monitor's (synchronous) main
    loop hands it events with notify_refused(), which is thread-safe."""

    def __init__(self, cfg: dict, server_name: str):
        import discord  # noqa: F401  (fail at start-up, not on the first event)
        self.token = (os.environ.get("DISCORD_BOT_TOKEN") or cfg.get("token") or "").strip().strip('"\'')
        if not self.token or self.token.startswith("YOUR"):
            raise ValueError("admin_bot needs a bot token (admin_bot.token or DISCORD_BOT_TOKEN)")
        self.channel_id = int(cfg["channel_id"])
        self.guild_id = int(cfg["guild_id"]) if cfg.get("guild_id") else None
        self.admin_users = {int(x) for x in cfg.get("admin_user_ids", [])}
        self.admin_roles = {int(x) for x in cfg.get("admin_role_ids", [])}
        if not (self.admin_users or self.admin_roles):
            raise ValueError("admin_bot needs admin_user_ids and/or admin_role_ids (who may press the buttons)")
        self.lists = ServerLists(cfg["save_dir"])
        if not os.path.isdir(self.lists.save_dir):
            log.warning("admin_bot: save_dir %s not found — notices will post, but the buttons can't edit the lists",
                        self.lists.save_dir)
        self.server_name = server_name
        self.cooldown = float(cfg.get("repeat_cooldown_seconds", 600))
        sc = cfg.get("status_channel") or {}
        self.status = StatusChannel(sc, server_name) if str(sc.get("channel_id", "")).isdigit() else None
        if sc.get("channel_id") and not self.status:
            log.warning("admin_bot: status_channel.channel_id isn't a channel ID; status channel off")
        self._status_task = None
        bc = cfg.get("status_board") or {}
        self.board_channel = int(bc["channel_id"]) if str(bc.get("channel_id", "")).isdigit() else None
        if bc.get("channel_id") and not self.board_channel:
            log.warning("admin_bot: status_board.channel_id isn't a channel ID; status board off")
        self.board_state = bc.get("state_file", "status_board.json")
        self.join = cfg.get("join") or {}         # address / password / note for /valheim join
        # The host's world-settings.env, read-only via the /valheim_home mount.
        self.world_file = (cfg.get("world_settings") or {}).get("file", "/valheim_home/world-settings.env")
        self._board_task = None
        self.live = None                     # extras.LiveState, from attach()
        self.backups = None                  # extras.BackupCopier, from attach()
        self.updater = None                  # updater.UpdateWatcher, from attach()
        self.announce = None                 # posts a line to the public webhook channel
        self._countdown = None               # running /valheim restart countdown task
        self._countdown_cancel = None
        # Community features (community.py), stored in the stats database.
        self.db_path = None                  # from attach()
        self._db = None                      # opened lazily on the bot's own thread
        # "In Valheim" role: a configured role ID, or "online_role": true (or a role name) to
        # have the bot find or create it.
        role = str(cfg.get("online_role_id", ""))
        self.online_role = int(role) if role.isdigit() else None
        auto = cfg.get("online_role")
        self.online_role_name = (auto.strip() if isinstance(auto, str) and auto.strip()
                                 else "In Valheim" if auto is True else None)
        # "owner_role": true (or a role name): a role for the Discord server's owner.
        owner = cfg.get("owner_role")
        self.owner_role_name = (owner.strip() if isinstance(owner, str) and owner.strip()
                                else "Odin" if owner is True else None)
        self._owner_task = None
        self._role_task = None                # keeps the "In Valheim" role in step with who's online
        # Stat channels (stat_channels.py): locked voice channels the bot creates and renames.
        stc = cfg.get("stat_channels") or {}
        self.stats_on = bool(stc.get("enabled", False))
        # "split" (default): one category per group (live / this week / titles); "single": one.
        self.stats_layout = "single" if str(stc.get("layout", "split")).lower() == "single" else "split"
        self.stats_category = str(stc.get("category") or "📊 Valheim")[:100]
        self.stats_categories = {k: str(v)[:100] for k, v in (stc.get("categories") or {}).items() if v}
        self._stats_show_cfg = stc.get("show")
        self.stats_show: list = []           # set in _start_tasks, once the rest of the config is read
        import stat_channels
        # The players channel takes over an existing status_channel (moved into the category).
        self.stats_take_status = bool(self.stats_on and self.status and (
            self._stats_show_cfg is None or "players" in stat_channels.expand(self._stats_show_cfg)))
        self._owner_name: Optional[str] = None
        self._stats_task = None
        self._stat_ids: dict = {}            # channel IDs when there's no database to keep them in
        self.webhook_url = ""                # from attach(): /valheim setup finds Huginn's channel with it
        self.set_webhook = None
        self._webhook_checked = False
        lfg = cfg.get("lfg") or {}
        self.remind_minutes = int(lfg.get("reminder_minutes", 15))
        self.discord_events = bool(lfg.get("discord_event", False))
        self.map_enabled = bool((cfg.get("map") or {}).get("enabled", True))
        self.map_seed = str((cfg.get("map") or {}).get("seed") or "").strip()
        # /valheim link sets a member's server nickname to the character (only if they have none).
        self.link_nickname = bool(cfg.get("link_nickname", False))
        # Commands used in the wrong channel get a private "run it in #…" instead. true: the
        # channels /valheim setup made; false: off; {"stats": "<channel id>", …}: overrides.
        cc = cfg.get("command_channels", True)
        self.command_channels_on = cc is not False
        self.command_channel_ids = {k: int(v) for k, v in cc.items() if str(v).isdigit()} \
            if isinstance(cc, dict) else {}
        # Handled refused-join notices are deleted this many hours later (0 keeps them).
        self.tidy_hours = float(cfg.get("tidy_notices_hours", 24))
        # Weekly title roles for the /valheim top leaders (community.TITLES).
        tc = cfg.get("titles") or {}
        self.titles_on = bool(tc.get("enabled", False))
        self.titles_period = "week" if str(tc.get("period", "all")).lower() == "week" else "all"
        day = str(tc.get("day", "sunday")).lower()
        self.titles_weekday = DAYS.index(day) if day in DAYS else 6
        self.titles_hour = int(tc.get("hour", 18))
        ch = str(tc.get("channel_id", ""))
        self.titles_channel = int(ch) if ch.isdigit() else None
        self.title_role_ids = {k: int(v) for k, v in (tc.get("roles") or {}).items() if str(v).isdigit()}
        self._titles_task = None
        self._titles_lock = None
        self._titles_warned = False
        self._attached = False               # attach() has run
        self.post_embed = None               # posts an embed to the public webhook channel
        self._refused: dict = {}             # platform id -> character name, for Permit follow-ups
        self._dm_sent: dict = {}             # (user, reason) -> time, so a rejoin doesn't spam DMs
        self._plan_task = None
        self.last_notice: dict[str, float] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ready = threading.Event()
        self.client = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        t = threading.Thread(target=self._run, name="discord-admin-bot", daemon=True)
        t.start()
        deadline = time.time() + 60
        while t.is_alive() and not self.ready.wait(1):
            if time.time() > deadline:
                log.warning("admin_bot: not connected to Discord after 60s; will keep trying in the background")
                return

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.client = self._build_client()

        async def main():
            async with self.client:
                await self.client.start(self.token)      # reconnects by itself after drops

        try:
            # Not client.run(): that makes its own loop, and notify_refused needs this one.
            self.loop.run_until_complete(main())
        except Exception as e:  # noqa: BLE001
            hint = ""
            if "Improper token" in str(e):
                hint = (" — use the token from the developer portal's Bot page (~70 chars, two dots), "
                        "not the OAuth2 Client Secret; a Reset Token invalidates the old one")
            log.error("admin_bot stopped: %s%s", e, hint)

    # -- called from the monitor thread ---------------------------------------
    def notify_refused(self, name: str, pid: str, ts: Optional[int] = None) -> None:
        now = time.time()
        if now - self.last_notice.get(pid, 0) < self.cooldown:
            log.info("admin_bot: %s (%s) refused again; notice already posted recently", name, pid)
            return
        if not (self.loop and self.client and self.ready.is_set()):
            log.warning("admin_bot: not connected; dropped join notice for %s (%s)", name, pid)
            return
        self.last_notice = {k: t for k, t in self.last_notice.items() if now - t < self.cooldown}
        self.last_notice[pid] = now
        fut = asyncio.run_coroutine_threadsafe(self._post_refused(name, pid), self.loop)
        fut.add_done_callback(lambda f: f.exception() and log.warning("admin_bot post failed: %s", f.exception()))

    def attach(self, live=None, backups=None, updater=None, announce=None, db_path=None,
               post_embed=None, webhook_url: str = "", set_webhook=None) -> None:
        """Hand the bot the monitor's live state, backup copier, updater link, ways to post
        to the public channel, and the stats database (community features)."""
        self.live, self.backups, self.updater, self.announce = live, backups, updater, announce
        self.db_path = db_path
        self.post_embed = post_embed
        self.webhook_url = webhook_url or ""
        self.set_webhook = set_webhook       # hands the monitor a webhook the bot created
        self._attached = True
        # start() waits for the connection, so on_ready usually ran before this: start the
        # tasks that need the database now.
        if self.loop and self.ready.is_set():
            self.loop.call_soon_threadsafe(self._start_tasks)

    def _start_tasks(self) -> None:
        """Start the background loops that aren't running yet. Called on every on_ready
        (it repeats after reconnects) and once attach() has handed over the database."""
        if self.status and self._status_task is None and not self.stats_take_status:
            self._status_task = asyncio.ensure_future(self._status_loop())
        if self.board_channel and self._board_task is None:
            self._board_task = asyncio.ensure_future(self._board_loop())
        if not self._attached:
            return                            # the rest need the database from attach()
        if self.db_path and self._plan_task is None:
            self._plan_task = asyncio.ensure_future(self._plan_loop())
        if self.titles_on and self._titles_task is None:
            if not (self.db_path and self.guild_id):
                if not self._titles_warned:
                    self._titles_warned = True
                    log.warning("admin_bot: titles need %s; titles off",
                                " and ".join(n for n, ok in (("guild_id", self.guild_id),
                                                             ("the stats database (database.path)", self.db_path))
                                             if not ok))
            else:
                self._titles_task = asyncio.ensure_future(self._titles_loop())
        if not self._webhook_checked:
            self._webhook_checked = True
            asyncio.ensure_future(self._check_webhook())
        if (self.online_role or self.online_role_name) and self.db_path and self.guild_id \
                and self._role_task is None:
            self._role_task = asyncio.ensure_future(self._online_role_loop())
        if self.stats_on and self._stats_task is None:
            import stat_channels
            show = self._stats_show_cfg
            if show is None:                 # everything that makes sense with this config
                show = [k for k in stat_channels.STATS
                        if not (k.startswith("title_") and k != "title_owner" and not self.titles_on)]
            unknown = [k for k in show if k not in stat_channels.STATS and k not in stat_channels.ALIASES]
            if unknown:
                log.warning("admin_bot: unknown stat_channels.show entries %s; known: %s",
                            unknown, ", ".join(stat_channels.STATS))
            self.stats_show = stat_channels.expand(show)
            if not self.guild_id:
                log.warning("admin_bot: stat_channels need guild_id; stat channels off")
            else:
                if not self.db_path:
                    dropped = [k for k in self.stats_show if k in stat_channels.NEEDS_DB]
                    if dropped:
                        log.warning("admin_bot: stat channels %s need the stats database; skipped", dropped)
                    self.stats_show = [k for k in self.stats_show if k not in stat_channels.NEEDS_DB]
                self._stats_task = asyncio.ensure_future(self._stats_loop())
        if self.owner_role_name and self._owner_task is None:
            if not (self.db_path and self.guild_id):
                log.warning("admin_bot: owner_role needs guild_id and the stats database; owner role off")
            else:
                self._owner_task = asyncio.ensure_future(self._owner_loop())

    @property
    def db(self):
        """The bot thread's own connection to the stats database, or None without one."""
        if self._db is None and self.db_path:
            import stats_db
            self._db = stats_db.connect(self.db_path)
        return self._db

    # -- called from the monitor thread: roles and DMs --------------------------
    def on_login(self, player: str, server_was_empty: bool) -> None:
        if self.loop and self.ready.is_set() and self.db_path:
            asyncio.run_coroutine_threadsafe(self._on_login(player, server_was_empty), self.loop)

    def on_logout(self, player: str) -> None:
        if self.loop and self.ready.is_set() and self.db_path and (self.online_role or self.online_role_name):
            asyncio.run_coroutine_threadsafe(self._set_role(player, False), self.loop)

    async def _on_login(self, player: str, server_was_empty: bool) -> None:
        import community
        try:
            if self.online_role or self.online_role_name:
                await self._set_role(player, True)
            now = time.time()
            for uid, reason in community.who_to_notify(self.db, player, server_was_empty).items():
                if now - self._dm_sent.get((uid, reason, player), 0) < 1800:
                    continue
                self._dm_sent[(uid, reason, player)] = now
                text = (f"🟢 **{player}** just joined **{self.server_name}**; the server was empty until now."
                        if reason == "first" else f"🟢 **{player}** just joined **{self.server_name}**.")
                await self._dm(uid, text + " Stop these with `/valheim notify off`.")
        except Exception as e:  # noqa: BLE001
            log.warning("admin_bot: login follow-ups failed: %s", e)

    async def _dm(self, user_id, text: str) -> bool:
        try:
            user = self.client.get_user(int(user_id)) or await self.client.fetch_user(int(user_id))
            await user.send(text[:2000])
            return True
        except Exception as e:  # noqa: BLE001  (DMs closed, user left, …)
            log.info("admin_bot: couldn't DM %s: %s", user_id, e)
            return False

    async def _set_role(self, player: str, on: bool) -> None:
        """Give or take the "In Valheim" role from the Discord user linked to a character."""
        import community
        uid = community.linked_user(self.db, player)
        if uid and self.guild_id:
            await self._online_role_member(uid, on, f"Playing Valheim as {player}" if on else f"Left Valheim ({player})")

    def _role_holders(self) -> set:
        """Discord users the bot has given the "In Valheim" role to (kept in the database,
        because listing a role's members needs a privileged intent)."""
        try:
            return set(json.loads(self._meta("online_role:holders") or "[]"))
        except ValueError:
            return set()

    async def _online_role_member(self, uid, on: bool, reason: str) -> bool:
        import discord
        try:
            guild = self.client.get_guild(self.guild_id) or await self.client.fetch_guild(self.guild_id)
            if self.online_role:
                role = guild.get_role(self.online_role) or discord.Object(id=self.online_role)
            else:
                role = await self._ensure_role(guild, "online_role", None, self.online_role_name, 0x57F287,
                                               hoist=True, reason="Shows who's playing Valheim right now")
            holders = self._role_holders()
            try:
                member = await guild.fetch_member(int(uid))
                await (member.add_roles if on else member.remove_roles)(role, reason=reason)
            except discord.NotFound:                 # left the Discord server
                pass
            (holders.add if on else holders.discard)(str(uid))
            self._meta("online_role:holders", json.dumps(sorted(holders)))
            return True
        except discord.Forbidden:
            log.warning("admin_bot: can't change the In-Valheim role: the bot needs Manage Roles, and its own "
                        "role must be above that role in Server Settings -> Roles")
        except discord.HTTPException as e:
            log.info("admin_bot: role change for %s failed: %s", uid, e)
        return False

    async def _sync_online_role(self) -> None:
        """Make the "In Valheim" role match who's online: take it from anyone the bot gave
        it to who isn't in the game (a missed logout, a failed removal, the bot restarted
        while they left), and give it to linked players who are."""
        import community
        if not self.live:
            return
        snap = self.live.snapshot()
        if not snap.get("known") and not snap.get("down"):
            return                                   # just started: don't guess
        online = {n for n, _ in snap.get("online") or []}
        want = {str(u) for u in (community.linked_user(self.db, n) for n in online) if u}
        if self._meta("online_role:holders") is None:
            # First run with this version: anyone linked may still have the role from before.
            have = {str(r[0]) for r in self.db.execute("SELECT DISTINCT user_id FROM discord_links")}
            self._meta("online_role:holders", json.dumps(sorted(have)))
        else:
            have = self._role_holders()
        for uid in have - want:
            await self._online_role_member(uid, False, "Not in Valheim any more")
        for uid in want - have:
            await self._online_role_member(uid, True, "Playing Valheim")

    async def _online_role_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await self._sync_online_role()
            except Exception as e:  # noqa: BLE001
                log.warning("admin_bot: In-Valheim role check failed: %s", e)
            await asyncio.sleep(300)

    def post_admin(self, text: str) -> None:
        """Thread-safe: a plain message to the admin channel."""
        if not (self.loop and self.client and self.ready.is_set()):
            return

        async def send():
            ch = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)
            await ch.send(text[:2000])
        asyncio.run_coroutine_threadsafe(send(), self.loop)

    def set_status(self, offline: bool, count: Optional[int]) -> None:
        """Thread-safe: record the server's state; the bot renames the channel when allowed."""
        if self.status:
            self.status.set_state(offline, count)

    # -- discord side ----------------------------------------------------------
    def _is_admin(self, user) -> bool:
        if user.id in self.admin_users:
            return True
        return bool(self.admin_roles & {r.id for r in getattr(user, "roles", [])})

    def _buttons(self, pid: str, disabled: bool = False):
        import discord
        view = discord.ui.View(timeout=None)
        for action, label, style in (("permit", "Permit", discord.ButtonStyle.success),
                                     ("ban", "Ban", discord.ButtonStyle.danger),
                                     ("ignore", "Ignore", discord.ButtonStyle.secondary)):
            view.add_item(discord.ui.Button(label=label, style=style, disabled=disabled,
                                            custom_id=f"{BTN_PREFIX}:{action}:{pid}"[:100]))
        return view

    async def _status_loop(self) -> None:
        import discord
        st = self.status
        try:
            channel = self.client.get_channel(st.channel_id) or await self.client.fetch_channel(st.channel_id)
            st.current = channel.name
        except discord.HTTPException as e:
            log.warning("admin_bot: can't see status channel %s (%s); status disabled", st.channel_id, e)
            return
        if channel.type in (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum):
            # A text channel would get renamed to "🟢-valheim-3-online". The status *board*
            # (status_board) is the text-channel feature.
            log.warning("admin_bot: status_channel %s (#%s) is a text channel. It must be a voice channel; "
                        "for a status message in a text channel, use status_board. Status channel off.",
                        st.channel_id, channel.name)
            return
        log.info("admin_bot: status channel is '%s'; it's renamed when the server's state changes, "
                 "at most every %.0f min", channel.name, st.min_interval / 60)
        told = None
        while True:
            await asyncio.sleep(5)
            name = st.due()
            if not name:
                wait = st.waiting()
                if wait and wait[0] != told:
                    told = wait[0]
                    log.info("admin_bot: status channel: renaming to '%s' at %s (Discord allows 2 renames "
                             "per 10 minutes)", wait[0], time.strftime("%H:%M:%S", time.localtime(wait[1])))
                continue
            try:
                await channel.edit(name=name, reason="Valheim server status")
                st.renamed(name)
                log.info("admin_bot: status channel -> %s", name)
            except discord.Forbidden:
                log.warning("admin_bot: no permission to rename the status channel "
                            "(give the bot Manage Channels on it); retrying in 10 min")
                st.last_rename = st.clock() + 600 - st.min_interval
            except discord.HTTPException as e:
                log.warning("admin_bot: status channel rename failed: %s", e)
                st.last_rename = st.clock()

    async def _board_loop(self) -> None:
        """Keep one message in the board channel showing the server's state. Its id is
        remembered in `board_state`, so a restart edits the same message, not a new one."""
        import discord
        import extras
        try:
            channel = self.client.get_channel(self.board_channel) or await self.client.fetch_channel(self.board_channel)
        except discord.HTTPException as e:
            log.warning("admin_bot: can't see status board channel %s (%s); board off", self.board_channel, e)
            return
        msg = None
        try:
            with open(self.board_state) as f:
                msg = await channel.fetch_message(int(json.load(f)["message_id"]))
        except (OSError, ValueError, KeyError, discord.HTTPException):
            msg = None
        if msg is not None:
            log.info("admin_bot: status board: updating its message in #%s", getattr(channel, "name", channel))
        last = None
        while True:
            if self.live is not None:
                board = extras.render_board(self.live.snapshot(), self.server_name)
                sig = json.dumps(board, sort_keys=True)
                if sig != last:
                    embed = discord.Embed.from_dict({**board, "footer": {"text": "Live · refreshes on its own"}})
                    embed.timestamp = discord.utils.utcnow()
                    try:
                        if msg is None:
                            msg = await channel.send(embed=embed)
                            log.info("admin_bot: status board posted in #%s", getattr(channel, "name", channel))
                            with open(self.board_state, "w") as f:
                                json.dump({"channel_id": self.board_channel, "message_id": msg.id}, f)
                        else:
                            await msg.edit(embed=embed)
                        last = sig
                    except discord.NotFound:          # someone deleted it: post a new one
                        msg = None
                        continue
                    except discord.Forbidden:
                        log.warning("admin_bot: no permission to post in the status board channel "
                                    "(needs View Channel, Send Messages, Embed Links, Read Message History)")
                        await asyncio.sleep(600)
                    except discord.HTTPException as e:
                        log.warning("admin_bot: status board update failed: %s", e)
            await asyncio.sleep(10)

    def join_embed(self) -> dict:
        """The /valheim join answer: everything a new player needs to connect."""
        snap = self.live.snapshot() if self.live else None
        if snap is None or not snap["known"]:
            status = "⚪ Status unknown right now."
        elif snap["down"]:
            status = "🔴 The server is **offline** right now; try again in a few minutes."
        elif snap["count"]:
            status = f"🟢 Online, **{snap['count']}** playing."
        else:
            status = "🟢 Online, nobody playing yet."
        fields = []
        code = snap.get("join_code") if snap else None
        fields.append({"name": "Join code (any platform, crossplay)",
                       "value": f"**`{code}`**\nIt changes whenever the server restarts; run `/valheim join` again then."
                       if code else "Not known yet. It shows up here once someone joins after the server's "
                                    "last restart. Until then, ask someone who's playing (it's in the pause menu), "
                                    "or use the address.",
                       "inline": False})
        address = str(self.join.get("address") or "")
        if address.upper().startswith("YOUR"):     # the example config's placeholder
            address = ""
        address = address or (snap.get("server_ip") if snap else None)
        if address:
            fields.append({"name": "Address (PC / Steam)", "value": f"**`{address}`**", "inline": True})
        if self.join.get("password"):
            fields.append({"name": "Password", "value": f"||`{self.join['password']}`||", "inline": True})
        fields.append({"name": "How to join",
                       "value": "Start Valheim → pick a character → **Join Game** → **Add server**, "
                                "and enter the join code (or the address on PC). Anyone already in the "
                                "game can also read the current code from the pause menu.", "inline": False})
        try:
            permitted = bool(self.lists.ids("permitted"))
        except OSError:
            permitted = False
        if permitted:
            fields.append({"name": "First time?",
                           "value": "This server only lets in players on its list. If you're turned away, "
                                    "tell an admin: they get a notice with a button to let you in.",
                           "inline": False})
        if self.join.get("note"):
            fields.append({"name": "Note", "value": str(self.join["note"])[:1000], "inline": False})
        return {"title": f"⚔️ Join {self.server_name}", "description": status, "color": 0x5865F2,
                "fields": fields}

    def start_restart(self, minutes: int, reason: str, by: str) -> str:
        """Start a restart countdown (command or button). Returns the reply for the admin."""
        minutes = max(0, min(int(minutes), 60))
        if self._countdown and not self._countdown.done():
            return "A restart is already counting down. `/valheim restart-cancel` stops it."
        if self.updater is None:
            return ("Restarting needs the updater link (`updater` in config.json and the host helper, "
                    "see host/README.md).")
        if self._online_count() == 0:
            minutes = 0
        self._countdown_cancel = asyncio.Event()
        self._countdown = asyncio.ensure_future(self._restart_countdown(minutes, reason.strip()[:100], by))
        return ("Restarting now (nobody is online)." if minutes == 0 else
                f"Restart in {minutes} min, or sooner if everyone leaves. `/valheim restart-cancel` stops it.")

    async def _change_setting(self, it, kind: str, key: str, value: str = "") -> None:
        """Validate, ask the host to write world-settings.env, and confirm once it has."""
        import discord
        import world_settings as ws
        before = ws.read_file(self.world_file)
        try:
            wanted = ws.apply(before, kind, key, value)
        except ValueError as e:
            await it.response.send_message(f"Not changed: {e}", ephemeral=True)
            return
        label = f"{kind} {key}" + (f" → {value}" if value else "")
        if wanted == before:
            await it.response.send_message(f"Already set: {label}. Nothing to change.", ephemeral=True)
            return
        err = await self._request(f"set {kind} {key} {value}".strip())
        if err:
            await it.response.send_message(f"Couldn't send the change: {err}", ephemeral=True)
            return
        await it.response.defer(ephemeral=True, thinking=True)
        for _ in range(20):                           # the host helper normally takes a second
            await asyncio.sleep(1)
            if ws.read_file(self.world_file) == wanted:
                log.info("admin_bot: world setting %s by %s", label, it.user)
                view = discord.ui.View(timeout=None)
                view.add_item(discord.ui.Button(label="Restart in 5 min (with warning)",
                                                style=discord.ButtonStyle.primary,
                                                custom_id=f"{BTN_PREFIX}:restart:5"))
                await it.followup.send(
                    f"✅ Saved: **{label}**. It takes effect at the next server restart.\n"
                    f"Now: `{ws.format_args(wanted) or '(all normal)'}`", view=view, ephemeral=True)
                return
        await it.followup.send(
            "⚠️ The change was sent, but the settings file didn't change within 20 s. Check that the host "
            "helper is updated (host/README.md, world settings) and that `/valheim_home` is mounted.",
            ephemeral=True)

    async def _public(self, text: str) -> None:
        if self.announce:
            await asyncio.get_running_loop().run_in_executor(None, self.announce, text)

    def _online_count(self) -> int:
        return self.live.snapshot()["count"] if self.live else 0

    async def _request(self, action: str) -> Optional[str]:
        """Queue a host request, and tell the admins if nothing picks it up."""
        err = self.updater.request(action) if self.updater else "the updater link isn't set up (`updater` in config.json)"
        if err:
            return err

        async def watch():
            await asyncio.sleep(30)
            if self.updater.pending():
                self.post_admin(f"⚠️ The `{action}` request wasn't picked up after 30 s. Is the host helper "
                                "installed and running? (`systemctl status valheim-bot-request.path`)")
        asyncio.ensure_future(watch())
        return None

    async def _restart_countdown(self, minutes: int, reason: str, by: str) -> None:
        """Warn the public channel, restart when the time is up or everyone has left."""
        why = f" ({reason})" if reason else ""
        loop = asyncio.get_running_loop()
        end = loop.time() + minutes * 60
        warnings = sorted({m for m in (minutes, 5, 1) if 0 < m <= minutes}, reverse=True)
        if minutes:
            await self._public(f"⚠️ **{self.server_name}** restarts in **{minutes} minute{'s' if minutes != 1 else ''}**"
                               f"{why}. Find a safe spot and log out; the world is saved on shutdown.")
        warnings = [m for m in warnings if m < minutes]
        while True:
            if self._countdown_cancel.is_set():
                await self._public(f"✅ The restart of **{self.server_name}** was cancelled.")
                return
            left = end - loop.time()
            if left <= 0 or self._online_count() == 0:
                break
            if warnings and left <= warnings[0] * 60:
                m = warnings.pop(0)
                await self._public(f"⚠️ **{self.server_name}** restarts in **{m} minute{'s' if m != 1 else ''}**{why}.")
            await asyncio.sleep(5)
        err = await self._request("restart")
        if err:
            self.post_admin(f"⚠️ Restart failed: {err}")
            await self._public(f"The restart of **{self.server_name}** didn't happen; the admins have been told.")
            return
        log.info("admin_bot: restart requested by %s%s", by, why)
        await self._public(f"🔄 **{self.server_name}** is restarting now{why}. "
                           "It installs any waiting Valheim update and is back in a few minutes.")

    async def _post_refused(self, name: str, pid: str) -> None:
        import discord
        channel = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)
        try:
            reason = self.lists.refusal_reason(pid)
        except OSError:
            reason = "refused by the server"
        embed = discord.Embed(title="🚫 Join attempt refused", color=0xE67E22,
                              description=f"**{discord.utils.escape_markdown(name)}** tried to join "
                                          f"**{self.server_name}** — {reason}.")
        link = steam_profile(pid)
        embed.add_field(name="Platform ID", value=f"`{pid}`" + (f"\n[Steam profile]({link})" if link else ""))
        self._refused[pid] = name
        if self.db_path:
            import community
            asked = community.access_request(self.db, name)
            if asked:
                embed.add_field(name="Requested by", value=f"<@{asked}> (`/valheim request-access`). "
                                "Permit links the character to them and lets them know.", inline=False)
        who = {"characters": [], "users": []}
        if self.db_path:
            who = community.who_is(self.db, pid, name)
            known = [u for u in who["users"] if u != asked]
            others = [c for c in who["characters"] if c.lower() != name.lower()]
            if known or others:
                lines = []
                if known:
                    lines.append("Discord: " + ", ".join(f"<@{u}>" for u in known) + " (linked character)")
                if others:
                    lines.append("Played before as: " + ", ".join(f"**{discord.utils.escape_markdown(c)}**"
                                                                  for c in others[:5]))
                embed.add_field(name="Who this is", value="\n".join(lines)[:1024], inline=False)
        # Tell the player why, unless they're banned (no need to explain that to them).
        if who["users"] and "ban" not in reason:
            why = f"you're {reason}" if reason.startswith("not ") else "the server refused it"
            if await self._dm_once(who["users"][0], "refused", (
                    f"🚪 Your join to **{self.server_name}** as **{name}** was refused: {why}. "
                    "The admins have been told and can let you in from Discord. You'll get a DM when they "
                    "do if you've used `/valheim request-access`.")):
                embed.set_footer(text="The player was told why by DM.")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed, view=self._buttons(pid),
                           allowed_mentions=discord.AllowedMentions.none())

    async def _dm_once(self, uid, reason: str, text: str, every: float = 1800) -> bool:
        """A DM at most once per `every` seconds for the same user and reason."""
        now = time.time()
        if now - self._dm_sent.get((str(uid), reason), 0) < every:
            return False
        self._dm_sent[(str(uid), reason)] = now
        return await self._dm(uid, text)

    def notify_version(self, extra: dict) -> None:
        """Thread-safe: someone tried to join with another game version. If the bot knows
        who (their platform id matches a linked or requested character), DM them."""
        if self.loop and self.ready.is_set() and self.db_path:
            asyncio.run_coroutine_threadsafe(self._version_dm(extra), self.loop)

    async def _version_dm(self, extra: dict) -> None:
        import community
        try:
            who = community.who_is(self.db, extra.get("platform_id"))
            if not who["users"]:
                return
            if extra.get("newer"):
                advice = "Your game is newer than the server's. The server will update soon; try again later."
            else:
                advice = ("Your game is older than the server's. Update Valheim (on PC restart Steam; on "
                          "Xbox, PlayStation or Switch check for updates), then join again.")
            await self._dm_once(who["users"][0], "version", f"⚠️ Couldn't join **{self.server_name}**: "
                                f"game version mismatch (yours: {extra.get('their')}, server: "
                                f"{extra.get('mine')}). {advice}")
        except Exception as e:  # noqa: BLE001
            log.warning("admin_bot: version-mismatch DM failed: %s", e)

    async def _welcome_requester(self, name: Optional[str]) -> str:
        """After a Permit: link the character to whoever asked for access as it, and DM them."""
        if not (name and self.db_path):
            return ""
        import community
        uid = community.access_request(self.db, name)
        if not uid:
            return ""
        community.link_player(self.db, name, uid, force=True)
        community.clear_access_request(self.db, name)
        sent = await self._dm(uid, f"✅ You've been let into **{self.server_name}** as **{name}**. "
                                   "Try joining again now; `/valheim join` has the join code.")
        return f"; linked to <@{uid}>" + (" and told by DM" if sent else " (their DMs are closed)")

    async def _on_rsvp(self, it, rest: str) -> None:
        import community
        plan_id, _, choice = rest.partition(":")
        if not (self.db_path and plan_id.isdigit() and choice in ("going", "maybe", "no")):
            await it.response.send_message("That signup isn't available any more.", ephemeral=True)
            return
        community.rsvp(self.db, int(plan_id), it.user.id, choice)
        plan = community.get_plan(self.db, int(plan_id))
        if not plan:
            await it.response.send_message("That plan was removed.", ephemeral=True)
            return
        import discord
        await it.response.edit_message(embed=discord.Embed.from_dict(community.render_plan(plan, self.server_name)),
                                       view=self._plan_buttons(int(plan_id)))

    def _plan_buttons(self, plan_id: int):
        import discord
        view = discord.ui.View(timeout=None)
        for choice, label, style in (("going", "✅ Going", discord.ButtonStyle.success),
                                     ("maybe", "❔ Maybe", discord.ButtonStyle.secondary),
                                     ("no", "❌ Can't", discord.ButtonStyle.secondary)):
            view.add_item(discord.ui.Button(label=label, style=style, custom_id=f"{BTN_PREFIX}:rsvp:{plan_id}:{choice}"))
        return view

    async def _post_plan(self, channel, title: str, at, creator_id, guild=None):
        """Post a game-night signup, open a thread on it for the planning chat, and create a
        Discord Event if that's turned on. Returns the signup message."""
        import datetime as _dt
        import discord
        import community
        plan_id = community.create_plan(self.db, title, int(at.timestamp()), channel.id, creator_id)
        community.rsvp(self.db, plan_id, creator_id, "going")
        p = community.get_plan(self.db, plan_id)
        msg = await channel.send(embed=discord.Embed.from_dict(community.render_plan(p, self.server_name)),
                                 view=self._plan_buttons(plan_id))
        community.set_plan_message(self.db, plan_id, msg.id)
        try:                                     # the thread shares the message's id
            thread = await msg.create_thread(name=f"🗺️ {title}"[:100], auto_archive_duration=4320)
            await thread.send(f"Plan **{title}** here: who brings what, where to meet. The reminder "
                              f"<t:{p['at']}:R> is posted here too.")
        except discord.HTTPException as e:
            log.info("admin_bot: couldn't open a thread for the game night (needs Create Public Threads): %s", e)
        if self.discord_events and guild:
            try:
                await guild.create_scheduled_event(
                    name=p["title"], start_time=at, end_time=at + _dt.timedelta(hours=2),
                    entity_type=discord.EntityType.external, location=f"Valheim: {self.server_name}",
                    privacy_level=discord.PrivacyLevel.guild_only, description=f"Signup: {msg.jump_url}")
            except discord.HTTPException as e:
                log.info("admin_bot: couldn't create the Discord event (needs Manage Events): %s", e)
        return msg

    def _pending_polls(self) -> list:
        try:
            return json.loads(self._meta("polls:pending") or "[]")
        except ValueError:
            return []

    async def _post_time_poll(self, channel, title: str, times: list, creator_id) -> None:
        """A Discord poll to pick a game night's time. It closes an hour before the earliest
        option (at most a week); then the winner becomes a signup (_resolve_polls)."""
        import datetime as _dt
        import discord
        hours = int((times[0] - _dt.datetime.now().astimezone()).total_seconds() // 3600) - 1
        hours = max(1, min(hours, 168))
        poll = discord.Poll(question=f"When should we do {title}?"[:300], duration=_dt.timedelta(hours=hours))
        for at in times:
            poll.add_answer(text=at.strftime("%a %d %b, %H:%M %Z").strip()[:55])
        msg = await channel.send(content=f"🗳️ Vote for a time for **{title}**. The winner becomes a "
                                         "game-night signup when the poll closes.", poll=poll)
        pending = self._pending_polls()
        pending.append({"message_id": msg.id, "channel_id": channel.id, "title": title,
                        "times": [int(t.timestamp()) for t in times], "creator": str(creator_id),
                        "ends_at": int(time.time()) + hours * 3600})
        self._meta("polls:pending", json.dumps(pending))

    async def _resolve_polls(self) -> None:
        """Turn closed time polls into signups."""
        import datetime as _dt
        import discord
        import community
        pending, keep = self._pending_polls(), []
        for rec in pending:
            if time.time() < rec["ends_at"] + 60:
                keep.append(rec)
                continue
            try:
                channel = self.client.get_channel(int(rec["channel_id"])) or \
                    await self.client.fetch_channel(int(rec["channel_id"]))
                msg = await channel.fetch_message(int(rec["message_id"]))
            except discord.NotFound:
                continue                          # deleted: drop it
            except discord.HTTPException:
                keep.append(rec)                  # try again next time
                continue
            answers = list(msg.poll.answers) if msg.poll else []
            winner = community.poll_winner([(a.vote_count, t) for a, t in zip(answers, rec["times"])])
            if winner is None:
                await channel.send(f"🗳️ Nobody voted on a time for **{rec['title']}**, so nothing was planned.")
                continue
            at = _dt.datetime.fromtimestamp(winner).astimezone()
            await channel.send(f"🗳️ The vote picked <t:{winner}:F> for **{rec['title']}**:")
            await self._post_plan(channel, rec["title"], at, int(rec["creator"]), getattr(channel, "guild", None))
        if len(keep) != len(pending):
            self._meta("polls:pending", json.dumps(keep))

    async def _plan_loop(self) -> None:
        """Ping the people signed up for a game night shortly before it starts, turn
        closed time polls into signups, and tidy handled admin notices."""
        import discord
        import community
        while True:
            await asyncio.sleep(30)
            for job in (self._resolve_polls, self._tidy_notices):
                try:
                    await job()
                except Exception as e:  # noqa: BLE001
                    log.warning("admin_bot: %s failed: %s", job.__name__, e)
            try:
                for plan in community.due_reminders(self.db, time.time(), self.remind_minutes * 60):
                    community.mark_reminded(self.db, plan["id"])
                    people = plan["rsvps"]["going"] + plan["rsvps"]["maybe"]
                    text = (f"⏰ **{plan['title']}** starts <t:{plan['at']}:R>! "
                            + (" ".join(f"<@{u}>" for u in people) if people else "Nobody has signed up yet.")
                            + " `/valheim join` has the join code.")
                    pings = discord.AllowedMentions(users=[discord.Object(id=int(u)) for u in people],
                                                    everyone=False, roles=False)
                    sent = False
                    if plan.get("message_id"):       # the plan's thread (same id as the signup)
                        try:
                            thread = self.client.get_channel(int(plan["message_id"])) or \
                                await self.client.fetch_channel(int(plan["message_id"]))
                            await thread.send(text, allowed_mentions=pings)
                            sent = True
                        except discord.HTTPException:
                            pass
                    if not sent:
                        channel = self.client.get_channel(int(plan["channel_id"])) or \
                            await self.client.fetch_channel(int(plan["channel_id"]))
                        await channel.send(text, allowed_mentions=pings)
            except Exception as e:  # noqa: BLE001
                log.warning("admin_bot: plan reminders failed: %s", e)

    # -- title roles -------------------------------------------------------------
    def _titles_since(self) -> int:
        """Log timestamp the titles count from: 0 for all time, or 7 days ago."""
        import community
        if self.titles_period != "week":
            return 0
        return int(time.time()) - community.log_clock_offset(self.db) - 7 * 86400

    def _titles_due(self, now: Optional[_dt.datetime] = None) -> Optional[str]:
        """The ISO week to reassign for, or None. The first run is immediate."""
        import community
        now = now or _dt.datetime.now().astimezone()
        y, w, _ = now.isocalendar()
        key = f"{y}-W{w:02d}"
        last = community.get_meta(self.db, "titles_week")
        if last is None:
            return key
        if now.weekday() != self.titles_weekday or now.hour < self.titles_hour:
            return None
        return None if last == key else key

    async def _ensure_role(self, guild, key: str, configured, name: str, colour: int,
                           hoist: bool = False, reason: str = ""):
        """A role the bot manages: the configured ID, the one it remembered (by ID, so it can
        be renamed), an existing role with that name, or a new one it creates."""
        import discord
        import community
        rid = configured or community.get_meta(self.db, f"role:{key}")
        role = guild.get_role(int(rid)) if rid else None
        if role is None:
            role = _assignable_role(guild, name)
        if role is None:
            role = await guild.create_role(name=name, colour=discord.Colour(colour), hoist=hoist, reason=reason)
            log.info("admin_bot: created the %s role", name)
        if str(role.id) != str(rid):
            community.set_meta(self.db, f"role:{key}", role.id)
        return role

    async def _sync_owner_role(self) -> None:
        """Give the owner role to whoever owns the Discord server, and take it from a
        previous owner. A new role is moved up to just below the bot's own role, so its
        holder is listed near the top of the member list."""
        import discord
        import community
        guild = self.client.get_guild(self.guild_id) or await self.client.fetch_guild(self.guild_id)
        created = community.get_meta(self.db, "role:owner") is None and \
            _assignable_role(guild, self.owner_role_name) is None
        role = await self._ensure_role(guild, "owner", None, self.owner_role_name, 0x206694, hoist=True,
                                       reason="The Discord server's owner")
        if created and getattr(guild, "me", None) is not None:
            try:
                await role.edit(position=max(1, guild.me.top_role.position - 1))
            except discord.HTTPException as e:
                log.info("admin_bot: couldn't move the %s role up: %s", role.name, e)
        owner, held = str(guild.owner_id), community.get_meta(self.db, "role_holder:owner")
        if owner != held:
            await self._move_role(guild, role, held, owner, f"{role.name}: owner of this Discord server")
            community.set_meta(self.db, "role_holder:owner", owner)
            log.info("admin_bot: gave the %s role to the server owner", role.name)

    async def _owner_loop(self) -> None:
        """At start-up, then every 6 hours in case the server changes hands."""
        import discord
        await asyncio.sleep(10)
        while True:
            try:
                await self._sync_owner_role()
            except discord.Forbidden:
                log.warning("admin_bot: can't give the owner role: the bot needs Manage Roles, and its own "
                            "role must be above that role in Server Settings -> Roles")
            except Exception as e:  # noqa: BLE001
                log.warning("admin_bot: owner role failed: %s", e)
            await asyncio.sleep(6 * 3600)

    # -- /valheim setup (server_layout.py) ------------------------------------------
    def _feed_channel_id(self) -> Optional[int]:
        """The channel Huginn's webhook posts to: a webhook URL answers GET with its channel."""
        if not self.webhook_url.startswith("https://"):
            return None
        try:
            import urllib.request
            req = urllib.request.Request(self.webhook_url, headers={"User-Agent": "valheim-discord-monitor"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return int(json.loads(r.read().decode())["channel_id"])
        except Exception as e:  # noqa: BLE001
            log.info("admin_bot: couldn't look up the webhook's channel: %s", e)
            return None

    def _layout_remembered(self) -> dict:
        import server_layout
        out = {}
        for cat in server_layout.TEMPLATE:
            for key in [f"cat:{cat['key']}"] + [f"ch:{c[0]}" for c in cat["channels"]]:
                v = self._meta(f"layout:{key}")
                if v:
                    out[key] = int(v)
        return out

    def _stat_ids_in_use(self) -> set:
        if self.db_path:
            rows = self.db.execute("SELECT value FROM meta WHERE key LIKE 'statchan:%'").fetchall()
            vals = [r[0] for r in rows]
        else:
            vals = list(self._stat_ids.values())
        return {int(v) for v in vals if str(v).isdigit()}

    async def _layout_plan(self, guild) -> dict:
        import discord
        import server_layout
        snap = {"categories": [{"id": c.id, "name": c.name, "position": c.position} for c in guild.categories],
                "channels": []}
        for ch in guild.channels:
            kind = {discord.ChannelType.text: "text", discord.ChannelType.voice: "voice"}.get(ch.type)
            if kind:
                snap["channels"].append({"id": ch.id, "name": ch.name, "kind": kind, "position": ch.position,
                                         "category_id": ch.category_id, "topic": getattr(ch, "topic", None)})
        snap["system_channel_id"] = getattr(guild, "system_channel_id", None)
        feed = await asyncio.get_running_loop().run_in_executor(None, self._feed_channel_id)
        p = server_layout.plan(snap, known={"admin": self.channel_id, "feed": feed},
                               remembered=self._layout_remembered(), exclude=self._stat_ids_in_use())
        # Huginn's webhook: create it if there's none, move it if it posts somewhere else.
        slot = next(c for c in p["channels"] if c["key"] == "feed")
        if not self.webhook_url:
            p["webhook"] = {"action": "create"}
        elif feed and feed != slot["id"]:
            where = next((c["name"] for c in snap["channels"] if c["id"] == feed), None)
            p["webhook"] = {"action": "move", "from": where}
        return p

    def _webhook_id(self) -> Optional[int]:
        m = re.search(r"/webhooks/(\d+)/", self.webhook_url or "")
        return int(m.group(1)) if m else None

    async def _layout_webhook(self, guild, p: dict, undo: dict, problems: list) -> None:
        """Create Huginn's webhook in the feed channel, or move the existing one there."""
        import discord
        action = (p.get("webhook") or {}).get("action")
        cid = self._meta("layout:ch:feed")
        feed = guild.get_channel(int(cid)) if cid else None
        if not action or feed is None:
            return
        try:
            if action == "create":
                hook = await feed.create_webhook(name="Huginn", reason="/valheim setup")
                self.webhook_url = hook.url
                if self.set_webhook:
                    self.set_webhook(hook.url)
                log.info("admin_bot: created Huginn's webhook in #%s", feed.name)
            else:
                hook = await self.client.fetch_webhook(self._webhook_id())
                undo.setdefault("webhook_channel", hook.channel_id)
                await hook.edit(channel=feed, reason="/valheim setup")
                log.info("admin_bot: moved Huginn's webhook to #%s", feed.name)
        except discord.Forbidden:
            problems.append("Huginn's webhook: the bot needs Manage Webhooks to "
                            + ("create it" if action == "create" else "move it") +
                            ". Or do it yourself: channel settings → Integrations → Webhooks.")
        except discord.HTTPException as e:
            problems.append(f"Huginn's webhook: {e}")

    async def _check_webhook(self) -> None:
        """At start-up: warn if Huginn's webhook posts into the private admin channel,
        where nobody else sees the posts."""
        feed = await asyncio.get_running_loop().run_in_executor(None, self._feed_channel_id)
        if feed and feed == self.channel_id:
            text = ("⚠️ Huginn's webhook posts into this admin channel, so logins, deaths and the other "
                    "public posts only show up here. Run `/valheim setup apply` to move it to "
                    "#huginns-watch (the bot needs Manage Webhooks), or move it yourself: this channel's "
                    "settings → Integrations → Webhooks → Channel.")
            log.warning("admin_bot: Huginn's webhook posts into the admin channel; public posts are hidden")
            self.post_admin(text)

    async def _layout_overwrites(self, guild, flags: set, current: Optional[dict] = None) -> Optional[dict]:
        """Permission overwrites for a private (admins only) or read-only channel, added to
        whatever the channel already has. None when nothing needs to change."""
        import discord
        if not ({"private", "readonly"} & flags):
            return None
        ow = dict(current or {})
        admins = [r for r in (guild.get_role(rid) for rid in self.admin_roles) if r]
        for uid in self.admin_users:
            try:
                admins.append(await guild.fetch_member(uid))
            except discord.HTTPException:
                pass
        me = getattr(guild, "me", None)
        if "private" in flags:
            ow[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
            for who in admins + ([me] if me else []):
                ow[who] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if "readonly" in flags:
            base = ow.get(guild.default_role) or discord.PermissionOverwrite()
            base.send_messages = False
            ow[guild.default_role] = base
            for who in admins + ([me] if me else []):
                ow[who] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        return ow

    @staticmethod
    def _dump_overwrites(ow: dict) -> list:
        import discord
        def kind(t):
            return "role" if isinstance(t, discord.Role) or getattr(t, "type", None) is discord.Role else "member"
        return [[t.id, kind(t), *(p.value for p in o.pair())] for t, o in ow.items()]

    @staticmethod
    def _load_overwrites(rows: list) -> dict:
        import discord
        return {discord.Object(tid, type=discord.Role if kind == "role" else discord.Member):
                discord.PermissionOverwrite.from_pair(discord.Permissions(a), discord.Permissions(d))
                for tid, kind, a, d in rows}

    def _layout_undo_state(self) -> dict:
        try:
            return json.loads(self._meta("layout:undo") or "{}")
        except ValueError:
            return {}

    async def _layout_apply(self, guild, p: dict) -> list:
        """Create, rename and move to match the plan. Remembers each object's original
        state (the first time it's touched) for /valheim setup undo. Returns problems."""
        import discord
        undo = self._layout_undo_state()
        undo.setdefault("before", {})
        undo.setdefault("created", [])
        problems = []

        def remember(obj):
            key = str(obj.id)
            if key not in undo["before"]:
                undo["before"][key] = {"name": obj.name, "category_id": getattr(obj, "category_id", None),
                                       "position": obj.position, "topic": getattr(obj, "topic", None),
                                       "overwrites": self._dump_overwrites(obj.overwrites)}

        cats = {}
        for c in p["categories"]:
            try:
                if c["id"] is None:
                    ow = await self._layout_overwrites(guild, {"private"}) if c["private"] else {}
                    cat = await guild.create_category(c["name"], overwrites=ow or {}, reason="/valheim setup")
                    undo["created"].append(cat.id)
                else:
                    cat = guild.get_channel(c["id"])
                    changes = {}
                    if cat.name != c["name"]:
                        changes["name"] = c["name"]
                    if c["private"]:
                        changes["overwrites"] = await self._layout_overwrites(guild, {"private"}, cat.overwrites)
                    if changes:
                        remember(cat)
                        await cat.edit(**changes, reason="/valheim setup")
                cats[c["key"]] = cat
                self._meta(f"layout:cat:{c['key']}", cat.id)
            except discord.HTTPException as e:
                problems.append(f"{c['name']}: {e}")

        for ch in p["channels"]:
            cat = cats.get(ch["category"])
            if cat is None:
                continue
            try:
                if ch["id"] is None:
                    ow = await self._layout_overwrites(guild, ch["flags"], cat.overwrites) or cat.overwrites
                    make = guild.create_text_channel if ch["kind"] == "text" else guild.create_voice_channel
                    extra = {"topic": ch["topic"]} if ch["kind"] == "text" and ch["topic"] else {}
                    obj = await make(ch["name"], category=cat, overwrites=ow, reason="/valheim setup", **extra)
                    undo["created"].append(obj.id)
                else:
                    obj = guild.get_channel(ch["id"])
                    changes = {}
                    target = ch["name"] if ch["kind"] == "voice" else norm_name(ch["name"])
                    if obj.name != target:
                        changes["name"] = ch["name"]
                    if obj.category_id != cat.id:
                        changes["category"] = cat
                    if ch["kind"] == "text" and ch["topic"] and not getattr(obj, "topic", None):
                        changes["topic"] = ch["topic"]
                    ow = await self._layout_overwrites(guild, ch["flags"], obj.overwrites)
                    if ow is not None:
                        changes["overwrites"] = ow
                    if changes:
                        remember(obj)
                        await obj.edit(**changes, reason="/valheim setup")
                self._meta(f"layout:ch:{ch['key']}", obj.id)
            except discord.HTTPException as e:
                problems.append(f"{ch['name']}: {e}")

        # Order: the stat categories first (at a glance), then the template, then the rest.
        try:
            stat_ids = self._stat_ids_in_use()
            order = [c for c in sorted(guild.categories, key=lambda c: c.position) if c.id in stat_ids]
            order += [cats[c["key"]] for c in p["categories"] if c["key"] in cats]
            order += [c for c in sorted(guild.categories, key=lambda c: c.position) if c not in order]
            payload = [{"id": c.id, "position": i} for i, c in enumerate(order)]
            for key, cat in cats.items():
                slots = [c for c in p["channels"] if c["category"] == key]
                for i, slot in enumerate(slots):
                    cid = self._meta(f"layout:ch:{slot['key']}")
                    if cid:
                        payload.append({"id": int(cid), "position": i})
            await guild._state.http.bulk_channel_update(guild.id, payload, reason="/valheim setup")
        except Exception as e:  # noqa: BLE001
            problems.append(f"ordering the categories: {e}")
        await self._layout_webhook(guild, p, undo, problems)
        # Idle voice users: Discord moves them to the AFK channel by itself once it's set.
        afk = self._meta("layout:ch:vc_afk")
        if afk and getattr(guild, "afk_channel", None) is None:
            try:
                undo.setdefault("afk_channel", None)
                await guild.edit(afk_channel=guild.get_channel(int(afk)), afk_timeout=900, reason="/valheim setup")
            except discord.Forbidden:
                problems.append("The Fishing Hut isn't the AFK channel yet: the bot needs Manage Server. Set it in "
                                "Server Settings → Overview → Inactive Channel.")
            except discord.HTTPException as e:
                problems.append(f"AFK channel: {e}")
        # Discord's join greetings: into the welcome channel if they'd be hidden or go nowhere.
        welcome = self._meta("layout:ch:welcome")
        if (p.get("system") or {}).get("move") and welcome:
            try:
                undo.setdefault("system_channel", p["system"]["current"])
                await guild.edit(system_channel=guild.get_channel(int(welcome)), reason="/valheim setup")
            except discord.Forbidden:
                problems.append("Discord's join messages still go to the old channel: the bot needs Manage "
                                "Server to change that. Set it in Server Settings → Engagement → System "
                                "Messages Channel.")
            except discord.HTTPException as e:
                problems.append(f"system messages channel: {e}")
        self._meta("layout:undo", json.dumps(undo))
        return problems

    async def _layout_guide(self, guild, p: dict) -> None:
        """Post (or update) a guide to every channel in the welcome channel."""
        import discord
        import stat_channels
        cid = self._meta("layout:ch:welcome")
        channel = guild.get_channel(int(cid)) if cid else None
        if channel is None:
            return
        lines = []
        for cat in p["categories"]:
            if cat["private"]:
                continue
            lines.append(f"**{cat['name']}**")
            for ch in (c for c in p["channels"] if c["category"] == cat["key"]):
                what = ch["topic"] or {"vc_main": "Voice chat: hang out while you play.",
                                       "vc_raid": "Voice chat for raids, boss fights and game nights.",
                                       "vc_afk": "Away from keyboard."}.get(ch["key"], "")
                mention = f"<#{self._meta('layout:ch:' + ch['key'])}>" if self._meta("layout:ch:" + ch["key"]) \
                    else ch["name"]
                lines.append(f"{mention}: {what}")
            lines.append("")
        if self._stat_ids_in_use():
            lines.append("**" + " / ".join(title for _, title, _ in stat_channels.GROUPS) + "**")
            lines.append("Locked voice channels that show the server live, this week's numbers and the "
                         "title holders. Read the names; you can't join them.")
        embed = discord.Embed(title="📖 A guide to the realm", description="\n".join(lines)[:4000],
                              color=0xC27C0E)
        try:
            mid = self._meta("layout:guide")
            msg = None
            if mid:
                try:
                    msg = await channel.fetch_message(int(mid))
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    msg = None
            if msg is None:
                msg = await channel.send(embed=embed)
                self._meta("layout:guide", msg.id)
            if not getattr(msg, "pinned", False):
                try:
                    await msg.pin(reason="/valheim setup: channel guide")
                except discord.HTTPException as e:
                    log.info("admin_bot: couldn't pin the channel guide (needs Pin Messages): %s", e)
        except discord.HTTPException as e:
            log.info("admin_bot: couldn't post the channel guide: %s", e)

    async def _layout_undo(self, guild) -> tuple:
        """Put every renamed/moved channel back. Channels the setup created are left (and
        listed), because deleting could lose messages."""
        import discord
        undo = self._layout_undo_state()
        restored, problems = 0, []
        positions = []
        for key, old in (undo.get("before") or {}).items():
            obj = guild.get_channel(int(key))
            if obj is None:
                continue
            try:
                changes = {"name": old["name"], "overwrites": self._load_overwrites(old["overwrites"])}
                if obj.type != discord.ChannelType.category:
                    cat = guild.get_channel(old["category_id"]) if old.get("category_id") else None
                    changes["category"] = cat
                if obj.type == discord.ChannelType.text:
                    changes["topic"] = old.get("topic")
                await obj.edit(**changes, reason="/valheim setup undo")
                positions.append({"id": obj.id, "position": old["position"]})
                restored += 1
            except discord.HTTPException as e:
                problems.append(f"{obj.name}: {e}")
        if positions:
            try:
                await guild._state.http.bulk_channel_update(guild.id, positions, reason="/valheim setup undo")
            except Exception as e:  # noqa: BLE001
                problems.append(f"positions: {e}")
        if undo.get("webhook_channel") and self._webhook_id():
            try:
                hook = await self.client.fetch_webhook(self._webhook_id())
                await hook.edit(channel=guild.get_channel(int(undo["webhook_channel"])),
                                reason="/valheim setup undo")
            except (discord.HTTPException, AttributeError, TypeError) as e:
                problems.append(f"Huginn's webhook: {e}")
        if "afk_channel" in undo:
            try:
                old = undo["afk_channel"]
                await guild.edit(afk_channel=guild.get_channel(int(old)) if old else None,
                                 reason="/valheim setup undo")
            except discord.HTTPException as e:
                problems.append(f"AFK channel: {e}")
        if "system_channel" in undo:
            try:
                old = undo["system_channel"]
                await guild.edit(system_channel=guild.get_channel(int(old)) if old else None,
                                 reason="/valheim setup undo")
            except discord.HTTPException as e:
                problems.append(f"system messages channel: {e}")
        created = [guild.get_channel(i) for i in undo.get("created", [])]
        created = [c for c in created if c is not None]
        self._meta("layout:undo", "")
        for key in list(self._layout_remembered()):
            self._meta(f"layout:{key}", "")
        return restored, created, problems

    # -- stat channels -----------------------------------------------------------
    def _stat_name(self, key: str) -> Optional[str]:
        import community
        import stat_channels
        db = self.db if self.db_path else None
        titles = {"title_owner": (self.owner_role_name or "Odin", self._owner_name)}
        if db:
            titles.update({f"title_{cat}": (name, (community.title_holder(db, cat) or {}).get("player"))
                           for cat, (name, _, _) in community.TITLES.items()})
        name = stat_channels.name_for(
            key, self.live.snapshot() if self.live else {}, db,
            offset=community.log_clock_offset(db) if db else 0,
            update_waiting=bool(self.updater and self.updater.new), titles=titles)
        return name[:100] if name else None

    def _meta(self, key: str, value=None):
        import community
        if not self.db_path:
            return self._stat_ids.get(key) if value is None else self._stat_ids.__setitem__(key, str(value))
        if value is None:
            return community.get_meta(self.db, key)
        community.set_meta(self.db, key, value)

    async def _ensure_stat_channels(self) -> dict:
        """Find or create the categories and one locked voice channel per stat, in order.
        They're remembered by ID, so they can be renamed or moved in Discord. An existing
        status_channel becomes the players channel and is moved in and locked."""
        import discord
        import stat_channels
        guild = self.client.get_guild(self.guild_id) or await self.client.fetch_guild(self.guild_id)
        locked = {guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False)}
        me = getattr(guild, "me", None)
        if me is not None:
            locked[me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        try:
            owner = await guild.fetch_member(guild.owner_id)
            self._owner_name = owner.display_name
        except (discord.HTTPException, AttributeError, TypeError):
            pass

        async def known(meta_key):
            cid = self._meta(meta_key)
            if not cid:
                return None
            ch = guild.get_channel(int(cid))
            if ch is None:
                try:
                    ch = await self.client.fetch_channel(int(cid))
                except discord.NotFound:
                    return None
            return ch

        # Earlier versions: one "📊 Valheim" category and a rotating "titles" channel.
        if not self._meta("statchan:cat:watch") and self._meta("statchan:category"):
            self._meta("statchan:cat:watch", self._meta("statchan:category"))
        if not self._meta("statchan:title_owner") and self._meta("statchan:titles"):
            self._meta("statchan:title_owner", self._meta("statchan:titles"))
        if self.stats_take_status and not self._meta("statchan:players"):
            self._meta("statchan:players", self.status.channel_id)

        groups = [(g, self.stats_categories.get(g, title)) for g, title, _ in stat_channels.GROUPS]
        if self.stats_layout == "single":
            groups = [("watch", self.stats_category)]
        categories = {}
        for pos, (group, title) in enumerate(groups):
            if not any(self.stats_layout == "single" or stat_channels.GROUP_OF[k] == group
                       for k in self.stats_show):
                continue
            cat = await known(f"statchan:cat:{group}") or next((c for c in guild.categories if c.name == title), None)
            if cat is None:
                cat = await guild.create_category(title, overwrites=locked, position=pos,
                                                  reason="Valheim stat channels")
                log.info("admin_bot: created the %s category", title)
            elif self.stats_layout == "split" and cat.name != title and group == "watch" and \
                    cat.name == self.stats_category:
                await cat.edit(name=title, reason="Valheim stat channels")   # the old single category
            self._meta(f"statchan:cat:{group}", cat.id)
            categories[group] = cat

        out = {}
        for i, key in enumerate(self.stats_show):
            cat = categories["watch" if self.stats_layout == "single" else stat_channels.GROUP_OF[key]]
            ch = await known(f"statchan:{key}")
            if ch is None:
                ch = await guild.create_voice_channel(self._stat_name(key) or f"… {key}", category=cat,
                                                      overwrites=locked, position=i,
                                                      reason="Valheim stat channel")
                log.info("admin_bot: created the stat channel %s", ch.name)
            elif getattr(ch, "category", None) != cat:
                await ch.edit(category=cat, overwrites=locked, position=i, reason="Valheim stat channel")
                log.info("admin_bot: moved %s into %s", ch.name, cat.name)
            self._meta(f"statchan:{key}", ch.id)
            out[key] = ch
        return out

    async def _stats_loop(self) -> None:
        """Keep the stat channels' names current, within Discord's rename limit."""
        import discord
        import stat_channels
        await asyncio.sleep(10)
        pace = stat_channels.Renamer()
        channels: dict = {}
        warned = False
        while True:
            try:
                if not channels:
                    channels = await self._ensure_stat_channels()
                    for key, ch in channels.items():
                        pace.current[key] = ch.name
                for key, ch in list(channels.items()):
                    name = self._stat_name(key)
                    if not pace.due(key, name):
                        continue
                    try:
                        await ch.edit(name=name, reason="Valheim stats")
                        pace.renamed(key, name)
                    except discord.NotFound:          # deleted: make it again next time round
                        self._meta(f"statchan:{key}", "")
                        channels = {}
                        break
                    except discord.Forbidden:
                        if not warned:
                            warned = True
                            log.warning("admin_bot: no permission to rename the stat channels (the bot needs "
                                        "Manage Channels)")
                        pace.renamed(key, ch.name)     # back off for one interval
            except discord.Forbidden:
                log.warning("admin_bot: can't create the stat channels: the bot needs Manage Channels; "
                            "trying again in 10 min")
                await asyncio.sleep(600)
            except Exception as e:  # noqa: BLE001
                log.warning("admin_bot: stat channels failed: %s", e)
            await asyncio.sleep(30)

    async def _title_role(self, guild, category: str):
        """The Discord role for a title: configured, remembered, found by name, or created.
        A role still carrying an earlier name (community.TITLE_OLD_NAMES) is renamed."""
        import discord
        import community
        name, why, colour = community.TITLES[category]
        configured = (self.title_role_ids.get(category)
                      or community.get_meta(self.db, f"role:title:{category}")
                      or community.get_meta(self.db, f"title_role:{category}"))     # before 2026-10
        role = await self._ensure_role(guild, f"title:{category}", configured, name, colour,
                                       reason=f"Valheim title: {why}")
        if role.name in community.TITLE_OLD_NAMES.get(category, ()):
            old = role.name
            await role.edit(name=name, colour=discord.Colour(colour), reason="Valheim title renamed")
            log.info("admin_bot: renamed the title role %s to %s", old, name)
        return role

    async def _move_role(self, guild, role, old_uid, new_uid, reason: str) -> None:
        import discord
        for uid, add in ((old_uid, False), (new_uid, True)):
            if not uid:
                continue
            try:
                member = await guild.fetch_member(int(uid))
                await (member.add_roles if add else member.remove_roles)(role, reason=reason)
            except discord.NotFound:              # left the Discord server
                pass

    async def _sync_titles(self, recompute: bool) -> tuple:
        """Give each title role to the Discord user linked to the title's character.
        recompute=True picks the leaders again (the weekly run); False only follows
        links made or removed since. Returns (holders, changed categories)."""
        import discord
        import community
        if self._titles_lock is None:
            self._titles_lock = asyncio.Lock()
        async with self._titles_lock:
            guild = self.client.get_guild(self.guild_id) or await self.client.fetch_guild(self.guild_id)
            since = self._titles_since()
            leaders = community.title_leaders(self.db, since) if recompute else {}
            holders, changed = {}, set()
            for cat, (name, _, _) in community.TITLES.items():
                held = community.title_holder(self.db, cat) or {}
                if recompute:
                    player = (leaders.get(cat) or {}).get("player")
                    if (player or "").lower() != (held.get("player") or "").lower():
                        changed.add(cat)
                else:
                    player = held.get("player")
                uid = community.linked_user(self.db, player) if player else None
                if uid != held.get("user_id"):
                    try:
                        role = await self._title_role(guild, cat)
                        await self._move_role(guild, role, held.get("user_id"), uid,
                                              f"Valheim title {name}: {player or 'nobody'}")
                    except discord.Forbidden:
                        log.warning("admin_bot: can't give the %s title role: the bot needs Manage Roles, and "
                                    "its own role must be above the title roles in Server Settings -> Roles", name)
                        uid = held.get("user_id")      # try again next time
                community.set_title_holder(self.db, cat, player, uid)
                holders[cat] = {"player": player, "user_id": uid,
                                "v": community.title_value(self.db, cat, player, since)} if player else None
            return holders, changed

    async def _rename_old_titles(self) -> None:
        """Rename title roles made under an earlier name (Huginn -> Sleipnir) at start-up,
        instead of waiting until that title next changes hands."""
        import community
        guild = self.client.get_guild(self.guild_id) or await self.client.fetch_guild(self.guild_id)
        for cat in community.TITLE_OLD_NAMES:
            if (self.title_role_ids.get(cat) or community.get_meta(self.db, f"role:title:{cat}")
                    or community.get_meta(self.db, f"title_role:{cat}")):
                await self._title_role(guild, cat)

    async def _titles_loop(self) -> None:
        """Once a week (and right away the first time), hand the titles to the leaders."""
        import discord
        import community
        await asyncio.sleep(15)
        try:
            await self._rename_old_titles()
        except Exception as e:  # noqa: BLE001
            log.warning("admin_bot: couldn't rename an old title role: %s", e)
        while True:
            try:
                week = self._titles_due()
                if week:
                    holders, changed = await self._sync_titles(recompute=True)
                    community.set_meta(self.db, "titles_week", week)
                    log.info("admin_bot: titles reassigned for %s (%d changed)", week, len(changed))
                    if changed:
                        await self._announce_titles(community.render_titles(holders, changed, self.titles_period))
            except discord.HTTPException as e:
                log.warning("admin_bot: title roles failed: %s", e)
            except Exception as e:  # noqa: BLE001
                log.warning("admin_bot: title roles failed: %s", e)
            await asyncio.sleep(300)

    def _titles_relink(self) -> None:
        """After /valheim link or unlink: move a title role to the newly linked account."""
        if not self._titles_task:
            return

        async def run():
            try:
                await self._sync_titles(recompute=False)
            except Exception as e:  # noqa: BLE001
                log.warning("admin_bot: title roles after a link change failed: %s", e)
        asyncio.ensure_future(run())

    async def _announce_titles(self, embed: dict) -> None:
        import discord
        if self.titles_channel:
            channel = self.client.get_channel(self.titles_channel) or await self.client.fetch_channel(self.titles_channel)
            await channel.send(embed=discord.Embed.from_dict(embed), allowed_mentions=discord.AllowedMentions.none())
        elif self.post_embed:
            await asyncio.get_running_loop().run_in_executor(None, self.post_embed, embed)

    def _build_client(self):
        import discord
        from discord import app_commands

        bot = self
        intents = discord.Intents.none()
        intents.guilds = True                 # enough for buttons + slash commands; no privileged intents

        class Tree(app_commands.CommandTree):
            async def interaction_check(self, it: discord.Interaction) -> bool:
                where = bot.wrong_channel(it)
                if where:
                    await it.response.send_message(
                        f"Run `/{it.command.qualified_name}` in <#{where}>, please. It keeps the chat tidy.",
                        ephemeral=True)
                    return False
                return True

        class Client(discord.Client):
            def __init__(self):
                super().__init__(intents=intents)
                self.tree = Tree(self)

            async def setup_hook(self):
                bot._register_commands(self.tree)
                if bot.guild_id:
                    g = discord.Object(id=bot.guild_id)
                    self.tree.copy_global_to(guild=g)
                    await self.tree.sync(guild=g)        # instant in that server
                else:
                    await self.tree.sync()               # global: can take up to an hour to appear

            async def on_ready(self):
                log.info("admin_bot: connected as %s; posting join notices to channel %s", self.user, bot.channel_id)
                bot.ready.set()
                bot._start_tasks()

            async def on_interaction(self, it: discord.Interaction):
                cid = (it.data or {}).get("custom_id", "") if it.type == discord.InteractionType.component else ""
                if cid.startswith(BTN_PREFIX + ":"):
                    await bot._on_button(it, cid)

        return Client()

    async def _on_button(self, it, custom_id: str) -> None:
        _, action, pid = custom_id.split(":", 2)
        if action == "rsvp":                     # game-night signups: anyone
            await self._on_rsvp(it, pid)
            return
        if not self._is_admin(it.user):
            await it.response.send_message("Only the server admins can do that.", ephemeral=True)
            return
        if action == "restart":                  # the button on a world-settings confirmation
            minutes = int(pid) if pid.isdigit() else 5
            await it.response.send_message(self.start_restart(minutes, "applying new world settings", str(it.user)),
                                            ephemeral=True)
            return
        if action == "ignore":
            outcome = "ignored"
        else:
            try:
                changes = (self.lists.permit if action == "permit" else self.lists.ban)(pid)
            except OSError as e:
                await it.response.send_message(f"Couldn't edit the list files: {e}", ephemeral=True)
                return
            verb = "permitted" if action == "permit" else "banned"
            outcome = f"{verb} ({', '.join(changes)})" if changes else f"{verb} (lists already said so)"
            log.info("admin_bot: %s %s by %s: %s", pid, verb, it.user, changes)
            if action == "permit":
                outcome += await self._welcome_requester(self._refused.get(pid))
        embed = it.message.embeds[0] if it.message and it.message.embeds else None
        if embed is not None:
            embed.add_field(name="Result", value=f"{outcome} by {it.user.mention}", inline=False)
        await it.response.edit_message(embed=embed, view=self._buttons(pid, disabled=True))
        if self.tidy_hours > 0 and it.message:
            self._queue_tidy(it.message.channel.id, it.message.id)

    def _queue_tidy(self, channel_id, message_id) -> None:
        try:
            queue = json.loads(self._meta("tidy:queue") or "[]")
        except ValueError:
            queue = []
        queue.append([str(channel_id), str(message_id), int(time.time() + self.tidy_hours * 3600)])
        self._meta("tidy:queue", json.dumps(queue[-500:]))

    async def _tidy_notices(self) -> None:
        """Delete handled refused-join notices once they're tidy_notices_hours old."""
        import discord
        try:
            queue = json.loads(self._meta("tidy:queue") or "[]")
        except ValueError:
            queue = []
        due = [q for q in queue if q[2] <= time.time()]
        if not due:
            return
        keep = [q for q in queue if q[2] > time.time()]
        for cid, mid, _ in due:
            try:
                await self.client.get_partial_messageable(int(cid)).get_partial_message(int(mid)).delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.info("admin_bot: can't delete old notices (needs Manage Messages); keeping them")
                keep = []                         # no point retrying without the permission
                break
        self._meta("tidy:queue", json.dumps(keep))

    def react_to(self, kind: str, channel_id, message_id) -> None:
        """Thread-safe: react to one of Huginn's posts (the monitor calls this after posting)."""
        emoji = REACTIONS.get(kind)
        if not (emoji and self.loop and self.ready.is_set()):
            return

        async def react():
            import discord
            try:
                msg = self.client.get_partial_messageable(int(channel_id)).get_partial_message(int(message_id))
                await msg.add_reaction(emoji)
            except discord.HTTPException as e:
                log.debug("admin_bot: couldn't react to a %s post: %s", kind, e)
        asyncio.run_coroutine_threadsafe(react(), self.loop)

    def command_channel(self, name: str) -> Optional[int]:
        """The channel a /valheim command belongs in, or None when it works anywhere."""
        if not self.command_channels_on or name not in COMMAND_PLACES:
            return None
        if name in self.command_channel_ids:
            return self.command_channel_ids[name]
        cid = self._meta(f"layout:ch:{COMMAND_PLACES[name]}") if self._attached else None
        return int(cid) if cid and str(cid).isdigit() else None

    def wrong_channel(self, it) -> Optional[int]:
        """The channel to send someone to when they run a command in the wrong one; None
        when it's fine here (right channel or a thread in it, or they're an admin)."""
        cmd = getattr(it, "command", None)
        name = getattr(cmd, "name", None)
        if not name or self._is_admin(it.user):
            return None
        want = self.command_channel(name)
        if want is None:
            return None
        channel = getattr(it, "channel", None)
        here = {getattr(it, "channel_id", None), getattr(channel, "parent_id", None)}
        return None if want in here else want

    def _register_commands(self, tree) -> None:
        import discord
        from discord import app_commands
        bot = self
        group = app_commands.Group(name="valheim", description="Manage who can join the Valheim server")

        async def guard(it: discord.Interaction) -> bool:
            if not bot._is_admin(it.user):
                await it.response.send_message("Only the server admins can do that.", ephemeral=True)
                return False
            return True

        async def run(it: discord.Interaction, fn, pid: str, verb: str):
            if not await guard(it):
                return
            pid = pid.strip()
            if not VALID_ID.match(pid):
                await it.response.send_message(
                    f"`{discord.utils.escape_markdown(pid)[:60]}` doesn't look like a player ID. "
                    "Use the form in the list files, e.g. `V_76561198000000000`.", ephemeral=True)
                return
            try:
                changes = fn(pid)
            except OSError as e:
                await it.response.send_message(f"Couldn't edit the list files: {e}", ephemeral=True)
                return
            log.info("admin_bot: /%s %s by %s: %s", verb, pid, it.user, changes)
            await it.response.send_message(f"`{pid}`: " + (", ".join(changes) or "no change needed"), ephemeral=True)

        @group.command(name="permit", description="Let a player in (unban; add to the permitted list if one is used)")
        @app_commands.describe(player_id="Platform ID, e.g. V_76561198000000000")
        async def permit(it: discord.Interaction, player_id: str):
            await run(it, bot.lists.permit, player_id, "permit")

        @group.command(name="ban", description="Ban a player (and remove them from the permitted list)")
        @app_commands.describe(player_id="Platform ID, e.g. V_76561198000000000")
        async def ban(it: discord.Interaction, player_id: str):
            await run(it, bot.lists.ban, player_id, "ban")

        @group.command(name="unban", description="Remove a player from the ban list")
        async def unban(it: discord.Interaction, player_id: str):
            await run(it, lambda p: ["removed from bannedlist.txt"] if bot.lists.remove("banned", p) else [],
                      player_id, "unban")

        @group.command(name="unpermit", description="Remove a player from the permitted list")
        async def unpermit(it: discord.Interaction, player_id: str):
            await run(it, lambda p: ["removed from permittedlist.txt"] if bot.lists.remove("permitted", p) else [],
                      player_id, "unpermit")

        @group.command(name="online", description="Who's on the Valheim server right now")
        async def online(it: discord.Interaction):
            import extras
            if bot.live is None:
                await it.response.send_message("The monitor isn't tracking the server yet.", ephemeral=True)
                return
            snap = bot.live.snapshot()
            if snap["down"]:
                text = f"🔴 **{bot.server_name}** is offline."
            elif snap["count"] == 0:
                text = f"🟢 **{bot.server_name}** is up, and nobody is playing."
            else:
                text = f"🟢 **{snap['count']} online** in {bot.server_name}:\n" + "\n".join(extras.player_lines(snap))
            await it.response.send_message(text[:2000])

        import world_settings as ws

        @group.command(name="settings", description="Show the world settings (preset, modifiers) and what can change")
        async def settings(it: discord.Interaction):
            if not await guard(it):
                return
            st = ws.read_file(bot.world_file)
            opts = [f"**preset**: {', '.join(ws.PRESETS)}"]
            opts += [f"**{k}**: {', '.join(v)}. {ws.DESCRIPTIONS[k]}" for k, v in ws.MODIFIERS.items()]
            opts += [f"**{k}**: on/off. {ws.DESCRIPTIONS[k]}" for k in ws.SETKEYS]
            embed = discord.Embed(title="🌍 World settings", color=0x5865F2,
                                  description="\n".join(ws.describe(st)))
            embed.add_field(name="What can change", value="\n".join(opts)[:1024], inline=False)
            embed.add_field(name="How", value="`/valheim preset`, `/valheim modifier`, `/valheim setkey`. "
                            "Changes apply at the next restart (`/valheim restart`).", inline=False)
            await it.response.send_message(embed=embed, ephemeral=True)

        @group.command(name="modifier", description="Change a world modifier (applies at the next restart)")
        @app_commands.describe(name="Which modifier", value="New value (normal = default)")
        @app_commands.choices(name=[app_commands.Choice(name=f"{k}: {ws.DESCRIPTIONS[k]}"[:100], value=k)
                                    for k in ws.MODIFIERS])
        async def modifier(it: discord.Interaction, name: str, value: str):
            await bot._change_setting(it, "modifier", name, value)

        @modifier.autocomplete("value")
        async def modifier_values(it: discord.Interaction, current: str):
            key = getattr(it.namespace, "name", None)
            values = ws.MODIFIERS.get(key) or sorted({v for vs in ws.MODIFIERS.values() for v in vs})
            return [app_commands.Choice(name=v, value=v) for v in values if current.lower() in v][:25]

        @group.command(name="preset", description="Change the world preset (applies at the next restart)")
        @app_commands.choices(name=[app_commands.Choice(name=p, value=p) for p in ws.PRESETS])
        async def preset(it: discord.Interaction, name: str):
            await bot._change_setting(it, "preset", name)

        @group.command(name="setkey", description="Turn a world option on or off (applies at the next restart)")
        @app_commands.choices(key=[app_commands.Choice(name=f"{k}: {ws.DESCRIPTIONS[k]}"[:100], value=k)
                                   for k in ws.SETKEYS],
                              state=[app_commands.Choice(name="on", value="on"),
                                     app_commands.Choice(name="off", value="off")])
        async def setkey(it: discord.Interaction, key: str, state: str):
            await bot._change_setting(it, "setkey", key, state)

        import community

        async def need_db(it: discord.Interaction) -> bool:
            if not bot.db_path:
                await it.response.send_message("This needs the stats database (`database.path` in config.json).",
                                                ephemeral=True)
                return False
            return True

        async def player_choices(it: discord.Interaction, current: str):
            if not bot.db_path:
                return []
            return [app_commands.Choice(name=n[:100], value=n[:100]) for n in community.player_names(bot.db, current)]

        @group.command(name="stats", description="Play time, deaths and more for a character (yours if linked)")
        @app_commands.describe(player="Character name (leave empty for your linked character)")
        async def stats(it: discord.Interaction, player: str = ""):
            if not await need_db(it):
                return
            name = player.strip()
            if not name:
                mine = community.linked_players(bot.db, it.user.id)
                if not mine:
                    await it.response.send_message("Which character? Give a name, or link yours once with "
                                                    "`/valheim link <character>`.", ephemeral=True)
                    return
                name = mine[0]
            s = community.player_stats(bot.db, name)
            if not s:
                await it.response.send_message(f"No play time recorded for **{discord.utils.escape_markdown(name)}**.",
                                                ephemeral=True)
                return
            embed = community.render_stats(s, community.log_clock_offset(bot.db),
                                           community.linked_user(bot.db, s["player"]))
            await it.response.send_message(embed=discord.Embed.from_dict(embed))
        stats.autocomplete("player")(player_choices)

        @group.command(name="top", description="Leaderboards: time played, deaths, visits, longest session, achievements")
        @app_commands.choices(category=[app_commands.Choice(name=v[0], value=k) for k, v in community.TOP.items()])
        async def top(it: discord.Interaction, category: str = "time"):
            if not await need_db(it):
                return
            embed = community.render_top(category, community.top(bot.db, category, 10))
            await it.response.send_message(embed=discord.Embed.from_dict(embed))

        @group.command(name="titles", description="Who holds Heimdall, Hel, Sleipnir, Thor and Bragi (the top of each board)")
        @app_commands.describe(refresh="Admins: reassign the titles now instead of waiting for the weekly run")
        async def titles(it: discord.Interaction, refresh: bool = False):
            if not await need_db(it):
                return
            if not bot._titles_task:
                await it.response.send_message("Title roles are off on this server (`admin_bot.titles` in "
                                                "config.json).", ephemeral=True)
                return
            if refresh and not bot._is_admin(it.user):
                await it.response.send_message("Only the server admins can reassign the titles.", ephemeral=True)
                return
            await it.response.defer()
            try:
                if refresh:
                    holders, changed = await bot._sync_titles(recompute=True)
                    log.info("admin_bot: titles reassigned by %s (%d changed)", it.user, len(changed))
                else:
                    holders, changed = (await bot._sync_titles(recompute=False))[0], set()
            except discord.HTTPException as e:
                await it.followup.send(f"Couldn't update the title roles: {e}", ephemeral=True)
                return
            await it.followup.send(embed=discord.Embed.from_dict(
                community.render_titles(holders, changed, bot.titles_period)),
                allowed_mentions=discord.AllowedMentions.none())

        @group.command(name="notify", description="Get a DM when someone joins the server")
        @app_commands.describe(when="What to be told about", player="For follow/unfollow: which character")
        @app_commands.choices(when=[app_commands.Choice(name="First player joins an empty server", value="first"),
                                    app_commands.Choice(name="A specific character joins (follow)", value="follow"),
                                    app_commands.Choice(name="Stop following a character", value="unfollow"),
                                    app_commands.Choice(name="Turn all notifications off", value="off"),
                                    app_commands.Choice(name="Show my notifications", value="list")])
        async def notify(it: discord.Interaction, when: str, player: str = ""):
            if not await need_db(it):
                return
            uid, name = it.user.id, player.strip()
            if when in ("follow", "unfollow"):
                if not name:
                    await it.response.send_message("Which character? Fill in `player`.", ephemeral=True)
                    return
                name = community.known_player(bot.db, name) or name
                community.set_follow(bot.db, uid, name, when == "follow")
                text = (f"You'll get a DM when **{name}** joins." if when == "follow"
                        else f"No more DMs about **{name}**.")
            elif when == "first":
                community.set_notify_first(bot.db, uid, True)
                text = "You'll get a DM when someone joins an empty server."
            elif when == "off":
                community.notify_off(bot.db, uid)
                text = "All notifications off."
            else:
                mine = community.my_notifications(bot.db, uid)
                parts = (["first player joins"] if mine["first"] else []) + [f"**{p}** joins" for p in mine["follows"]]
                text = "You're notified when: " + ", ".join(parts) if parts else "You have no notifications on."
            if when in ("first", "follow"):
                text += " (Make sure you accept DMs from server members.)"
            await it.response.send_message(text, ephemeral=True)
        notify.autocomplete("player")(player_choices)

        @group.command(name="link", description="Link your Discord account to your character")
        @app_commands.describe(character="Your character's name, as it appears in-game")
        async def link(it: discord.Interaction, character: str):
            if not await need_db(it):
                return
            name = community.known_player(bot.db, character)
            if not name:
                await it.response.send_message(
                    f"No character called **{discord.utils.escape_markdown(character)}** has played here yet. "
                    "Join once, then link.", ephemeral=True)
                return
            err = community.link_player(bot.db, name, it.user.id)
            await it.response.send_message(err or f"✅ **{name}** is now linked to you. `/valheim stats` shows "
                                                   "your stats, and milestones will mention you.", ephemeral=True)
            if not err:
                bot._titles_relink()
                if bot.link_nickname and it.guild and isinstance(it.user, discord.Member) \
                        and not it.user.nick and it.user.id != it.guild.owner_id:
                    try:
                        await it.user.edit(nick=name[:32], reason="/valheim link")
                    except discord.HTTPException as e:
                        log.info("admin_bot: couldn't set %s's nickname (needs Manage Nicknames): %s", it.user, e)
        link.autocomplete("character")(player_choices)

        @group.command(name="unlink", description="Unlink a character from your Discord account")
        @app_commands.describe(character="Leave empty to unlink all of yours. Admins can unlink anyone's.")
        async def unlink(it: discord.Interaction, character: str = ""):
            if not await need_db(it):
                return
            mine = community.linked_players(bot.db, it.user.id)
            if not character.strip():
                for n in mine:
                    community.unlink_player(bot.db, n)
                await it.response.send_message(f"Unlinked {', '.join(mine)}." if mine else "Nothing was linked.",
                                                ephemeral=True)
                bot._titles_relink()
                return
            name = community.known_player(bot.db, character) or character.strip()
            owner = community.linked_user(bot.db, name)
            if owner and owner != str(it.user.id) and not bot._is_admin(it.user):
                await it.response.send_message("That character is linked to someone else; only an admin can "
                                                "unlink it.", ephemeral=True)
                return
            done = community.unlink_player(bot.db, name)
            await it.response.send_message(f"Unlinked **{name}**." if done else f"**{name}** wasn't linked.",
                                            ephemeral=True)
            if done:
                bot._titles_relink()
        unlink.autocomplete("character")(player_choices)

        @group.command(name="request-access", description="New here? Tell the admins which character you'll join as")
        @app_commands.describe(character="The character name you'll use in Valheim")
        async def request_access(it: discord.Interaction, character: str):
            if not await need_db(it):
                return
            name = character.strip()[:40]
            if not name:
                await it.response.send_message("Which character name?", ephemeral=True)
                return
            community.request_access(bot.db, name, it.user.id)
            bot.post_admin(f"🙋 <@{it.user.id}> asked to join as **{discord.utils.escape_markdown(name)}**. "
                           "When that character is refused, the notice will say it's them, and **Permit** "
                           "links the character to them and lets them know.")
            await it.response.send_message(
                f"Thanks! Now try joining as **{discord.utils.escape_markdown(name)}** "
                "(`/valheim join` has the code). If you're turned away, the admins see it's you and "
                "can let you in with one click, and you'll get a DM.", ephemeral=True)

        @group.command(name="plan", description="Plan a game night: a signup with a reminder, or a poll for the time")
        @app_commands.describe(title="What's happening, e.g. 'Bonemass run'",
                               when="e.g. 20:00, 8pm, sat 20:00, in 2h. Several ('sat 20:00, sun 18:00') "
                                    "start a poll for the time")
        async def plan(it: discord.Interaction, title: str, when: str):
            if not await need_db(it):
                return
            import datetime as _dt
            now = _dt.datetime.now().astimezone()
            title = title.strip()[:100]
            try:
                times = community.parse_whens(when, now)
            except ValueError as e:
                await it.response.send_message(str(e), ephemeral=True)
                return
            if not times:
                await it.response.send_message("When? e.g. `sat 20:00`.", ephemeral=True)
                return
            if len(times) == 1:
                await it.response.send_message(f"📅 Planned **{title}**.", ephemeral=True)
                await bot._post_plan(it.channel, title, times[0], it.user.id, it.guild)
                return
            await it.response.send_message(f"🗳️ Poll posted for **{title}**. When it closes, the winning time "
                                            "becomes a signup automatically.", ephemeral=True)
            await bot._post_time_poll(it.channel, title, times[:10], it.user.id)

        @group.command(name="map", description="The world seed and a link to a map of it (spoilers!)")
        async def map_(it: discord.Interaction):
            if not bot.map_enabled:
                await it.response.send_message("The map link is turned off on this server.", ephemeral=True)
                return
            found = (("World", bot.map_seed) if bot.map_seed
                     else await asyncio.to_thread(community.world_seed, bot.lists.save_dir))
            if not found:
                log.warning("admin_bot: no world seed found under %s/worlds_local; "
                            "set admin_bot.map.seed in config.json", bot.lists.save_dir)
                await it.response.send_message(
                    "Couldn't read the world seed from the save folder. An admin can set it with "
                    "`\"map\": {\"seed\": \"…\"}` in config.json.", ephemeral=True)
                return
            world, seed = found
            await it.response.send_message(
                f"🗺️ **{world}**: seed `{seed}`\n[Open the world map]({community.map_url(seed)}): "
                "**spoilers**, it shows the whole world, including places nobody has found yet.", ephemeral=True)

        @group.command(name="join", description="How to join the Valheim server: join code, address, password")
        async def join(it: discord.Interaction):
            embed = discord.Embed.from_dict(bot.join_embed())
            await it.response.send_message(embed=embed, ephemeral=True)

        @group.command(name="backups", description="List the copied world backups")
        async def backups(it: discord.Interaction):
            if not await guard(it):
                return
            if bot.backups is None:
                await it.response.send_message("Backup copying isn't set up (the `backups` block in config.json).",
                                                ephemeral=True)
                return
            rows = bot.backups.listing(10)
            if not rows:
                text = f"No backups in `{bot.backups.dest}` yet."
            else:
                text = f"Newest backups in `{bot.backups.dest}`:\n" + "\n".join(
                    f"• `{stem}` · {size / 1e6:.0f} MB · <t:{int(mtime)}:R>" for mtime, size, stem in rows)
            await it.response.send_message(text[:2000], ephemeral=True)

        @group.command(name="update-check", description="Ask the server to check for a Valheim update now")
        async def update_check(it: discord.Interaction):
            if not await guard(it):
                return
            err = await bot._request("check")
            await it.response.send_message(
                f"Couldn't ask for a check: {err}" if err else
                "Asked the server to check for an update. The result posts in the admin channel within a "
                "minute or two, and if an update is found, in the public channel too.", ephemeral=True)

        @group.command(name="restart", description="Restart the server (installs any waiting update), with a warning")
        @app_commands.describe(minutes="Minutes of warning, 0-60 (0 = now). It happens early if everyone leaves.",
                               reason="Shown to players, e.g. 'installing the update'")
        async def restart(it: discord.Interaction, minutes: int = 5, reason: str = ""):
            # Plain `int`, not app_commands.Range: with postponed annotations discord.py
            # resolves the type at module level, where app_commands isn't imported.
            if not await guard(it):
                return
            await it.response.send_message(bot.start_restart(minutes, reason, str(it.user)), ephemeral=True)

        @group.command(name="restart-cancel", description="Cancel a restart countdown")
        async def restart_cancel(it: discord.Interaction):
            if not await guard(it):
                return
            if bot._countdown and not bot._countdown.done():
                bot._countdown_cancel.set()
                await it.response.send_message("Cancelled.", ephemeral=True)
            else:
                await it.response.send_message("No restart is counting down.", ephemeral=True)

        @group.command(name="lists", description="Show the permitted, banned and admin lists")
        async def lists(it: discord.Interaction):
            if not await guard(it):
                return
            try:
                parts = []
                for which in ("permitted", "banned", "admin"):
                    ids = bot.lists.ids(which)
                    parts.append(f"**{LIST_FILES[which]}** ({len(ids)})\n" +
                                 ("\n".join(f"`{x}`" for x in ids[:40]) or "_empty_"))
                text = "\n\n".join(parts)
            except OSError as e:
                text = f"Couldn't read the list files: {e}"
            await it.response.send_message(text[:1990], ephemeral=True)

        @group.command(name="setup", description="Admins: organise this Discord into Valheim-themed channels")
        @app_commands.describe(action="preview: show what would change · apply: do it (asks first) · "
                                      "undo: put renamed channels back")
        @app_commands.choices(action=[app_commands.Choice(name="preview", value="preview"),
                                      app_commands.Choice(name="apply", value="apply"),
                                      app_commands.Choice(name="undo", value="undo")])
        async def setup(it: discord.Interaction, action: str = "preview"):
            import server_layout
            if not await guard(it):
                return
            if not it.guild:
                await it.response.send_message("Run this in your Discord server.", ephemeral=True)
                return
            await it.response.defer(ephemeral=True, thinking=True)
            guild = it.guild
            if action == "undo":
                restored, created, problems = await bot._layout_undo(guild)
                text = f"↩️ Put {restored} channel(s) and categories back as they were."
                if created:
                    text += ("\nThe setup also created these; delete any you don't want (they may have "
                             "messages): " + ", ".join(c.mention if hasattr(c, "mention") else c.name
                                                       for c in created))
                if problems:
                    text += "\n⚠️ " + "\n⚠️ ".join(problems[:10])
                await it.followup.send(text[:1990], ephemeral=True)
                return
            p = await bot._layout_plan(guild)
            embed = discord.Embed(title="🛠️ Server layout: preview" if action == "preview"
                                  else "🛠️ Server layout: apply this?",
                                  description=server_layout.render(p), color=0xC27C0E)
            if action == "preview":
                embed.set_footer(text="Run /valheim setup apply to do it. Undo with /valheim setup undo.")
                await it.followup.send(embed=embed, ephemeral=True)
                return
            view = discord.ui.View(timeout=600)
            go = discord.ui.Button(label="Apply", style=discord.ButtonStyle.danger)
            stop = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

            async def on_go(bi: discord.Interaction):
                if bi.user.id != it.user.id:
                    await bi.response.send_message("Only the admin who ran it can confirm.", ephemeral=True)
                    return
                await bi.response.edit_message(content="Working… (this takes a minute on a big server)",
                                               embed=None, view=None)
                problems = await bot._layout_apply(guild, p)
                await bot._layout_guide(guild, p)
                log.info("admin_bot: /valheim setup applied by %s (%d problem(s))", it.user, len(problems))
                text = ("✅ Done. A guide to every channel is posted in the welcome channel. "
                        "Undo with `/valheim setup undo`.")
                if problems:
                    text += "\n⚠️ Some changes failed (the bot needs Manage Channels and Manage Roles):\n" + \
                        "\n".join(problems[:10])
                await bi.edit_original_response(content=text[:1990])

            async def on_stop(bi: discord.Interaction):
                await bi.response.edit_message(content="Cancelled; nothing changed.", embed=None, view=None)
            go.callback, stop.callback = on_go, on_stop
            view.add_item(go)
            view.add_item(stop)
            await it.followup.send(embed=embed, view=view, ephemeral=True)

        tree.add_command(group)


def build_admin_bot(cfg: dict, server_name: str) -> Optional[AdminBot]:
    b = cfg.get("admin_bot") or {}
    if not b.get("enabled"):
        return None
    try:
        bot = AdminBot(b, server_name)
    except ImportError:
        log.warning("admin_bot.enabled but discord.py isn't installed (pip install -r requirements.txt); disabled")
        return None
    except (ValueError, KeyError) as e:
        log.warning("admin_bot disabled: %s", e)
        return None
    bot.start()
    return bot
