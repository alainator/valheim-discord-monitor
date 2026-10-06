#!/usr/bin/env python3
"""
Portals, tombstones, ships and tamed animals, read from the world save. No mods.

Valheim 1.0 keeps a world's objects ("ZDOs") in uncompressed worlds_local/<world>/*.chunk
files next to _main.<N>.db2 (older servers: inside <world>.db). Each object is stored as
its position (3 floats), its prefab's hash, then its data, where strings are a key hash, a
7-bit length and UTF-8. So:

  * a portal is the portal prefab's hash, with its name under the "tag" key;
  * a tombstone is Player_tombstone's hash, with "ownerName" and "timeOfDeath" (game time
    in 100 ns ticks). It disappears once its owner empties it, so every tombstone in the
    save is one still lying out there;
  * a ship or cart is just its prefab and position;
  * an animal is saved wild or tame alike: a tame one has the int "tamed" = 1, and maybe
    the name players gave it ("TamedName") and its "level" (1 + stars);
  * every piece a player placed has the long "creator": the player's ID. The save has no
    names for those IDs, but beds and tombstones keep both "owner" (the ID) and
    "ownerName" (the character), which is how IDs get names.

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
# prefab -> (what to call it, emoji)
SHIPS = {_key(p): kind for p, kind in (("Raft", ("raft", "🛶")), ("Karve", ("karve", "⛵")),
                                       ("VikingShip", ("longship", "⛵")), ("VikingShip_Ashlands", ("drakkar", "🚢")),
                                       ("Cart", ("cart", "🛒")))}
TAMEABLE = {_key(p): kind for p, kind in (
    ("Wolf", ("wolf", "🐺")), ("Wolf_cub", ("wolf cub", "🐺")), ("Boar", ("boar", "🐗")), ("Boar_piggy", ("piglet", "🐗")),
    ("Lox", ("lox", "🦣")), ("Lox_Calf", ("lox calf", "🦣")), ("Hen", ("hen", "🐔")), ("Chicken", ("chick", "🐣")),
    ("Asksvin", ("asksvin", "🦎")), ("Asksvin_hatchling", ("asksvin hatchling", "🦎")))}
TAMED, TAMED_NAME, LEVEL = _key("tamed"), _key("TamedName"), _key("level")
CREATOR, OWNER = _key("creator"), _key("owner")
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
    return _string_at(data, i + 4)


def _string_at(data: bytes, p: int) -> Optional[str]:
    """A 7-bit length and UTF-8 starting at p, or None."""
    n, shift = 0, 0
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


def _int(data: bytes, start: int, end: int, key: bytes) -> Optional[int]:
    i = data.find(key, start, end)
    if i < 0 or i + 8 > len(data):
        return None
    return struct.unpack_from("<i", data, i + 4)[0]


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
    """{"portals", "tombstones", "ships", "tames": [...]} found in one file's bytes."""
    kinds = [(PORTALS, "portal"), ({TOMBSTONE: None}, "tombstone"), (SHIPS, "ship"), (TAMEABLE, "animal")]
    hits = sorted((i, what, kind) for table, what in kinds for key, kind in table.items() for i in _hits(data, key))
    out: dict = {"portals": [], "tombstones": [], "ships": [], "tames": [], "builders": {}, "names": {}}
    for n, (i, what, kind) in enumerate(hits):
        pos = _position(data, i)
        if not pos:
            continue
        # An object's data ends where the next one we know starts, so a value is never
        # taken from a neighbour (a nameless portal from the next portal, say).
        end = min(i + WINDOW, hits[n + 1][0] if n + 1 < len(hits) else len(data))
        place = {"x": pos[0], "y": pos[1], "z": pos[2]}
        if what == "portal":
            out["portals"].append({"kind": kind, "tag": (_string(data, i + 4, end, TAG) or "").strip(), **place})
        elif what == "tombstone":
            owner = _string(data, i + 4, end, OWNER_NAME)
            if owner:
                ticks = _long(data, i + 4, end, TIME_OF_DEATH)
                died = ticks / 1e7 if ticks and 0 < ticks < 10 ** 17 else None
                out["tombstones"].append({"owner": owner, "died": died, **place})
        elif what == "ship":
            out["ships"].append({"kind": kind[0], "emoji": kind[1], **place})
        elif _int(data, i + 4, end, TAMED) == 1:
            level = _int(data, i + 4, end, LEVEL)
            out["tames"].append({"kind": kind[0], "emoji": kind[1], "name": (_string(data, i + 4, end, TAMED_NAME)
                                                                            or "").strip(),
                                 "stars": max(0, min(level - 1, 5)) if level else 0, **place})
    for i in _hits(data, CREATOR):                     # one per placed piece
        if i + 12 <= len(data):
            pid = struct.unpack_from("<q", data, i + 4)[0]
            if 0 < pid < 1 << 53:
                out["builders"][pid] = out["builders"].get(pid, 0) + 1
    for i in _hits(data, OWNER_NAME):                  # beds and tombstones: ID -> character
        o = data.rfind(OWNER, max(0, i - 96), i)
        name = _string_at(data, i + 4)
        if o >= 0 and name and name.strip():
            pid = struct.unpack_from("<q", data, o + 4)[0]
            if 0 < pid < 1 << 53:
                out["names"][pid] = name.strip()
    return out


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
    """Everything scan_bytes finds in the live world, or None without a save."""
    files = object_files(save_dir, world)
    if not files:
        return None
    out: dict = {"portals": [], "tombstones": [], "ships": [], "tames": [], "builders": {}, "names": {}}
    for path in files:
        try:
            with open(path, "rb") as f:
                found = scan_bytes(f.read())
        except OSError:
            continue
        for k in ("portals", "tombstones", "ships", "tames"):
            out[k] += found[k]
        for pid, n in found["builders"].items():
            out["builders"][pid] = out["builders"].get(pid, 0) + n
        out["names"].update(found["names"])
    return out


def builder_counts(found: dict) -> tuple:
    """([(character, pieces)] best first, (unnamed builders, their pieces)): characters
    with several IDs (a character remade under the same name) are added together."""
    named: dict = {}
    unnamed = [0, 0]
    for pid, n in found["builders"].items():
        name = found["names"].get(pid)
        if name:
            named[name] = named.get(name, 0) + n
        else:
            unnamed[0] += 1
            unnamed[1] += n
    return sorted(named.items(), key=lambda kv: (-kv[1], kv[0].lower())), tuple(unnamed)


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


def render_ships(ships: list, server_name: str = "") -> dict:
    boats = [s for s in ships if s["kind"] != "cart"]
    carts = [s for s in ships if s["kind"] == "cart"]
    lines = [f"{s['emoji']} **{s['kind'].capitalize()}** · {where(s['x'], s['z'])}"
             for s in sorted(boats, key=lambda s: -math.hypot(s["x"], s["z"]))[:20]]
    if len(boats) > 20:
        lines.append(f"…and {len(boats) - 20} more")
    if carts:
        lines.append(f"\n🛒 **Carts** ({len(carts)}): " + "; ".join(where(c["x"], c["z"]) for c in carts[:5])
                     + (" …" if len(carts) > 5 else ""))
    counts = {}
    for s in boats:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    summary = " · ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items())
    return {"title": f"⛵ Ships{' in ' + server_name if server_name else ''}: {len(boats)}", "color": 0x1F6FB2,
            "description": ((summary + "\n\n") if summary else "") + ("\n".join(lines) or "No ships yet. Time to build a raft."),
            "footer": {"text": "From the last world save (every 30 minutes) · furthest from the start first"}}


def render_tames(tames: list, server_name: str = "") -> dict:
    counts: dict = {}
    for t in tames:
        counts.setdefault((t["emoji"], t["kind"]), 0)
        counts[(t["emoji"], t["kind"])] += 1
    summary = " · ".join(f"{e} {n} {k}{'' if n == 1 or k.endswith(('lox', 'asksvin')) else 's'}"
                         for (e, k), n in sorted(counts.items(), key=lambda c: -c[1]))
    named = sorted((t for t in tames if t["name"]), key=lambda t: t["name"].lower())
    lines = [f"{t['emoji']} **{t['name']}** ({t['kind']}{' ' + '★' * t['stars'] if t['stars'] else ''})"
             f" · {where(t['x'], t['z'])}" for t in named[:25]]
    if len(named) > 25:
        lines.append(f"…and {len(named) - 25} more with names")
    unnamed = len(tames) - len(named)
    if unnamed and named:
        lines.append(f"…plus {unnamed} without a name")
    return {"title": f"🐾 Tamed animals{' in ' + server_name if server_name else ''}: {len(tames)}", "color": 0x8D6E63,
            "description": ((summary + "\n\n") if summary else "") + ("\n".join(lines) or
                            ("None of them have names yet." if tames else "No tamed animals yet. Wolves love meat.")),
            "footer": {"text": "From the last world save (every 30 minutes) · name a tame by hovering it and pressing E"}}


def render_builders(found: dict, server_name: str = "") -> dict:
    named, (others, other_pieces) = builder_counts(found)
    medals = ("🥇", "🥈", "🥉")
    total = sum(found["builders"].values())
    lines = [f"{medals[i] if i < 3 else f'`{i + 1:>2}.`'} **{name}**: {n:,} pieces"
             for i, (name, n) in enumerate(named[:15])]
    if others:
        lines.append(f"\n…plus **{others}** builder{'s' if others != 1 else ''} I can't name yet "
                     f"({other_pieces:,} pieces). A name is learned once they sleep in a bed or leave a tombstone.")
    return {"title": f"🔨 Builders{' of ' + server_name if server_name else ''}: {total:,} pieces", "color": 0xA1887F,
            "description": "\n".join(lines) or "Nothing built yet.",
            "footer": {"text": "Pieces standing in the world now, by who placed them · from the last save"}}


# -- the death map ------------------------------------------------------------------------
def hotspots(points: list, cell: float = 250) -> list:
    """[(deaths, x, z, {owner: n})] for each `cell`-metre square with deaths, worst first;
    x, z is the average spot inside it."""
    cells: dict = {}
    for p in points:
        key = (math.floor(p["x"] / cell), math.floor(p["z"] / cell))
        cells.setdefault(key, []).append(p)
    out = []
    for group in cells.values():
        who: dict = {}
        for p in group:
            who[p["owner"]] = who.get(p["owner"], 0) + 1
        out.append((len(group), sum(p["x"] for p in group) / len(group), sum(p["z"] for p in group) / len(group), who))
    return sorted(out, key=lambda h: -h[0])


def render_deathmap(points: list, player: str = "", server_name: str = "") -> dict:
    title = f"💀 Where {player} dies" if player else f"💀 Where we die{' in ' + server_name if server_name else ''}"
    if not points:
        return {"title": title, "color": 0x992D22,
                "description": "No tombstones recorded yet. Each one is noted when a world save has it, so "
                               "deaths show up here from now on."}
    lines = [f"**{len(points)}** tombstone{'s' if len(points) != 1 else ''} recorded."]
    for n, x, z, who in hotspots(points)[:5]:
        names = ", ".join(f"{o} ×{c}" if c > 1 else o for o, c in sorted(who.items(), key=lambda kv: -kv[1])[:4])
        lines.append(f"☠️ **{n}** · {where(x, z)}" + ("" if player else f"\n   {names}"))
    return {"title": title, "color": 0x992D22, "description": "\n".join(lines),
            "footer": {"text": "Tombstones seen in world saves · one recovered within ~30 minutes may be missed"}}


def death_map(points: list, portals: list = (), title: str = "Where we die") -> Optional[bytes]:
    """A PNG map of tombstone spots (red), with portals (purple) and the start for bearings,
    zoomed to where the deaths are. None without Pillow or deaths."""
    if not points:
        return None
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    xs, zs = [p["x"] for p in points] + [0], [p["z"] for p in points] + [0]
    cx, cz = (min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2
    half = max(max(xs) - min(xs), max(zs) - min(zs), 800) / 2 * 1.15
    size, top = 800, 50
    img = Image.new("RGB", (size, size + top), (43, 45, 49))
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font, small = ImageFont.load_default(size=20), ImageFont.load_default(size=13)
    except TypeError:                                              # Pillow before 10.1
        font = small = ImageFont.load_default()

    def px(x, z):
        return (size / 2 + (x - cx) / half * size / 2, top + size / 2 - (z - cz) / half * size / 2)

    draw.text((16, 14), title, fill=(242, 243, 245), font=font)
    step = 500 if half < 2000 else 1000 if half < 5000 else 2000          # grid lines, metres
    g = math.floor((cx - half) / step) * step
    while g <= cx + half:
        x0, _ = px(g, 0)
        draw.line([(x0, top), (x0, top + size)], fill=(60, 63, 69), width=1)
        draw.text((x0 + 3, top + size - 16), f"x {g:.0f}", fill=(128, 132, 142), font=small)
        g += step
    g = math.floor((cz - half) / step) * step
    while g <= cz + half:
        _, z0 = px(0, g)
        draw.line([(0, z0), (size, z0)], fill=(60, 63, 69), width=1)
        draw.text((4, z0 + 2), f"z {g:.0f}", fill=(128, 132, 142), font=small)
        g += step
    sx, sy = px(0, 0)
    labelled: list = [(sx, sy)]                  # keep portal names clear of "start"
    for p in portals:
        x, y = px(p["x"], p["z"])
        if 0 <= x <= size and top <= y <= top + size:
            draw.polygon([(x, y - 5), (x + 5, y), (x, y + 5), (x - 5, y)], fill=(126, 87, 194))
            if p.get("tag") and all(math.hypot(x - a, y - b) > 60 for a, b in labelled):
                labelled.append((x, y))
                draw.text((x + 7, y - 7), p["tag"], fill=(179, 157, 219), font=small)
    draw.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], outline=(87, 242, 135), width=2)
    draw.text((sx + 9, sy - 7), "start", fill=(87, 242, 135), font=small)
    for p in points:
        x, y = px(p["x"], p["z"])
        draw.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(237, 66, 69, 150))
    for n, hx, hz, _ in hotspots(points)[:5]:
        if n > 1:
            x, y = px(hx, hz)
            r = 8 + 3 * min(n, 10)
            draw.ellipse([x - r, y - r, x + r, y + r], outline=(237, 66, 69), width=2)
            draw.text((x, y - r - 3), str(n), fill=(255, 255, 255), font=small, anchor="mb")
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# -- the weekly world digest ---------------------------------------------------------------
def snapshot(found: dict) -> dict:
    """What to remember of the world for next week's comparison (JSON-friendly)."""
    named, (_, unnamed_pieces) = builder_counts(found)
    return {"pieces": sum(found["builders"].values()), "builders": dict(named), "unnamed": unnamed_pieces,
            "portals": sorted(p["tag"] for p in found["portals"]),
            "ships": [[s["kind"], round(s["x"]), round(s["z"])] for s in found["ships"]],
            "tombstones": [[t["owner"], round(t["died"] or 0)] for t in found["tombstones"]],
            "tames": [[a["kind"], a["name"]] for a in found["tames"]]}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 or word.endswith(('lox', 'asksvin')) else 's'}"


def _match_ships(old: list, new: list, moved_m: float = 200) -> tuple:
    """(new ships, gone ships, moved count): each ship is matched to the nearest one of the
    same kind from last week; further than moved_m means it sailed somewhere."""
    left = [list(s) for s in old]
    added, moved = [], 0
    for kind, x, z in new:
        same = [s for s in left if s[0] == kind]
        if not same:
            added.append(kind)
            continue
        near = min(same, key=lambda s: math.hypot(s[1] - x, s[2] - z))
        left.remove(near)
        if math.hypot(near[1] - x, near[2] - z) > moved_m:
            moved += 1
    return added, [s[0] for s in left], moved


def digest(old: dict, new: dict) -> list:
    """Lines saying what changed in the world between two snapshots (nothing if nothing did)."""
    lines = []
    delta = new["pieces"] - old.get("pieces", 0)
    if delta:
        grew = sorted(((n - old.get("builders", {}).get(name, 0), name) for name, n in new["builders"].items()),
                      reverse=True)
        top = [f"{name} +{d:,}" for d, name in grew if d > 0][:3]
        line = f"🔨 **{'+' if delta > 0 else '−'}{abs(delta):,} pieces** (now {new['pieces']:,})"
        line += f" · most building: {', '.join(top)}" if top else " · torn down, or wrecked by raids" if delta < 0 else ""
        lines.append(line)
    old_tags, new_tags = list(old.get("portals", [])), list(new["portals"])
    added = []
    for tag in new_tags:
        if tag in old_tags:
            old_tags.remove(tag)
        else:
            added.append(tag)
    if added:
        lines.append("🌀 New portals: " + ", ".join(sorted({t or "(no name)" for t in added})))
    if old_tags:
        lines.append("🌀 Portals taken down: " + ", ".join(sorted({t or "(no name)" for t in old_tags})))
    ship_new, ship_gone, moved = _match_ships(old.get("ships", []), new["ships"])
    bits = [f"{_plural(ship_new.count(k), k)} built" for k in sorted(set(ship_new))]
    bits += [f"{_plural(ship_gone.count(k), k)} gone" for k in sorted(set(ship_gone))]
    if moved:
        bits.append(f"{moved} {'ship' if moved == 1 else 'ships'} sailed somewhere new")
    if bits:
        lines.append("⛵ " + " · ".join(bits))
    old_graves = {tuple(g) for g in old.get("tombstones", [])}
    new_graves = {tuple(g) for g in new["tombstones"]}
    recovered, fresh = len(old_graves - new_graves), sorted(new_graves - old_graves)
    if recovered or fresh:
        parts = [f"{recovered} recovered"] if recovered else []
        if fresh:
            parts.append(f"{len(fresh)} new ({', '.join(sorted({o for o, _ in fresh}))})")
        still = len(new_graves & old_graves)
        lines.append("🪦 Tombstones: " + ", ".join(parts) + (f", {still} still out there" if still else ""))
    old_kinds: dict = {}
    for kind, _ in old.get("tames", []):
        old_kinds[kind] = old_kinds.get(kind, 0) + 1
    new_kinds: dict = {}
    for kind, _ in new["tames"]:
        new_kinds[kind] = new_kinds.get(kind, 0) + 1
    changes = [f"{'+' if new_kinds.get(k, 0) > old_kinds.get(k, 0) else '−'}"
               f"{_plural(abs(new_kinds.get(k, 0) - old_kinds.get(k, 0)), k)}"
               for k in sorted(set(old_kinds) | set(new_kinds)) if new_kinds.get(k, 0) != old_kinds.get(k, 0)]
    names = sorted({n for _, n in new["tames"] if n} - {n for _, n in old.get("tames", []) if n})
    if changes or names:
        lines.append("🐾 Tames: " + ", ".join(changes) + (f"{' · ' if changes else ''}newly named: {', '.join(names)}"
                                                         if names else ""))
    return lines


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
    print(f"{len(found['ships'])} ships and carts:")
    for s in found["ships"]:
        print(f"  {s['kind']:<24} {where(s['x'], s['z'])}")
    named, (others, other_pieces) = builder_counts(found)
    print(f"{sum(found['builders'].values())} pieces by {len(found['builders'])} builders"
          f" ({others} without a name, {other_pieces} pieces):")
    for name, n in named:
        print(f"  {name:<24} {n}")
    print(f"{len(found['tames'])} tamed animals:")
    for a in sorted(found["tames"], key=lambda a: (a["kind"], a["name"].lower())):
        print(f"  {a['kind']:<12} {a['name'] or '(no name)':<16} {'*' * a['stars']:<3} {where(a['x'], a['z'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
