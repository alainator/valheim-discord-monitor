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
import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Optional

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
        self._board_task = None
        self.live = None                     # extras.LiveState, from attach()
        self.backups = None                  # extras.BackupCopier, from attach()
        self.updater = None                  # updater.UpdateWatcher, from attach()
        self.announce = None                 # posts a line to the public webhook channel
        self._countdown = None               # running /valheim restart countdown task
        self._countdown_cancel = None
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

    def attach(self, live=None, backups=None, updater=None, announce=None) -> None:
        """Hand the bot the monitor's live state, backup copier, updater link and a way to
        post to the public channel (for the board and the /valheim commands)."""
        self.live, self.backups, self.updater, self.announce = live, backups, updater, announce

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
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed, view=self._buttons(pid))

    def _build_client(self):
        import discord
        from discord import app_commands

        bot = self
        intents = discord.Intents.none()
        intents.guilds = True                 # enough for buttons + slash commands; no privileged intents

        class Client(discord.Client):
            def __init__(self):
                super().__init__(intents=intents)
                self.tree = app_commands.CommandTree(self)

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
                if bot.status and bot._status_task is None:     # on_ready repeats after reconnects
                    bot._status_task = asyncio.ensure_future(bot._status_loop())
                if bot.board_channel and bot._board_task is None:
                    bot._board_task = asyncio.ensure_future(bot._board_loop())

            async def on_interaction(self, it: discord.Interaction):
                cid = (it.data or {}).get("custom_id", "") if it.type == discord.InteractionType.component else ""
                if cid.startswith(BTN_PREFIX + ":"):
                    await bot._on_button(it, cid)

        return Client()

    async def _on_button(self, it, custom_id: str) -> None:
        _, action, pid = custom_id.split(":", 2)
        if not self._is_admin(it.user):
            await it.response.send_message("Only the server admins can do that.", ephemeral=True)
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
        embed = it.message.embeds[0] if it.message and it.message.embeds else None
        if embed is not None:
            embed.add_field(name="Result", value=f"{outcome} by {it.user.mention}", inline=False)
        await it.response.edit_message(embed=embed, view=self._buttons(pid, disabled=True))

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
            minutes = max(0, min(int(minutes), 60))
            if bot._countdown and not bot._countdown.done():
                await it.response.send_message("A restart is already counting down. `/valheim restart-cancel` stops it.",
                                                ephemeral=True)
                return
            if bot.updater is None:
                await it.response.send_message("Restarting needs the updater link (`updater` in config.json and "
                                                "the host helper, see host/README.md).", ephemeral=True)
                return
            if bot._online_count() == 0:
                minutes = 0
            bot._countdown_cancel = asyncio.Event()
            bot._countdown = asyncio.ensure_future(bot._restart_countdown(minutes, reason.strip()[:100], str(it.user)))
            await it.response.send_message(
                "Restarting now (nobody is online)." if minutes == 0 else
                f"Restart in {minutes} min, or sooner if everyone leaves. `/valheim restart-cancel` stops it.",
                ephemeral=True)

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
