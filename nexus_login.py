#!/usr/bin/env python3
"""
Obtain a LOW.MS Nexus panel API token by signing in with YOUR OWN panel account
through a headless browser, the same way the panel's Console tab does.

The panel authenticates with Auth0; its API accepts only the short-lived session
token the panel holds, not the public `lowms_…` API keys. This script:

  1. opens https://panel.low.ms in headless Chromium (persistent profile, so the
     Auth0 session cookie survives between runs and most refreshes are silent),
  2. signs in with your email/password if the login page appears,
  3. captures the bearer token from the panel's first API request,
  4. caches it (with its expiry) in ~/.valheim-monitor/nexus_token.json.

Requires:  pip install playwright && python -m playwright install --with-deps chromium

Credentials come from config.json  ("source": {"login": {"email": …, "password": …}})
or the environment (NEXUS_EMAIL / NEXUS_PASSWORD). Keep them on the machine that
runs the monitor; never commit them.

Standalone check:
    NEXUS_EMAIL=you@example.com NEXUS_PASSWORD=… python nexus_login.py --server-id <uuid> --check
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("nexus-login")

PANEL = "https://panel.low.ms"
API_HOST = "api.prod.nexus.low.ms"
STATE_DIR = Path(os.environ.get("VALHEIM_MONITOR_STATE", Path.home() / ".valheim-monitor"))

# Auth0 Universal Login (classic and "new" experience). Tried in order; override
# via "login": {"selectors": {...}} in config if LOW.MS customises the page.
DEFAULT_SELECTORS = {
    "email": ['input[name="username"]', 'input[name="email"]', 'input[type="email"]', "#username", "#email"],
    "password": ['input[name="password"]', 'input[type="password"]', "#password"],
    "submit": ['button[type="submit"]', 'button[name="action"]', 'input[type="submit"]'],
}


def jwt_exp(token: str) -> Optional[float]:
    """Expiry (unix seconds) from a JWT's payload, or None if it isn't a JWT."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:
        return None


PLACEHOLDER_HINTS = ("your@email", "you@example.com", "yourpassword", "your-panel-password",
                     "changeme", "paste", "example.com")


def check_credentials(email, password):
    """Fail loudly on placeholder credentials.

    A placeholder email is rejected by Auth0 as malformed, so the sign-in never reaches
    a password field and the monitor just retries forever with a confusing
    "no password field found" error. Catch it here and say what to fix instead.
    """
    e, pw = (email or "").strip(), (password or "").strip()
    if not e or not pw:
        raise RuntimeError("nexus login needs an email and password: set NEXUS_EMAIL / NEXUS_PASSWORD, "
                           "or source.login in config.json (or switch to the 'lowms' source, which needs "
                           "no panel login at all)")
    low = e.lower()
    if "@" not in low or "." not in low.split("@")[-1] or any(h in low for h in PLACEHOLDER_HINTS) \
            or any(h in pw.lower() for h in PLACEHOLDER_HINTS):
        raise RuntimeError(f"nexus login credentials are still placeholders (email={e!r}). Auth0 rejects "
                           "that as an invalid address, so the sign-in never reaches the password step. "
                           "Put your real panel login in NEXUS_EMAIL / NEXUS_PASSWORD or source.login, "
                           "or switch to the 'lowms' source, which needs no panel login at all")


class TokenCache:
    def __init__(self, server_id: str, email: str, password: str, selectors: Optional[dict] = None,
                 headless: bool = True, login_timeout: float = 90.0, cache_path: Optional[Path] = None):
        self.server_id, self.email, self.password = server_id, email, password
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.headless, self.login_timeout = headless, login_timeout
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)   # token + browser session cookies
        self.cache_path = cache_path or STATE_DIR / "nexus_token.json"
        self.profile_dir = STATE_DIR / "browser-profile"
        self._token: Optional[str] = None
        self._exp: Optional[float] = None
        # The log poller and the maintenance thread share this cache; the lock stops them
        # both launching a browser sign-in when the token expires at the same moment.
        self._lock = threading.RLock()
        self._load()

    # -- cache -------------------------------------------------------------
    def _load(self):
        try:
            d = json.loads(self.cache_path.read_text())
            self._token, self._exp = d.get("token"), d.get("exp")
        except Exception:
            pass

    def _save(self):
        # Created 0600 from the start (not chmod'ed afterwards): it holds a bearer token.
        fd = os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"token": self._token, "exp": self._exp, "saved": time.time()}))
        try:
            os.chmod(self.cache_path, 0o600)   # an older cache file may have looser permissions
        except OSError:
            pass

    def invalidate(self):
        with self._lock:
            self._token = self._exp = None
            self._save()

    def get(self) -> str:
        with self._lock:
            if self._token and (self._exp is None or self._exp - 60 > time.time()):
                return self._token
            log.info("Nexus token missing or expiring; signing in to the panel")
            self._token = self.login()
            self._exp = jwt_exp(self._token)
            self._save()
            return self._token

    # -- browser -----------------------------------------------------------
    def login(self) -> str:
        check_credentials(self.email, self.password)
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            # Not sys.exit: this can run inside the monitor's loop, and must not kill it.
            raise RuntimeError("nexus login needs Playwright:  pip install playwright && "
                               "python -m playwright install --with-deps chromium") from None

        captured: dict = {}
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(self.profile_dir), headless=self.headless,
                                                       viewport={"width": 1280, "height": 900})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def on_request(req):
                if API_HOST in req.url and "value" not in captured:
                    auth = req.headers.get("authorization", "")
                    if auth.lower().startswith("bearer "):
                        captured["value"] = auth.split(" ", 1)[1].strip()

            page.on("request", on_request)
            page.goto(f"{PANEL}/account/servers/{self.server_id}/console", wait_until="domcontentloaded")

            deadline = time.time() + self.login_timeout
            attempted_login = False
            while time.time() < deadline and "value" not in captured:
                if "auth.low.ms" in page.url and not attempted_login:
                    attempted_login = True
                    try:
                        self._fill_login(page)
                    except Exception as e:  # keep going; maybe we were mid-redirect
                        detail = ""
                        try:  # Auth0 shows the reason on the page ("Email is not valid." etc.)
                            for sel in ("[role=alert]", ".ulp-input-error-message", "#error-element-username",
                                        "#error-element-password", ".error-cloud"):
                                for el in page.query_selector_all(sel):
                                    t = (el.inner_text() or "").strip()
                                    if t and t not in detail:
                                        detail += ("; " if detail else "") + t
                        except Exception:
                            pass
                        log.warning("Login form handling failed: %s%s", e,
                                    f"  [page says: {detail}]" if detail else "")
                page.wait_for_timeout(500)

            if "value" not in captured:
                self._dump_failure(page)
                ctx.close()
                raise RuntimeError(f"Could not obtain a panel token within {self.login_timeout:.0f}s "
                                   f"(last URL: {page.url}). See {STATE_DIR}/login-failure.png")
            ctx.close()
        log.info("Obtained panel token (expires %s)",
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(jwt_exp(captured["value"]) or 0)))
        return captured["value"]

    def _first_visible(self, page, candidates, timeout=8000):
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:
                continue
        return None

    def _fill_login(self, page):
        log.info("Login page detected; submitting credentials")
        email = self._first_visible(page, self.selectors["email"])
        if email is None:
            raise RuntimeError("no email/username field found on login page")
        email.fill(self.email)

        password = self._first_visible(page, self.selectors["password"], timeout=1500)
        if password is None:
            # Identifier-first flow: submit the email, then the password page appears.
            btn = self._first_visible(page, self.selectors["submit"])
            if btn:
                btn.click()
            password = self._first_visible(page, self.selectors["password"], timeout=10000)
            if password is None:
                raise RuntimeError("no password field found after submitting email")
        password.fill(self.password)
        btn = self._first_visible(page, self.selectors["submit"])
        if btn:
            btn.click()
        else:
            password.press("Enter")

    def _dump_failure(self, page):
        try:
            page.screenshot(path=str(STATE_DIR / "login-failure.png"), full_page=True)
            (STATE_DIR / "login-failure.html").write_text(page.content())
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server-id", required=True, help="Server UUID from the panel URL")
    ap.add_argument("--check", action="store_true", help="Sign in, fetch 3 console lines, print them")
    ap.add_argument("--headed", action="store_true", help="Show the browser (needs a display)")
    ap.add_argument("--force", action="store_true", help="Ignore the cached token")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    email, password = os.environ.get("NEXUS_EMAIL"), os.environ.get("NEXUS_PASSWORD")
    if not (email and password):
        sys.exit("Set NEXUS_EMAIL and NEXUS_PASSWORD in the environment for a standalone check")
    tc = TokenCache(args.server_id, email, password, headless=not args.headed)
    if args.force:
        tc.invalidate()
    token = tc.get()
    print(f"token ok, expires {time.strftime('%Y-%m-%d %H:%M', time.localtime(jwt_exp(token) or 0))}")
    if args.check:
        import urllib.request
        url = f"https://{API_HOST}/user/servers/{args.server_id}/daemon/console?lines=3"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in json.load(r).get("lines", []):
                print("  ", line)


if __name__ == "__main__":
    main()
