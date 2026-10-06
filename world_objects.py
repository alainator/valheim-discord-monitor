#!/usr/bin/env python3
"""
Portals and tombstones, read from the world save. No mods.

Valheim 1.0 keeps a world's objects ("ZDOs") in uncompressed worlds_local/<world>/*.chunk
files next to _main.<N>.db2 (older servers: inside <world>.db). Each object is stored as
its position (3 floats), its prefab's hash, then its data, where strings are a key hash, a
7-bit length and UTF-8. So:

  * a portal is the portal prefab's hash, with its name under the "tag" key;
  * a tombstone is Player_tombstone's hash, with "ownerName" and "timeOfDeath" (game time
    in 100 ns ticks). It disappears once its owner empties it, so every tombstone in the
    save is one still lying out there.

Run it on its own to check a save: python3 world_objects.py /path/to/valheim_save_data
"""

from __future__ import annotations

import glob
import math
import os
import struct
import sys
from typing import Optional


def stable_hash(text: str) -> int:
    """Valheim's string hash (Utils.GetStableHashCode), as an unsigned 32-bit number."""
    a = b = 5381
    for i in range(0, len(text), 2):
        a = (((a << 5) + a) ^ ord(text[i])) & 0xFFFFFFFF
        if i == len(text) - 1:
            break
        b = (((b << 5) + b) ^ ord(text[i + 1])) & 0xFFFFFFFF
    return (a + b * 1566083941) & 0xFFFFFFFF


def _key(text: str) -> bytes:
    return struct.pack("<I", stable_hash(text))


PORTALS = {_key("portal_wood"): "portal", _key("portal_stone"): "stone portal", _key("portal"): "stone portal"}
TOMBSTONE = _key("Player_tombstone")
TAG, OWNER_NAME, TIME_OF_DEATH = _key("tag"), _key("ownerName"), _key("timeOfDeath")
WINDOW = 512                     # bytes after the prefab hash to look for an object's data


def _position(data: bytes, at: int) -> Optional[tuple]:
    """The 3 floats before a prefab hash, if they look like a place in the world."""
    if at < 12:
        return None
    x, y, z = struct.unpack_from("<3f", data, at - 12)
    if not all(math.isfinite(v) for v in (x, y, z)) or abs(x) > 15000 or abs(z) > 15000 or abs(y) > 5000:
        return None
    return x, y, z


def _string(data: bytes, start: int, end: int, key: bytes) -> Optional[str]:
    """The string stored under `key` between start and end, or None."""
    i = data.find(key, start, end)
    if i < 0:
        return None
    p, n, shift = i + 4, 0, 0
    while p < len(data) and shift < 35:              # 7-bit encoded length
        byte = data[p]
        n |= (byte & 0x7F) << shift
        p += 1
        shift += 7
        if not byte & 0x80:
            break
    if n > 200 or p + n > len(data):
        return None
    try:
        return data[p:p + n].decode("utf-8")
    except UnicodeDecodeError:
        return None


def _long(data: bytes, start: int, end: int, key: bytes) -> Optional[int]:
    i = data.find(key, start, end)
    if i < 0 or i + 12 > len(data):
        return None
    return struct.unpack_from("<q", data, i + 4)[0]


def _hits(data: bytes, key: bytes):
    i = data.find(key)
    while i >= 0:
        yield i
        i = data.find(key, i + 1)


def scan_bytes(data: bytes) -> dict:
    """{"portals": [...], "tombstones": [...]} found in one file's bytes."""
    portal_at = sorted((i, kind) for key, kind in PORTALS.items() for i in _hits(data, key))
    portals = []
    for n, (i, kind) in enumerate(portal_at):
        pos = _position(data, i)
        if not pos:
            continue
        # Stop at the next portal, so a portal without a name never takes its neighbour's.
        end = min(i + WINDOW, portal_at[n + 1][0] if n + 1 < len(portal_at) else len(data))
        tag = _string(data, i + 4, end, TAG)
        portals.append({"kind": kind, "tag": (tag or "").strip(), "x": pos[0], "y": pos[1], "z": pos[2]})
    tombstones = []
    for i in _hits(data, TOMBSTONE):
        pos = _position(data, i)
        owner = _string(data, i + 4, i + WINDOW, OWNER_NAME) if pos else None
        if not owner:
            continue
        ticks = _long(data, i + 4, i + WINDOW, TIME_OF_DEATH)
        died = ticks / 1e7 if ticks and 0 < ticks < 10 ** 17 else None
        tombstones.append({"owner": owner, "died": died, "x": pos[0], "y": pos[1], "z": pos[2]})
    return {"portals": portals, "tombstones": tombstones}


def object_files(save_dir: str, world: Optional[str] = None) -> list:
    """The files holding the live world's objects: its .chunk files (Valheim 1.0), or the
    older <world>.db."""
    import bosses
    save = bosses.world_save(save_dir, world)
    if not save:
        return []
    if save.endswith(".db2"):
        return sorted(glob.glob(os.path.join(os.path.dirname(save), "*.chunk")))
    return [save]


def scan(save_dir: str, world: Optional[str] = None) -> Optional[dict]:
    """Every portal and tombstone in the live world, or None without a save."""
    files = object_files(save_dir, world)
    if not files:
        return None
    out = {"portals": [], "tombstones": []}
    for path in files:
        try:
            with open(path, "rb") as f:
                found = scan_bytes(f.read())
        except OSError:
            continue
        out["portals"] += found["portals"]
        out["tombstones"] += found["tombstones"]
    return out


# -- what to show -----------------------------------------------------------------------
def where(x: float, z: float) -> str:
    """ "x 2014, z −1171 · 2.3 km SE of the start" (the start is near 0, 0)."""
    dist = math.hypot(x, z)
    if dist < 150:
        rel = "near the start"
    else:
        dirs = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        rel = f"{dist / 1000:.1f} km {dirs[round(math.degrees(math.atan2(x, z)) / 45) % 8]} of the start"
    return f"x {x:.0f}, z {z:.0f} · {rel}"


def portal_pairs(portals: list) -> dict:
    """{"pairs": [tag], "lonely": [portal], "crowded": {tag: [portal]}, "unnamed": [portal]}.
    Two portals with the same name connect; one alone goes nowhere; with three or more,
    which two connect is down to chance."""
    by_tag: dict = {}
    for p in portals:
        by_tag.setdefault(p["tag"], []).append(p)
    out = {"pairs": [], "lonely": [], "crowded": {}, "unnamed": by_tag.pop("", [])}
    for tag in sorted(by_tag, key=str.lower):
        group = by_tag[tag]
        if len(group) == 2:
            out["pairs"].append(tag)
        elif len(group) == 1:
            out["lonely"].append(group[0])
        else:
            out["crowded"][tag] = group
    return out


def render_portals(portals: list, server_name: str = "") -> dict:
    p = portal_pairs(portals)
    lines = []
    if p["lonely"]:
        lines.append("**Going nowhere** (no other portal with that name):")
        lines += [f"🔸 **{x['tag']}** · {where(x['x'], x['z'])}" for x in p["lonely"]]
    if p["crowded"]:
        lines.append("**More than two with one name** (which two connect is luck):")
        lines += [f"🔶 **{tag}** ×{len(g)}" for tag, g in p["crowded"].items()]
    if p["unnamed"]:
        lines.append(f"**No name:** {len(p['unnamed'])} " + ("portal" if len(p["unnamed"]) == 1 else "portals")
                     + ": " + "; ".join(where(x["x"], x["z"]) for x in p["unnamed"][:5]))
    if p["pairs"]:
        names = ", ".join(p["pairs"])
        lines.append(f"**Connected** ({len(p['pairs'])}): " + (names if len(names) < 1500 else names[:1500] + "…"))
    total = len(portals)
    return {"title": f"🌀 Portals{' in ' + server_name if server_name else ''}: {total}", "color": 0x7E57C2,
            "description": "\n".join(lines)[:4000] or "No portals in the world yet.",
            "footer": {"text": "From the last world save (every 30 minutes) · x, z are map coordinates"}}


def render_tombstones(tombstones: list, day_of=None, server_name: str = "") -> dict:
    lines = []
    for t in sorted(tombstones, key=lambda t: t["died"] or 0, reverse=True)[:25]:
        when = f" · died on day {day_of(t['died'])}" if t["died"] and day_of else ""
        lines.append(f"🪦 **{t['owner']}**{when}\n   {where(t['x'], t['z'])}")
    more = len(tombstones) - 25
    if more > 0:
        lines.append(f"…and {more} more")
    return {"title": f"🪦 Tombstones still out there{' in ' + server_name if server_name else ''}: {len(tombstones)}",
            "color": 0x546E7A,
            "description": "\n".join(lines) or "None. Everyone has picked up their things. 🎉",
            "footer": {"text": "From the last world save (every 30 minutes) · gone once its owner empties it"}}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: world_objects.py <save_dir> [world]")
        return 2
    found = scan(argv[0], argv[1] if len(argv) > 1 else None)
    if found is None:
        print("No world save found under", os.path.join(argv[0], "worlds_local"))
        return 1
    p = portal_pairs(found["portals"])
    print(f"{len(found['portals'])} portals: {len(p['pairs'])} pairs, {len(p['lonely'])} alone, "
          f"{len(p['crowded'])} names used 3+ times, {len(p['unnamed'])} without a name")
    for x in sorted(found["portals"], key=lambda x: x["tag"].lower()):
        print(f"  {x['tag'] or '(no name)':<24} {where(x['x'], x['z'])}")
    print(f"{len(found['tombstones'])} tombstones:")
    import bosses
    for t in found["tombstones"]:
        print(f"  {t['owner']:<24} day {bosses.day_of(t['died']) if t['died'] else '?'}  {where(t['x'], t['z'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
