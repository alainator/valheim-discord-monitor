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

It needs write access to the server's save dir (where the list files live), so it
suits the self-hosted `file` source, where the monitor runs next to the server.
Webhooks are one-way, so this is a real bot: `pip install discord.py`, a bot token,
and it joins the Discord server with the `applications.commands` + `bot` scopes.
Only the users / roles in `admin_user_ids` / `admin_role_ids` can press the buttons.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger("valheim-monitor.bot")

LIST_FILES = {"permitted": "permittedlist.txt", "banned": "bannedlist.txt", "admin": "adminlist.txt"}
STEAM_PREFIXES = ("V_", "Steam_")      # Steam ids: "V_" since Valheim 1.0, "Steam_" before
BTN_PREFIX = "vdm"          # custom_id = "vdm:<action>:<id>", so buttons survive a bot restart


# ---------------------------------------------------------------------------
# The list files
# ---------------------------------------------------------------------------
class ServerLists:
    """Reads and edits Valheim's permittedlist / bannedlist / adminlist.txt.

    One id per line; lines starting with // are comments (the server writes one as a
    header) and are preserved. Files are rewritten in place, not replaced, so they keep
    the owner and mode the server gave them."""

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

    def add(self, which: str, pid: str) -> bool:
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
        with open(self.path(which), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

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
        self.last_notice: dict[str, float] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ready = threading.Event()
        self.client = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        t = threading.Thread(target=self._run, name="discord-admin-bot", daemon=True)
        t.start()
        if not self.ready.wait(60):
            log.warning("admin_bot: not connected to Discord after 60s; will keep trying in the background")

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
        self.last_notice[pid] = now
        if not (self.loop and self.client and self.ready.is_set()):
            log.warning("admin_bot: not connected; dropped join notice for %s (%s)", name, pid)
            return
        fut = asyncio.run_coroutine_threadsafe(self._post_refused(name, pid), self.loop)
        fut.add_done_callback(lambda f: f.exception() and log.warning("admin_bot post failed: %s", f.exception()))

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
            try:
                changes = fn(pid.strip())
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
