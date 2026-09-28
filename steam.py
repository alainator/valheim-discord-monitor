#!/usr/bin/env python3
"""
Fetch public Valheim achievements from the Steam Web API and store them.

The monitor links each character name to a SteamID64 from the server log's
handshake line, then this module pulls, for each SteamID:
  - profile (persona, avatar, profile URL, visibility)  — GetPlayerSummaries
  - unlocked achievements                                — GetPlayerAchievements
and the game's achievement catalogue once (names/icons) — GetSchemaForGame.

Only public profiles with public game details return achievements; anything
else is stored with a visibility flag so the page can note it. Stdlib only.

Needs a free Steam Web API key (the same STEAM_API_KEY the steamapi source uses).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("valheim-monitor.steam")

APP_ID = 892970  # Valheim
API = "https://api.steampowered.com"
SCHEMA_TTL = 86400  # refresh the achievement catalogue at most once a day


ICON_CDN = "https://shared.fastly.steamstatic.com/community_assets/images/apps"


def icon_url(url: str, app_id: int = APP_ID) -> str:
    """Rebuild an achievement icon URL into the form Steam actually serves today.

    GetSchemaForGame still returns icons under the retired host
    steamcdn-a.akamaihd.net with the old path /steamcommunity/public/images/apps/…,
    which now 404s. The same asset hash is served from
    shared.fastly.steamstatic.com/community_assets/images/apps/<appid>/<hash>.jpg,
    so keep the filename and rebuild the rest. Unrecognised values pass through.
    """
    if not url or not isinstance(url, str):
        return url
    name = url.rsplit("/", 1)[-1].split("?")[0].strip()
    # Only rewrite things that look like Steam's <sha1>.jpg asset names.
    if not name or "." not in name:
        return url
    return f"{ICON_CDN}/{app_id}/{name}"


def _get(path: str, params: dict, timeout: float = 20.0):
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-discord-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


class KeyRejected(Exception):
    """The Steam Web API key itself was refused (revoked, rotated, or wrong)."""


def check_key(key: str) -> None:
    """Cheap pre-flight so a bad key can't be mistaken for everyone going private.

    GetPlayerAchievements answers 403 both for a private profile AND for a bad key, so
    without this every player would be written to the database as "private" the moment
    the key is rotated. Uses a global (non-player) endpoint, which only fails on the key.
    """
    try:
        _get("/ISteamUserStats/GetSchemaForGame/v2/", {"key": key, "appid": APP_ID, "l": "english"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise KeyRejected(f"Steam rejected the API key (HTTP {e.code}). It was probably rotated or "
                              f"revoked - update STEAM_API_KEY or steam.api_key") from None
    except Exception:
        pass  # network blips are not the key's fault; let the normal calls retry


def fetch_schema(key: str) -> list:
    d = _get("/ISteamUserStats/GetSchemaForGame/v2/", {"key": key, "appid": APP_ID, "l": "english"})
    ach = (((d or {}).get("game") or {}).get("availableGameStats") or {}).get("achievements") or []
    for a in ach:  # normalise the CDN URLs before they reach the database
        for k in ("icon", "icongray"):
            if a.get(k):
                a[k] = icon_url(a[k])
    return ach


def fetch_summaries(key: str, steam_ids: list) -> dict:
    out = {}
    for i in range(0, len(steam_ids), 100):
        chunk = steam_ids[i:i + 100]
        d = _get("/ISteamUser/GetPlayerSummaries/v2/", {"key": key, "steamids": ",".join(chunk)})
        for p in ((d or {}).get("response") or {}).get("players", []):
            out[p["steamid"]] = {
                "persona": p.get("personaname"),
                "avatar": p.get("avatarfull") or p.get("avatarmedium"),
                "profile_url": p.get("profileurl"),
                "visibility": p.get("communityvisibilitystate"),
            }
    return out


def fetch_achievements(key: str, steam_id: str):
    """Return (unlocked, total, unlocks[(apiname, unlocktime)], error_or_None)."""
    try:
        d = _get("/ISteamUserStats/GetPlayerAchievements/v1/",
                 {"key": key, "steamid": steam_id, "appid": APP_ID, "l": "english"})
    except urllib.error.HTTPError as e:
        if e.code in (403, 401):
            return None, None, [], "private"
        return None, None, [], f"http_{e.code}"
    except Exception as e:  # noqa: BLE001
        return None, None, [], f"error:{e}"
    ps = (d or {}).get("playerstats") or {}
    if not ps.get("success"):
        return None, None, [], (ps.get("error") or "unavailable")
    ach = ps.get("achievements") or []
    unlocks = [(a["apiname"], int(a.get("unlocktime") or 0)) for a in ach if a.get("achieved")]
    return len(unlocks), len(ach), unlocks, None


def update_all(store, key: str, limit: int = 25, schema_ttl: int = SCHEMA_TTL) -> int:
    """Refresh Steam data for the known SteamIDs. Returns how many profiles updated."""
    if not key:
        return 0
    steam_ids = store.steam_ids_to_update(limit=limit)
    if not steam_ids:
        return 0

    # Verify the key BEFORE touching any player rows (see check_key).
    try:
        check_key(key)
    except KeyRejected as e:
        log.error("Steam refresh aborted: %s", e)
        return 0

    # Achievement catalogue — at most once per schema_ttl.
    last = store.get_meta("steam_schema_at")
    if last is None or time.time() - int(last) > schema_ttl:
        try:
            schema = fetch_schema(key)
            if schema:
                store.save_schema(schema)
        except Exception as e:  # noqa: BLE001
            log.warning("Steam schema fetch failed: %s", e)

    name_by_api = {r["apiname"]: r["name"] for r in
                   (dict(x) for x in store.conn.execute("SELECT apiname, name FROM steam_schema"))}

    try:
        summaries = fetch_summaries(key, steam_ids)
    except Exception as e:  # noqa: BLE001
        log.warning("Steam summaries fetch failed: %s", e)
        summaries = {}

    updated = 0
    for sid in steam_ids:
        prof = summaries.get(sid, {})
        unlocked, total, unlocks, err = fetch_achievements(key, sid)
        if err and (err.startswith("error:") or err == "http_429" or err.startswith("http_5")):
            # A network blip or Steam outage: keep the counts we already have.
            store.save_profile(sid, error=err)
            updated += 1
            time.sleep(0.4)
            continue
        last_at, last_name = None, None
        if unlocks:
            a, last_at = max(unlocks, key=lambda x: x[1])
            last_name = name_by_api.get(a, a)
            store.save_unlocks(sid, unlocks)
        fields = dict(unlocked=unlocked, total=total, last_unlock_at=last_at,
                      last_unlock_name=last_name, error=err)
        if prof:  # a failed summaries call must not blank out a persona/avatar we already have
            fields.update(persona=prof.get("persona"), avatar=prof.get("avatar"),
                          profile_url=prof.get("profile_url"), visibility=prof.get("visibility"))
        store.save_profile(sid, **fields)
        updated += 1
        time.sleep(0.4)  # be gentle with the API
    log.info("Steam: refreshed %d profile(s)", updated)
    return updated


if __name__ == "__main__":
    import argparse, os, stats_db
    ap = argparse.ArgumentParser(description="Refresh Steam achievements into the stats DB.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--key", default=os.environ.get("STEAM_API_KEY"))
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not a.key:
        raise SystemExit("Set --key or STEAM_API_KEY")
    st = stats_db.Store(a.db)
    n = update_all(st, a.key, a.limit)
    print(f"updated {n} profiles")
    for r in stats_db.achievements_leaderboard(st.conn, 20):
        print(f"  {r.get('persona') or r['steam_id']}: {r['unlocked']}/{r['total']} "
              f"({r.get('error') or 'ok'})  names={r.get('names')}")
