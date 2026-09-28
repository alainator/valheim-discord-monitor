#!/usr/bin/env python3
"""
Unattended game updates and nightly backups for a LOW.MS Valheim server.

Runs inside the monitor. Every `check_interval_seconds` (15 min by default) it asks:

  1. Is the server EMPTY right now?  Only if the server's own "Connections N" line
     (Valheim writes one every 10 minutes) is recent, reads 0, nobody has logged in
     since, and the monitor tracks nobody online. A stale or missing count is treated
     as "not empty" -- a dropped log feed must never look like an empty server.
  2. If empty:
       - backup: inside the nightly window (2-6 AM server time) and not yet done today
         -> stop the server, back up, (update if one is waiting), start it again.
       - update: the panel reports an update waiting -> install it.

Which API does what (see api.prod.nexus.low.ms/v1/docs):

  Public API (lowms_ key)  backups, power (start/stop), jobs, server status
  Panel session            update-info and update_server -- the public API has no update
                           endpoint, so these use the same private endpoints as the panel's
                           own "Update" button, via the monitor's signed-in panel session.

The run happens on a background thread so the monitor keeps tailing the log (a boot
writes enough lines to scroll past the console window). While it runs, the usual
"restarting / back online / offline" Discord posts are replaced by one maintenance line.

Stdlib only.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

log = logging.getLogger("valheim-monitor.maintenance")

API_BASE = "https://api.prod.nexus.low.ms"
DONE = {"COMPLETED", "FAILED", "CANCELLED"}
# The panel's own per-action timeouts (seconds), read from its job-watcher code.
JOB_TIMEOUT = {"stop": 600, "start": 300, "restart": 300, "backup": 3600, "update_server": 1800}
SUPPRESSED_KINDS = {"server_restart", "server_online", "server_offline"}


class ApiError(Exception):
    def __init__(self, status: int, code: str = "", message: str = ""):
        super().__init__(f"HTTP {status} {code}: {message}".strip())
        self.status, self.code, self.message = status, code, message


def _http(method: str, url: str, headers: dict, body=None, timeout: float = 30.0):
    data = None
    headers = {"Accept": "application/json", "User-Agent": "valheim-discord-monitor", **headers}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        code, msg = "", raw[:300]
        try:  # the public API's error envelope: {"error": {"code": ..., "message": ...}}
            err = json.loads(raw).get("error") or {}
            code, msg = err.get("code", ""), err.get("message", msg)
        except Exception:
            pass
        raise ApiError(e.code, code, msg) from None


def _unwrap_job(resp) -> Optional[dict]:
    """Find the job object in a response of unknown shape (the panel's isn't documented)."""
    if not isinstance(resp, dict):
        return None
    for cand in (resp.get("job"), (resp.get("data") or {}) if isinstance(resp.get("data"), dict) else None, resp):
        if isinstance(cand, dict) and cand.get("id") and ("status" in cand or "type" in cand):
            return cand
    if isinstance(resp.get("data"), dict) and isinstance(resp["data"].get("job"), dict):
        return resp["data"]["job"]
    return None


def _as_list(resp) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("data", "jobs", "items", "results"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------
class PublicAPI:
    """Documented LOW.MS public API, authenticated with a lowms_ key."""

    def __init__(self, key: str, server_id: str, base: str = API_BASE):
        self.key, self.sid, self.base = key, server_id, base.rstrip("/")

    def _req(self, method, path, body=None):
        return _http(method, self.base + path, {"Authorization": f"Bearer {self.key}"}, body)

    def me(self):
        return self._req("GET", "/v1/me")

    def server(self):
        return self._req("GET", f"/v1/servers/{self.sid}")

    def power(self, action: str) -> dict:
        return _unwrap_job(self._req("POST", f"/v1/servers/{self.sid}/power", {"action": action})) or {}

    def create_backup(self) -> tuple[dict, dict]:
        r = self._req("POST", f"/v1/servers/{self.sid}/backups")
        return (r.get("backup") or {}), (r.get("job") or {})

    def list_backups(self, limit: int = 50) -> list:
        return _as_list(self._req("GET", f"/v1/servers/{self.sid}/backups?limit={limit}"))

    def job(self, job_id: str) -> dict:
        r = self._req("GET", f"/v1/jobs/{job_id}")
        return _unwrap_job(r) or r


class PanelAPI:
    """The panel's own (undocumented) endpoints, using the monitor's signed-in session.
    Only the calls the public API lacks: update status, update install, backup delete."""

    def __init__(self, token_cache, server_id: str, base: str = API_BASE):
        self.tc, self.sid, self.base = token_cache, server_id, base.rstrip("/")

    def _req(self, method, path, body=None):
        try:
            return _http(method, self.base + path, {"Authorization": f"Bearer {self.tc.get()}"}, body)
        except ApiError as e:
            if e.status in (401, 403):  # session expired: sign in again once
                log.info("Panel session rejected (%s); signing in again", e.status)
                self.tc.invalidate()
                return _http(method, self.base + path, {"Authorization": f"Bearer {self.tc.get()}"}, body)
            raise

    def update_info(self) -> dict:
        # -> {updateState: "available"|"current"|..., installedBuildId, installedVersion, ...}
        return self._req("GET", f"/user/servers/{self.sid}/update-info") or {}

    def update_server(self, stop_first: bool = True) -> Optional[dict]:
        # Exactly what the panel's Update button sends.
        return _unwrap_job(self._req("POST", f"/servers/{self.sid}/actions/update_server",
                                     {"stopFirst": stop_first}))

    def jobs(self, limit: int = 20) -> list:
        return _as_list(self._req("GET", f"/user/servers/{self.sid}/jobs?limit={limit}"))

    def delete_backup(self, backup_id: str):
        return self._req("DELETE", f"/user/servers/{self.sid}/backups/{backup_id}")


# ---------------------------------------------------------------------------
# Presence: is the server empty?
# ---------------------------------------------------------------------------
class Presence:
    """Fed every parsed event by the monitor's main loop; answers 'is it empty?'."""

    def __init__(self, max_age: float, clock: Callable[[], float] = time.time):
        self.max_age, self.clock = max_age, clock
        self.count_at: Optional[float] = None
        self.count: Optional[int] = None
        self.login_at: Optional[float] = None
        self.tracked_online = 0

    def observe(self, ev, tracked_online: int) -> None:
        now = self.clock()
        self.tracked_online = tracked_online
        if ev.kind == "count" and ev.extra.get("count") is not None:
            self.count_at, self.count = now, int(ev.extra["count"])
        elif ev.kind in ("login", "respawn"):
            self.login_at = now

    def empty(self) -> tuple[bool, str]:
        now = self.clock()
        if self.count_at is None:
            return False, "no player count seen yet since the monitor started"
        age = now - self.count_at
        if age > self.max_age:
            return False, f"player count is stale ({age / 60:.0f} min old)"
        if self.count != 0:
            return False, f"{self.count} player(s) connected"
        if self.login_at is not None and self.login_at >= self.count_at:
            return False, "a player logged in after the last count"
        if self.tracked_online:
            return False, f"{self.tracked_online} player(s) tracked online"
        return True, "empty"


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class Maintenance:
    def __init__(self, cfg: dict, public: PublicAPI, panel: Optional[PanelAPI], notify: Callable[[str, str], None],
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 state_path: Optional[str] = None):
        self.cfg = cfg
        self.pub, self.panel, self.notify = public, panel, notify
        self.clock, self.sleep = clock, sleep
        self.interval = float(cfg.get("check_interval_seconds", 900))
        self.poll = float(cfg.get("job_poll_seconds", 10))
        self.dry_run = bool(cfg.get("dry_run", False))
        # How long after a run to wait for its boot line before normal alerts resume.
        self.quiet_seconds = float(cfg.get("post_run_quiet_seconds", 900))
        tzname = cfg.get("timezone", "America/Los_Angeles")
        self.tz = ZoneInfo(tzname) if ZoneInfo else None
        b = cfg.get("backup") or {}
        self.backup_enabled = bool(b.get("enabled", True))
        self.window = self._parse_window(b.get("window", "02:00-06:00"))
        self.stop_for_backup = bool(b.get("stop_server", True))
        self.prune = bool(b.get("delete_oldest_when_full", True))
        u = cfg.get("update") or {}
        self.update_enabled = bool(u.get("enabled", True)) and panel is not None
        # The check is hourly and runs regardless of who is online; the INSTALL waits for
        # an empty server. `settle` guards against acting on a momentary gap -- crossplay
        # players drop and reconnect, and we don't want to restart under someone's feet.
        self.update_interval = float(u.get("check_interval_seconds", 3600))
        self.settle = float(u.get("empty_settle_seconds", cfg.get("empty_settle_seconds", 180)))
        self.retry_cooldown = float(u.get("retry_cooldown_seconds", 3600))
        self.last_update_check = 0.0
        self.pending_update: Optional[dict] = None
        self.update_retry_after = 0.0
        self.empty_since: Optional[float] = None
        self.presence = Presence(float(cfg.get("count_max_age_seconds", 780)), clock)
        self.state_path = Path(state_path or cfg.get("state_file", "maintenance_state.json"))
        self.state = self._load()
        self.last_check = 0.0
        self.thread: Optional[threading.Thread] = None
        self.active = False
        self.quiet_until = 0.0
        self.boot_seen = False
        self.swallow_online = False

    # -- config / state ----------------------------------------------------
    @staticmethod
    def _parse_window(s: str):
        a, b = s.split("-")
        t = lambda x: _dt.time(*map(int, x.strip().split(":")))  # noqa: E731
        return t(a), t(b)

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save(self):
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1))
            tmp.replace(self.state_path)
        except Exception as e:
            log.warning("Could not save maintenance state: %s", e)

    def local_now(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.clock(), self.tz)

    def in_window(self) -> bool:
        t = self.local_now().time()
        a, b = self.window
        return a <= t < b if a <= b else (t >= a or t < b)

    def window_date(self) -> str:
        """The date the most recent backup window opened: "tonight" for a window that
        crosses midnight (23:00-03:00) stays the same night after 00:00."""
        now = self.local_now()
        d = now.date() if now.time() >= self.window[0] else now.date() - _dt.timedelta(days=1)
        return d.isoformat()

    def backup_due(self) -> bool:
        return (self.backup_enabled and self.in_window()
                and self.state.get("last_backup_date") != self.window_date())

    # -- hooks from the monitor's main loop ------------------------------
    def observe(self, ev, tracked_online: int) -> None:
        self.presence.observe(ev, tracked_online)
        if ev.kind == "server_online":
            if self.active:
                self.boot_seen = True           # the run's own restart; stays quiet
            elif self.quiet_until > self.clock():
                # The run's boot line arrived after the run finished: swallow exactly this
                # one "back online", then return to normal alerts immediately.
                self.swallow_online, self.quiet_until = True, 0.0

    def suppressing(self, kind: str) -> bool:
        if kind == "server_online" and self.swallow_online:
            self.swallow_online = False
            return True
        return kind in SUPPRESSED_KINDS and (self.active or self.clock() < self.quiet_until)

    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def check_for_update(self) -> Optional[dict]:
        """Ask the panel whether an update is waiting. Safe to call while players are on:
        it is a read-only call, and nothing is installed until the server is empty."""
        if not self.update_enabled:
            return None
        try:
            info = self.panel.update_info()
        except Exception as e:  # noqa: BLE001
            log.warning("Update check failed: %s", e)
            return self.pending_update
        state = info.get("updateState")
        installed = info.get("installedVersion") or info.get("installedBuildId")
        if state == "available":
            if not self.pending_update:
                ver = info.get("latestVersion") or info.get("latestBuildId") or "a new build"
                log.info("Update available (%s); will install once the server is empty", ver)
                # Only announce the wait if it actually has to wait -- when the server is
                # already empty the install starts moments later and says so itself.
                if not self.presence.empty()[0]:
                    self.notify("maintenance_pending", f"a game update ({ver}) is waiting — it will install "
                                                       f"as soon as nobody is playing")
            self.pending_update = info
        else:
            if self.pending_update:
                log.info("Update no longer pending (state %s, installed %s)", state, installed)
            self.pending_update = None
            log.debug("Update check: state %s (installed %s)", state, installed)
        return self.pending_update

    def tick(self) -> Optional[str]:
        """Called every loop (~every poll). Cheap: the only API call is the hourly
        update check. Returns a short reason string (for logs/tests)."""
        now = self.clock()
        if self.busy():
            return "running"

        # 1. Hourly update check — runs whether or not anyone is playing.
        if self.update_enabled and now - self.last_update_check >= self.update_interval:
            self.last_update_check = now
            self.check_for_update()

        # 2. Is the server empty, and has it been empty long enough to act?
        ok, why = self.presence.empty()
        if not ok:
            self.empty_since = None
            if now - self.last_check >= self.interval:
                self.last_check = now
                log.info("Maintenance: waiting (%s)%s", why,
                         "; an update is queued" if self.pending_update else "")
            return f"skip: {why}"
        if self.empty_since is None:
            self.empty_since = now
        settled = now - self.empty_since >= self.settle
        self.last_check = now

        do_backup = self.backup_due()
        update = self.pending_update if (settled and now >= self.update_retry_after) else None
        if do_backup and not settled:
            do_backup = False
        if not do_backup and not update:
            return "idle" if settled else "settling"
        plan = (["backup"] if do_backup else []) + (["update"] if update else [])
        if self.dry_run:
            log.info("DRY RUN: would run %s now", " + ".join(plan))
            if do_backup:  # don't repeat the dry-run backup every 15 min
                self.state["last_backup_date"] = self.window_date()
                self._save()
            return "dry-run: " + "+".join(plan)
        self.active, self.boot_seen = True, False
        self.thread = threading.Thread(target=self._run, args=(do_backup, update), name="maintenance", daemon=True)
        self.thread.start()
        return "started: " + "+".join(plan)

    # -- the run -------------------------------------------------------------
    def _wait(self, job: dict, kind: str, getter: Optional[Callable[[str], dict]] = None) -> dict:
        """Poll a job until it finishes. Raises on FAILED/CANCELLED/timeout."""
        jid = job.get("id")
        if not jid:
            raise RuntimeError(f"{kind}: no job id returned")
        getter = getter or self.pub.job
        deadline = self.clock() + JOB_TIMEOUT.get(kind, 1800)
        status = job.get("status")
        while status not in DONE:
            if self.clock() > deadline:
                raise RuntimeError(f"{kind} job {jid} still {status} after {JOB_TIMEOUT.get(kind)}s")
            self.sleep(self.poll)
            try:
                job = getter(jid)
            except ApiError as e:
                if e.status == 429 or e.status >= 500:
                    self.sleep(30)
                    continue
                raise
            except OSError as e:          # URLError, timeouts: a blip must not abandon a running job
                log.warning("%s job %s: status check failed (%s); retrying", kind, jid, e)
                continue
            status = job.get("status")
        if status != "COMPLETED":
            raise RuntimeError(f"{kind} job {status}: {job.get('error') or 'no detail'}")
        return job

    def _panel_job(self, jid: str) -> dict:
        """Look a job up via the public API, falling back to the panel's job list."""
        try:
            return self.pub.job(jid)
        except ApiError as e:
            if e.status not in (403, 404) or not self.panel:
                raise
        for j in self.panel.jobs():
            if j.get("id") == jid:
                return j
        raise RuntimeError(f"job {jid} not found")

    def _power(self, action: str):
        try:
            self._wait(self.pub.power(action), action)
        except ApiError as e:
            if e.status == 409 and "state" in (e.message or "").lower():
                return  # already stopped/started
            if e.status == 409 and action == "start":
                return  # another job (e.g. the update) is bringing it up
            raise

    def _backup(self) -> dict:
        try:
            backup, job = self.pub.create_backup()
        except ApiError as e:
            if e.status != 409 or not (self.prune and self.panel) or "flight" in (e.message or "").lower():
                raise
            # Allowance used up: delete the oldest unpinned backup and try once more.
            done = [b for b in self.pub.list_backups(100)
                    if not b.get("pinned") and (b.get("status") or "").upper() in ("COMPLETED", "READY", "SUCCEEDED")]
            if not done:
                raise
            oldest = min(done, key=lambda b: b.get("createdAt") or "")
            log.info("Backup allowance full; deleting oldest unpinned backup %s (%s)", oldest.get("id"),
                     oldest.get("createdAt"))
            self.panel.delete_backup(oldest["id"])
            backup, job = self.pub.create_backup()
        self._wait(job, "backup")
        return backup

    def _run(self, do_backup: bool, update: Optional[dict]):
        stopped = False
        parts, problems = [], []
        try:
            what = " and ".join((["nightly backup"] if do_backup else []) +
                                ([f"game update ({update.get('latestVersion') or update.get('latestBuildId') or 'new build'})"]
                                 if update else []))
            self.notify("maintenance_start", what)
            if do_backup:
                try:
                    if self.stop_for_backup:
                        stopped = True  # set first: a stop that fails half-way must still get a start
                        self._power("stop")
                    b = self._backup()
                    self.state["last_backup_date"] = self.window_date()
                    self.state["last_backup_at"] = int(self.clock())
                    self._save()
                    size = b.get("sizeBytes")
                    parts.append("backup saved" + (f" ({size / 1e6:.0f} MB)" if size else ""))
                except Exception as e:
                    log.warning("Backup failed: %s", e)
                    problems.append(f"backup failed: {e}")
                    # Don't retry every 15 minutes tonight; one alert per night is enough.
                    self.state["last_backup_date"] = self.window_date()
                    self._save()
            if update:
                try:
                    job = self.panel.update_server(stop_first=True)
                    if job:
                        self._wait(job, "update_server", getter=self._panel_job)
                    else:
                        self._wait_for_panel_update_job()
                    info = self.panel.update_info()
                    self.state["last_update_at"] = int(self.clock())
                    self._save()
                    ver = info.get("installedVersion") or info.get("installedBuildId")
                    parts.append("updated" + (f" to {ver}" if ver else ""))
                    self.pending_update = None
                    self.last_update_check = 0.0   # re-check soon to confirm it took
                    stopped = True  # make sure it is running afterwards, whatever update_server did
                except Exception as e:
                    log.warning("Update failed: %s", e)
                    problems.append(f"update failed: {e}")
                    # Back off instead of retrying the moment the loop comes round again.
                    self.update_retry_after = self.clock() + self.retry_cooldown
                    stopped = True
        finally:
            if stopped:
                try:
                    self._power("start")
                except Exception as e:
                    log.error("Could not start the server after maintenance: %s", e)
                    problems.append(f"SERVER NOT RESTARTED: {e}")
            # If the boot line hasn't shown up yet, keep quiet a while so it doesn't post
            # "back online"; if it has, return to normal alerts straight away.
            self.quiet_until = self.clock() + (0 if self.boot_seen else self.quiet_seconds)
            self.active = False
            if problems:
                self.notify("maintenance_failed", "; ".join(parts + problems))
            else:
                self.notify("maintenance_done", "; ".join(parts) or "nothing to do")

    def _wait_for_panel_update_job(self):
        """The action gave no job back: find the newest update_server job and wait on it."""
        for _ in range(6):
            jobs = [j for j in self.panel.jobs() if j.get("type") == "update_server"]
            if jobs:
                newest = max(jobs, key=lambda j: j.get("createdAt") or "")
                return self._wait(newest, "update_server", getter=self._panel_job)
            self.sleep(self.poll)
        raise RuntimeError("update requested but no update job appeared")

    # -- read-only report for --maintenance-check ---------------------------
    def report(self) -> list[str]:
        out = []
        try:
            me = self.pub.me()
            key = me.get("key") or {}
            out.append(f"API key '{key.get('name')}' scopes: {', '.join(key.get('scopes') or [])}")
            need = {"backups:read", "backups:write", "servers:power"}
            missing = need - set(key.get("scopes") or [])
            if missing:
                out.append(f"  MISSING scopes: {', '.join(sorted(missing))}")
        except Exception as e:
            out.append(f"API key check FAILED: {e}")
        try:
            s = self.pub.server()
            out.append(f"Server: {s.get('name')} status={s.get('status')}")
        except Exception as e:
            out.append(f"Server lookup FAILED: {e}")
        try:
            bs = self.pub.list_backups(100)
            pinned = sum(1 for b in bs if b.get("pinned"))
            newest = bs[0].get("createdAt") if bs else None
            out.append(f"Backups: {len(bs)} (pinned {pinned}); newest {newest}")
        except Exception as e:
            out.append(f"Backup list FAILED: {e}")
        if self.panel:
            try:
                i = self.panel.update_info()
                out.append(f"Update: state={i.get('updateState')} installed={i.get('installedVersion') or i.get('installedBuildId')}")
            except Exception as e:
                out.append(f"Update check FAILED: {e}")
        else:
            out.append("Update: disabled (needs the nexus panel login)")
        out.append(f"Server time now: {self.local_now():%Y-%m-%d %H:%M %Z}; backup window "
                   f"{self.window[0]:%H:%M}-{self.window[1]:%H:%M}; in window: {self.in_window()}; "
                   f"last backup: {self.state.get('last_backup_date')}")
        return out
