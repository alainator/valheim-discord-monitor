"""
Integration with a host-side auto-updater (check_update.sh + cron), for self-hosted
servers. See host/README.md for the host side.

  * Reads the updater's log (update_check.log) and turns its decisions into Discord
    posts: an update is waiting, it's being installed, the checker is failing.
  * Writes status.json with the monitor's live player count, so the updater can use it
    instead of guessing from the last "now N player(s)" log line (which a log rotation
    can wipe, making an occupied server look empty).
  * Drops request files ("check", "restart") that a systemd path unit on the host picks
    up and runs as the valheim user, so the container never needs host privileges.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

log = logging.getLogger("valheim-monitor.updater")

RE_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (?P<msg>.*)$")
RE_LOCAL = re.compile(r"Local build: (?P<local>\d+) \| Remote build: (?P<remote>\d+)")
RE_DETECTED = re.compile(r"Update detected \((?P<old>\d+) -> (?P<new>\d+)\)")
RE_DEFERRED = re.compile(r"Update available but (?P<n>\d+) player\(s\) connected")
RE_RESTARTING = re.compile(r"No players connected\. Restarting to apply update")
RE_NONE = re.compile(r"No update available")
RE_ERROR = re.compile(r"ERROR: (?P<err>.*)")
RE_REQ_CHECK = re.compile(r"Update check requested from Discord")
RE_RESTORE_DONE = re.compile(r"Restore done: (?P<name>\S+) \(the world before it is saved as (?P<safe>\S+)\)")

REQUESTS = ("check", "restart")
RE_SET_REQUEST = re.compile(r"^set (preset|modifier|setkey) [a-z]+( [a-z]+)?$")
RE_RESTORE_REQUEST = re.compile(r"^restore \d{14}$")


def backup_id(name: str) -> Optional[str]:
    """The 14 digits (date and time) after "_backup_" in a backup's name, which is how a
    restore request names it: "Alheim_backup_auto-20260928-170645" -> "20260928170645"."""
    if "_backup_" not in name:
        return None
    digits = re.sub(r"\D", "", name.split("_backup_", 1)[1])
    return digits[:14] if len(digits) >= 14 else None


class UpdateWatcher:
    """Turns update_check.log lines into ("public" | "admin", text) messages."""

    def __init__(self, cfg: dict, clock=time.time):
        self.log_path = cfg.get("log", "")
        self.bot_dir = cfg.get("bot_dir", "")
        self.clock = clock
        self.local: Optional[str] = None
        self.old: Optional[str] = None
        self.new: Optional[str] = None
        self.announced: set = set()          # builds already announced as waiting
        self.check_requested = False
        self.error_posted: dict = {}         # error text -> when it was last posted

    def handle(self, line: str) -> list:
        m = RE_LINE.match(line.strip())
        if not m:
            return []
        msg, out = m.group("msg"), []
        if (m2 := RE_LOCAL.search(msg)):
            self.local = m2.group("local")
        elif (m2 := RE_DETECTED.search(msg)):
            self.old, self.new = m2.group("old"), m2.group("new")
        elif (m2 := RE_DEFERRED.search(msg)):
            if self.new and self.new not in self.announced:
                self.announced.add(self.new)
                n = int(m2.group("n"))
                out.append(("public", f"🆕 A Valheim update is available (build {self.old} → {self.new}). "
                                      f"It installs automatically once everyone has left "
                                      f"({n} player{'s' if n != 1 else ''} online now)."))
            if self.check_requested:
                self.check_requested = False
                out.append(("admin", f"Update check: build {self.new} is waiting for {m2.group('n')} "
                                     f"player(s) to leave. `/odin restart` installs it sooner."))
        elif RE_RESTARTING.search(msg):
            if self.new:
                self.announced.add(self.new)
            out.append(("public", "🔄 Installing the Valheim update"
                                  + (f" (build {self.old} → {self.new})" if self.new else "")
                                  + ". The server is restarting and will be back in a few minutes."))
            self.check_requested = False
        elif RE_NONE.search(msg):
            self.new = None
            if self.check_requested:
                self.check_requested = False
                out.append(("admin", "✅ Update check: no update available"
                                     + (f" (build {self.local})." if self.local else ".")))
        elif RE_REQ_CHECK.search(msg):
            self.check_requested = True
        elif (m2 := RE_RESTORE_DONE.search(msg)):
            out.append(("admin", f"✅ World restored from `{m2.group('name')}`. The world as it was before is kept "
                                 f"as `{m2.group('safe')}`, so `/odin restore` can put it back."))
            out.append(("news", "⏪ The world has been restored from a backup. The server is starting again."))
        elif (m2 := RE_ERROR.search(msg)):
            err, now = m2.group("err"), self.clock()
            if err not in self.error_posted or now - self.error_posted[err] > 6 * 3600:
                self.error_posted[err] = now
                out.append(("admin", f"⚠️ The update checker failed: {err}"))
        return out

    # -- files shared with the host ------------------------------------------
    def _write(self, name: str, text: str) -> None:
        path = os.path.join(self.bot_dir, name)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def write_status(self, count: Optional[int], down: bool) -> None:
        """status.json for check_update.sh. count is null while unknown, so the script
        falls back to its own check; a stale file (monitor stopped) is ignored by age."""
        if not self.bot_dir:
            return
        try:
            self._write("status.json", json.dumps({"count": None if down else count, "down": down,
                                                   "updated_at": int(self.clock())}) + "\n")
        except OSError as e:
            log.debug("status.json not written: %s", e)

    def request(self, action: str) -> Optional[str]:
        """Ask the host helper to act: "check", "restart", or "set <kind> <key> [value]"
        (world settings; the host re-checks every value). Returns an error, or None."""
        if action not in REQUESTS and not RE_SET_REQUEST.match(action) and not RE_RESTORE_REQUEST.match(action):
            return f"unknown request {action!r}"
        if not self.bot_dir or not os.path.isdir(self.bot_dir):
            return "the shared bot folder isn't mounted (updater.bot_dir)"
        if os.path.exists(os.path.join(self.bot_dir, "request")):
            return ("an earlier request hasn't been picked up yet. Is the host helper installed "
                    "(host/README.md)?")
        try:
            self._write("request", action + "\n")
        except OSError as e:
            return f"couldn't write the request: {e}"
        return None

    def pending(self) -> bool:
        return bool(self.bot_dir) and os.path.exists(os.path.join(self.bot_dir, "request"))
