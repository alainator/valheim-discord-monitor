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
    "ownerName" (the character), which is how IDs get names;
  * a sign keeps what's written on it under "text";
  * a cartography table keeps the shared map under "data", gzipped: an int version (3), the
    explored map (an int count, then a byte per cell), then the pins shared on it (an int
    count, then per pin the player ID, name, position, icon, crossed out, and the author's
    platform ID, which isn't kept here);
  * workbenches, forges, beds, wards and the other crafting stations are just their prefab,
    position and creator, and close together they make a base;
  * a smelter, kiln, blast furnace, eitr refinery, windmill or spinning wheel keeps its
    "fuel" (float), how many items are "queued" (int) and which ("item0", "item1"…), and
    "bakeTimer" (seconds into the current one); a fermenter its "Content" and "StartTime"
    (game time in 100 ns ticks); a beehive or sap collector its "level" (ready to collect).

Run it on its own to check a save: python3 world_objects.py /path/to/valheim_save_data
"""

from __future__ import annotations

import glob
import gzip
import math
import os
import re
import struct
import sys
import zlib
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
# Containers whose "items" are worth searching (ships, carts and tombstones too, below).
CHESTS = {_key(p): kind for p, kind in (
    ("piece_chest_wood", "chest"), ("piece_chest", "reinforced chest"), ("piece_chest_private", "personal chest"),
    ("piece_chest_blackmetal", "black metal chest"), ("piece_chest_barrel", "barrel"))}
ITEMS, TEXT, SIGN = _key("items"), _key("text"), _key("sign")
# What makes a base: prefab -> (what to call it, emoji). Beds keep their owner's name.
BASE_PIECES = {_key(p): kind for p, kind in (
    ("piece_workbench", ("workbench", "🔨")), ("forge", ("forge", "⚒️")), ("bed", ("bed", "🛏️")),
    ("piece_bed02", ("bed", "🛏️")), ("guard_stone", ("ward", "🛡️")), ("piece_stonecutter", ("stonecutter", "🪨")),
    ("piece_artisanstation", ("artisan table", "🪚")), ("blackforge", ("black forge", "⚒️")),
    ("piece_magetable", ("galdr table", "🔮")), ("piece_cauldron", ("cauldron", "🍲")),
    ("fermenter", ("fermenter", "🍺")), ("smelter", ("smelter", "🔥")), ("charcoal_kiln", ("kiln", "🔥")),
    ("blastfurnace", ("blast furnace", "🔥")), ("eitrrefinery", ("eitr refinery", "🔥")),
    ("piece_spinningwheel", ("spinning wheel", "🧶")), ("windmill", ("windmill", "🌾")),
    ("piece_cartographytable", ("cartography table", "🗺️")),
    ("piece_beehive", ("beehive", "🐝")), ("piece_sapcollector", ("sap collector", "🌳")))}
# Stations that work through a queue (Smelter in the game): what they burn, if anything.
QUEUE_STATIONS = {"smelter": "coal", "blast furnace": "coal", "eitr refinery": "sap", "kiln": None,
                  "windmill": None, "spinning wheel": None}
COLLECTORS = {"beehive": "honey", "sap collector": "sap"}
FERMENT_SECONDS = 2400            # a fermenter takes 2400 s of game time (Fermenter.m_fermentationDuration)
FUEL, QUEUED, BAKE_TIMER, CONTENT, START_TIME = (_key("fuel"), _key("queued"), _key("bakeTimer"), _key("Content"),
                                                 _key("StartTime"))
QUEUE_ITEMS = [_key(f"item{n}") for n in range(60)]
DATA = _key("data")
# The map's pin icons, by number (Minimap.PinType). 14-16 are Hildir's quest pins.
PIN_ICONS = {0: "fire", 1: "house", 2: "hammer", 3: "dot", 4: "death", 5: "bed", 6: "cave", 7: "shout",
             8: "none", 9: "boss", 10: "player", 11: "event", 12: "ping", 13: "event area",
             14: "hildir", 15: "hildir", 16: "hildir"}
INVENTORY_WINDOW = 2048          # how far after the prefab a container's "items" may start
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


def _read_string(data: bytes, p: int) -> tuple:
    """(text or None, position after it) for a 7-bit length and UTF-8 at p."""
    n, shift = 0, 0
    while p < len(data) and shift < 35:
        byte = data[p]
        n |= (byte & 0x7F) << shift
        p += 1
        shift += 7
        if not byte & 0x80:
            break
    if n > 200 or p + n > len(data):
        return None, p
    try:
        return data[p:p + n].decode("utf-8"), p + n
    except UnicodeDecodeError:
        return None, p + n


def inventory(data: bytes, at: int) -> Optional[list]:
    """[(item hash, stack, crafter name or None)] from the byte array stored under the
    "items" key at `at`, or None if it doesn't look like an inventory.

    Valheim 1.0's layout (worked out from a real save): an int length; then the inventory:
    int version, ushort count, and per item: int (durability), byte column, byte row, a
    byte, a flags byte, ushort stack if flags & 0x08, crafter ID (long) and name if
    flags & 0x20, the item's ID hash (uint), and an end byte. Flags we haven't seen stop
    the read, rather than misread what follows."""
    try:
        size = struct.unpack_from("<i", data, at + 4)[0]
        if not 6 <= size <= 1 << 16 or at + 8 + size > len(data):
            return None
        p, end = at + 8, at + 8 + size
        version, count = struct.unpack_from("<iH", data, p)
        if not 90 <= version <= 1000 or count > 500:
            return None
        p += 6
        out = []
        for _ in range(count):
            flags = data[p + 7]
            if flags & ~0x69:                   # a field we don't know the size of
                break
            p += 8
            stack = 1
            if flags & 0x08:
                stack = struct.unpack_from("<H", data, p)[0]
                p += 2
            crafter = None
            if flags & 0x20:
                crafter, p = _read_string(data, p + 8)
            item = struct.unpack_from("<I", data, p)[0]
            p += 5
            if p > end:
                break
            out.append((item, stack, crafter))
        return out
    except (struct.error, IndexError):
        return None


def _container(data: bytes, i: int, end: int) -> Optional[list]:
    j = data.find(ITEMS, i + 4, end)
    return inventory(data, j) if j >= 0 else None


MAP_SIZE = 2048        # the explored map is MAP_SIZE x MAP_SIZE cells...
MAP_CELL = 12          # ...of 12 x 12 m, centred on the start (Valheim's Minimap)
WORLD_RADIUS = 10500   # metres to the edge of the world


def read_map(data: bytes, i: int, end: int) -> tuple:
    """What's shared on the cartography table whose prefab hash is at `i`: (pins, explored).
    pins is [{"name", "icon", "x", "z", "crossed", "owner"}]; explored is MAP_SIZE² bytes, 1
    for each explored cell, row by row from the south (row = z, column = x). ([], None) when
    nothing is shared yet, or anything doesn't add up (every byte must be accounted for)."""
    j = data.find(DATA, i + 4, end)
    if j < 0 or j + 8 > len(data):
        return [], None
    n = struct.unpack_from("<i", data, j + 4)[0]
    if not 0 < n <= len(data) - j - 8:
        return [], None
    try:
        raw = gzip.decompress(data[j + 8:j + 8 + n])
        version, cells = struct.unpack_from("<ii", raw, 0)
        if version != 3 or not 0 < cells <= 1 << 24:
            return [], None
        p = 8 + cells
        count = struct.unpack_from("<i", raw, p)[0]
        if not 0 <= count <= 100000:
            return [], None
        p += 4
        pins = []
        for _ in range(count):
            owner = struct.unpack_from("<q", raw, p)[0]
            name, p = _read_string(raw, p + 8)
            x, _, z = struct.unpack_from("<3f", raw, p)
            icon, crossed = struct.unpack_from("<iB", raw, p + 12)
            _, p = _read_string(raw, p + 17)                      # the author's platform ID: not kept
            pins.append({"name": (name or "").strip(), "icon": PIN_ICONS.get(icon, str(icon)), "x": x, "z": z,
                         "crossed": bool(crossed), "owner": owner})
        if p != len(raw):
            return [], None
        explored = raw[8:8 + cells] if cells == MAP_SIZE * MAP_SIZE else None
        return pins, explored
    except (OSError, EOFError, ValueError, struct.error, IndexError, zlib.error):
        return [], None


def merge_explored(a: Optional[bytes], b: Optional[bytes]) -> Optional[bytes]:
    """Both tables' explored maps together (a cell either has explored counts)."""
    if a is None or b is None:
        return a if b is None else b
    return (int.from_bytes(a, "big") | int.from_bytes(b, "big")).to_bytes(len(a), "big")


def map_cell(x: float, z: float) -> tuple:
    """(column, row) of the map cell a world position falls in."""
    return round(x / MAP_CELL + MAP_SIZE / 2), round(z / MAP_CELL + MAP_SIZE / 2)


def is_explored(explored: Optional[bytes], x: float, z: float) -> bool:
    col, row = map_cell(x, z)
    return bool(explored) and 0 <= col < MAP_SIZE and 0 <= row < MAP_SIZE and explored[row * MAP_SIZE + col] == 1


def explored_share(explored: Optional[bytes]) -> Optional[dict]:
    """{"cells", "km2", "percent"}: how much of the world the shared map has uncovered."""
    if not explored:
        return None
    cells = explored.count(1)
    world = math.pi * (WORLD_RADIUS / MAP_CELL) ** 2
    return {"cells": cells, "km2": cells * MAP_CELL * MAP_CELL / 1e6, "percent": min(100.0, 100 * cells / world)}


def _float(data: bytes, start: int, end: int, key: bytes) -> Optional[float]:
    i = data.find(key, start, end)
    if i < 0 or i + 8 > len(data):
        return None
    v = struct.unpack_from("<f", data, i + 4)[0]
    return v if math.isfinite(v) else None


def station_state(data: bytes, i: int, end: int, kind: str) -> dict:
    """What a production station is doing, from its data (see the module's docstring)."""
    if kind in QUEUE_STATIONS:
        queued = max(0, min(_int(data, i + 4, end, QUEUED) or 0, len(QUEUE_ITEMS)))
        items = [_string(data, i + 4, end, QUEUE_ITEMS[n]) or "" for n in range(queued)]
        return {"fuel": _float(data, i + 4, end, FUEL) or 0.0, "queue": [x for x in items if x],
                "busy": (_float(data, i + 4, end, BAKE_TIMER) or 0) > 0}
    if kind == "fermenter":
        content = (_string(data, i + 4, end, CONTENT) or "").strip()
        ticks = _long(data, i + 4, end, START_TIME) if content else None
        return {"content": content, "started": ticks / 1e7 if ticks and 0 < ticks < 10 ** 17 else None}
    if kind in COLLECTORS:
        return {"level": max(0, _int(data, i + 4, end, LEVEL) or 0)}
    return {}


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
    """{"portals", "tombstones", "ships", "tames", "containers", "signs": [...], "builders",
    "names": {...}} found in one file's bytes."""
    kinds = [(PORTALS, "portal"), ({TOMBSTONE: None}, "tombstone"), (SHIPS, "ship"), (TAMEABLE, "animal"),
             (CHESTS, "chest"), ({SIGN: None}, "sign"), (BASE_PIECES, "piece")]
    hits = sorted((i, what, kind) for table, what in kinds for key, kind in table.items() for i in _hits(data, key))
    out: dict = {"portals": [], "tombstones": [], "ships": [], "tames": [], "containers": [], "signs": [],
                 "pieces": [], "pins": [], "explored": None, "builders": {}, "names": {}}
    for n, (i, what, kind) in enumerate(hits):
        pos = _position(data, i)
        if not pos:
            continue
        # An object's data ends where the next one we know starts, so a value is never
        # taken from a neighbour (a nameless portal from the next portal, say).
        next_hit = hits[n + 1][0] if n + 1 < len(hits) else len(data)
        end = min(i + WINDOW, next_hit)
        place = {"x": pos[0], "y": pos[1], "z": pos[2]}
        stored = None
        if what in ("chest", "ship", "tombstone"):
            stored = _container(data, i, min(i + INVENTORY_WINDOW, next_hit))
        if what == "portal":
            out["portals"].append({"kind": kind, "tag": (_string(data, i + 4, end, TAG) or "").strip(), **place})
        elif what == "tombstone":
            owner = _string(data, i + 4, end, OWNER_NAME)
            if owner:
                ticks = _long(data, i + 4, end, TIME_OF_DEATH)
                died = ticks / 1e7 if ticks and 0 < ticks < 10 ** 17 else None
                out["tombstones"].append({"owner": owner, "died": died, **place})
                if stored:
                    out["containers"].append({"kind": "tombstone", "owner": owner, "items": stored, **place})
        elif what == "ship":
            out["ships"].append({"kind": kind[0], "emoji": kind[1], **place})
            if stored:
                out["containers"].append({"kind": kind[0], "items": stored, **place})
        elif what == "chest":
            if stored is not None:
                out["containers"].append({"kind": kind, "items": stored, **place})
            out["pieces"].append({"kind": "chest", "emoji": "📦", "creator": _long(data, i + 4, end, CREATOR), **place})
        elif what == "sign":
            text = sign_text(_string(data, i + 4, end, TEXT) or "")
            if text:
                out["signs"].append({"text": text, **place})
        elif what == "piece":
            piece = {"kind": kind[0], "emoji": kind[1], "creator": _long(data, i + 4, end, CREATOR), **place}
            if kind[0] in QUEUE_STATIONS or kind[0] in COLLECTORS or kind[0] == "fermenter":
                # a windmill's queue of 50 names runs past WINDOW
                piece.update(station_state(data, i, min(i + INVENTORY_WINDOW, next_hit), kind[0]))
            if kind[0] == "bed":
                piece["owner"] = (_string(data, i + 4, end, OWNER_NAME) or "").strip()
            elif kind[0] == "cartography table":
                pins, explored = read_map(data, i, min(i + 4096, next_hit))   # as far as the check looked
                out["pins"] += pins
                out["explored"] = merge_explored(out["explored"], explored)
            out["pieces"].append(piece)
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
    out: dict = {"portals": [], "tombstones": [], "ships": [], "tames": [], "containers": [], "signs": [],
                 "pieces": [], "pins": [], "explored": None, "builders": {}, "names": {}}
    for path in files:
        try:
            with open(path, "rb") as f:
                found = scan_bytes(f.read())
        except OSError:
            continue
        for k in ("portals", "tombstones", "ships", "tames", "containers", "signs", "pieces"):
            out[k] += found[k]
        out["pins"] += found["pins"]
        out["explored"] = merge_explored(out["explored"], found["explored"])
        for pid, n in found["builders"].items():
            out["builders"][pid] = out["builders"].get(pid, 0) + n
        out["names"].update(found["names"])
    # Several tables share the same pins: keep each once.
    seen: set = set()
    pins = []
    for pin in out["pins"]:
        k = (pin["name"], pin["icon"], round(pin["x"]), round(pin["z"]))
        if k not in seen:
            seen.add(k)
            pins.append(pin)
    out["pins"] = pins
    import bosses
    out["clock"] = bosses.world_time(save_dir, world)
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


def sign_text(raw: str) -> str:
    """A sign's text without the game's rich-text tags (<color=red>, <b>…) and with its lines
    joined: what players actually read on it."""
    return " ".join(re.sub(r"<[^<>]{1,40}>", "", raw).split())[:80]


def md(text: str) -> str:
    """Player-written text, safe to put in an embed: no accidental bold, links or code."""
    return re.sub(r"([\\*_~`|>#\[\]()])", r"\\\1", text)


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


# -- what's in the chests (/muninn find, /muninn stock) -----------------------------------
# Personal chests only open for their owner in the game, so their contents stay private here too.
PRIVATE_KINDS = {"personal chest"}


def searchable(found: dict, tombstones: bool = True) -> list:
    """The containers whose contents can be shown: not personal chests; tombstones optional."""
    return [c for c in found.get("containers", []) if c["kind"] not in PRIVATE_KINDS
            and (tombstones or c["kind"] != "tombstone")]


def stock(found: dict) -> dict:
    """{item hash: total} over the chests, barrels, carts and ships (not tombstones)."""
    totals: dict = {}
    for c in searchable(found, tombstones=False):
        for item, n, _ in c["items"]:
            totals[item] = totals.get(item, 0) + n
    return totals


def landmark(x: float, z: float, found: dict) -> str:
    """Where a container is, in words: the nearest sign within 15 m, the nearest portal
    within 100 m, and the coordinates."""
    bits = []
    sign = min(found.get("signs", []), key=lambda s: math.hypot(s["x"] - x, s["z"] - z), default=None)
    if sign and math.hypot(sign["x"] - x, sign["z"] - z) <= 15:
        bits.append(f"by the sign “{md(sign['text'][:40])}”")
    portal = min((p for p in found.get("portals", []) if p["tag"]),
                 key=lambda p: math.hypot(p["x"] - x, p["z"] - z), default=None)
    if portal and math.hypot(portal["x"] - x, portal["z"] - z) <= 100:
        bits.append(f"near the {portal['tag']} portal")
    bits.append(where(x, z))
    return " · ".join(bits)


def find_items(found: dict, query: str) -> list:
    """[(item hash, total, [(count, container)])] for items whose name contains `query`
    (an exact name first), most first; places most first."""
    import items
    q = query.strip().lower()
    by_item: dict = {}
    for c in searchable(found):
        here: dict = {}
        for item, n, _ in c["items"]:
            here[item] = here.get(item, 0) + n
        for item, n in here.items():
            name = items.name_of(item).lower()
            if q and (q in name or q == (items.id_of(item) or "").lower()):
                by_item.setdefault(item, []).append((n, c))
    out = [(item, sum(n for n, _ in places), sorted(places, key=lambda pc: -pc[0])) for item, places in by_item.items()]
    return sorted(out, key=lambda r: (items.name_of(r[0]).lower() != q, -r[1]))


def item_names(found: dict) -> list:
    """Every item name found in the searchable containers, for autocomplete."""
    import items
    return sorted({items.name_of(item) for c in searchable(found) for item, _, _ in c["items"]
                   if items.id_of(item)}, key=str.lower)


def _container_label(c: dict) -> str:
    if c["kind"] == "tombstone":
        return f"{c.get('owner', 'someone')}'s tombstone"
    return f"a {c['kind']}" if c["kind"] not in ("karve", "longship", "drakkar", "cart", "raft") else f"a {c['kind']}'s cargo"


def render_find(found: dict, query: str, server_name: str = "") -> dict:
    import items
    results = find_items(found, query)
    if not results:
        return {"title": f"🔎 {query}", "color": 0x3BA55D,
                "description": f"No **{query}** in any chest, barrel, cart or ship"
                               f"{' in ' + server_name if server_name else ''} (personal chests aren't searched)."}
    lines = []
    for item, total, places in results[:4]:
        lines.append(f"**{items.name_of(item)}**: {total:,} in {len(places)} place{'s' if len(places) != 1 else ''}")
        for n, c in places[:6 if len(results) == 1 else 3]:
            lines.append(f"📦 **{n:,}** in {_container_label(c)} · {landmark(c['x'], c['z'], found)}")
        shown = 6 if len(results) == 1 else 3
        if len(places) > shown:
            lines.append(f"…and {len(places) - shown} more place{'s' if len(places) - shown != 1 else ''}")
    if len(results) > 4:
        lines.append(f"\nAlso matching: " + ", ".join(items.name_of(r[0]) for r in results[4:12]))
    return {"title": f"🔎 {query}", "color": 0x3BA55D, "description": "\n".join(lines)[:4000],
            "footer": {"text": "From the last world save (every 30 minutes) · personal chests aren't searched"}}


def render_stock(found: dict, server_name: str = "", limit: int = 40) -> dict:
    import items
    totals = sorted(stock(found).items(), key=lambda kv: (-kv[1], items.name_of(kv[0]).lower()))
    boxes = len(searchable(found, tombstones=False))
    if not totals:
        return {"title": "📦 What we have", "color": 0x3BA55D, "description": "The chests are empty, or there are none yet."}
    lines = [f"`{n:>6,}` {items.name_of(item)}" for item, n in totals[:limit]]
    more = len(totals) - limit
    return {"title": f"📦 What we have{' in ' + server_name if server_name else ''}", "color": 0x3BA55D,
            "description": "\n".join(lines) + (f"\n…and {more} more kinds of item" if more > 0 else ""),
            "footer": {"text": f"{len(totals)} kinds of item in {boxes} chests, barrels, carts and ships · "
                               "from the last world save · /muninn find <item> says where"}}


# -- bases and signs (/muninn bases, /muninn signs) --------------------------------------
BASE_LINK = 40         # metres: pieces this close (or chained this close) are one base
PIN_REACH = 50         # a named house pin this close to a base's pieces names it
PORTAL_REACH = 150     # a named portal this close to a base's centre is "its" portal


def _groups(points: list, link: float) -> list:
    """Single-linkage groups of points ({"x", "z"}) closer than `link`, via a grid."""
    parent = list(range(len(points)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    cells: dict = {}
    for i, pt in enumerate(points):
        cells.setdefault((int(pt["x"] // link), int(pt["z"] // link)), []).append(i)
    for (cx, cz), members in cells.items():
        near = [j for dx in (-1, 0, 1) for dz in (-1, 0, 1) for j in cells.get((cx + dx, cz + dz), ())]
        for i in members:
            for j in near:
                if j > i and math.hypot(points[i]["x"] - points[j]["x"], points[i]["z"] - points[j]["z"]) < link:
                    parent[root(i)] = root(j)
    groups: dict = {}
    for i in range(len(points)):
        groups.setdefault(root(i), []).append(points[i])
    return list(groups.values())


def _is_base(pieces: list) -> bool:
    """A bed, a ward, a station beyond the workbench, or 3+ chests. A workbench alone (put
    down to build a portal or a bridge) is an outpost, not a base; so is a beehive."""
    kinds = [p["kind"] for p in pieces]
    return any(k not in ("workbench", "chest", *COLLECTORS) for k in kinds) or kinds.count("chest") >= 3


def bases(found: dict) -> tuple:
    """([base], outposts): each base is {"x", "z", "pieces", "beds": [owner], "kinds": {kind: n},
    "builders": [(name, n)], "pin", "portal"}, biggest first. "pin" is the name of the house
    pin on the cartography table nearest its middle, if one is close enough."""
    out, outposts = [], 0
    for group in _groups(found.get("pieces", []), BASE_LINK):
        if not _is_base(group):
            outposts += 1
            continue
        x = sum(p["x"] for p in group) / len(group)
        z = sum(p["z"] for p in group) / len(group)
        kinds: dict = {}
        for p in group:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        beds = sorted({p["owner"] for p in group if p.get("owner")}, key=str.lower)
        built: dict = {}
        for p in group:
            name = found.get("names", {}).get(p.get("creator"))
            if name:
                built[name] = built.get(name, 0) + 1
        houses = [h for h in found.get("pins", []) if h["icon"] == "house" and h["name"]
                  and any(math.hypot(h["x"] - p["x"], h["z"] - p["z"]) <= PIN_REACH for p in group)]
        house = min(houses, key=lambda h: math.hypot(h["x"] - x, h["z"] - z), default=None)
        portal = min((pt for pt in found.get("portals", []) if pt["tag"]),
                     key=lambda pt: math.hypot(pt["x"] - x, pt["z"] - z), default=None)
        if portal and math.hypot(portal["x"] - x, portal["z"] - z) > PORTAL_REACH:
            portal = None
        out.append({"x": x, "z": z, "pieces": len(group), "beds": beds, "kinds": kinds, "members": group,
                    "builders": sorted(built.items(), key=lambda kv: (-kv[1], kv[0].lower())),
                    "pin": house["name"] if house else None, "portal": portal["tag"] if portal else None})
    out.sort(key=lambda b: (-b["pieces"], b["x"], b["z"]))
    return out, outposts


def _names(names: list, escape=md) -> str:
    names = [escape(n) for n in names]
    if len(names) <= 2:
        return " and ".join(names)
    if len(names) == 3:
        return f"{names[0]}, {names[1]} and {names[2]}"
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"


def base_name(b: dict, escape=md) -> str:
    """What to call a base: markdown-escaped for Discord, or as is (escape=str) for a picture."""
    if b["pin"]:
        return escape(b["pin"][:40])
    who = b["beds"] or [name for name, _ in b["builders"][:2]]
    return f"{_names(who, escape)}'s base" if who else "A base nobody's named"


def _count(kind: str, n: int) -> str:
    """ "workbench", "3 workbenches"."""
    if n == 1:
        return kind
    if kind.endswith("y") and kind[-2:-1] not in "aeiou":
        return f"{n} {kind[:-1]}ies"
    return f"{n} {kind}es" if kind.endswith(("ch", "sh")) else f"{n} {kind}s"


def render_bases(found: dict, server_name: str = "", limit: int = 12) -> dict:
    found_bases, outposts = bases(found)
    lines = []
    for b in found_bases[:limit]:
        place = " · ".join(([f"near the {md(b['portal'])} portal"] if b["portal"] else []) + [where(b["x"], b["z"])])
        lines.append(f"🏠 **{base_name(b)}** · {place}")
        stuff = []
        if b["beds"]:
            stuff.append("🛏️ " + _names(b["beds"]))
        elif b["kinds"].get("bed"):
            stuff.append(f"🛏️ {_count('bed', b['kinds']['bed'])}, nobody's")
        icons = {kind[0]: kind[1] for kind in BASE_PIECES.values()}
        others = [(k, n) for k, n in b["kinds"].items() if k not in ("bed", "chest")]
        others.sort(key=lambda kn: (-kn[1], kn[0]))
        stuff += [f"{icons.get(k, '')} {_count(k, n)}" for k, n in others[:5]]
        if b["kinds"].get("chest"):
            stuff.append(f"📦 {b['kinds']['chest']} chest{'s' if b['kinds']['chest'] != 1 else ''}")
        line = " · ".join(stuff)
        builders = [n for n, _ in b["builders"]]
        if builders and set(builders) != set(b["beds"]):          # not just repeating the bed owners
            line += f" · built by {_names(builders)}"
        lines.append(line + "\n")
    if len(found_bases) > limit:
        lines.append(f"…and {len(found_bases) - limit} smaller base{'s' if len(found_bases) - limit != 1 else ''}")
    footer = (f"{outposts} lone workbench{'es' if outposts != 1 else ''} not counted · " if outposts else "") + \
        "from the last world save · put a house pin on the cartography table to name a base"
    return {"title": f"🏠 Bases{' in ' + server_name if server_name else ''}: {len(found_bases)}", "color": 0x8D6E63,
            "description": "\n".join(lines).strip()[:4000] or "No bases yet: a bed or a ward, a forge or a few chests "
                                                                  "makes one.",
            "footer": {"text": footer}}


def render_signs(found: dict, search: str = "", server_name: str = "", limit: int = 30) -> dict:
    q = search.strip().lower()
    signs = sorted((sg for sg in found.get("signs", []) if q in sg["text"].lower()), key=lambda sg: sg["text"].lower())
    lines = []
    for sg in signs[:limit]:
        portal = min((pt for pt in found.get("portals", []) if pt["tag"]),
                     key=lambda pt: math.hypot(pt["x"] - sg["x"], pt["z"] - sg["z"]), default=None)
        near = (f" · near the {md(portal['tag'])} portal"
                if portal and math.hypot(portal["x"] - sg["x"], portal["z"] - sg["z"]) <= 100 else "")
        lines.append(f"🪧 “{md(sg['text'])}”{near} · {where(sg['x'], sg['z'])}")
    if len(signs) > limit:
        lines.append(f"…and {len(signs) - limit} more: add a search to narrow it down")
    if not signs:
        empty = (f"No sign says **{md(search.strip())}**." if q else
                 "No signs yet. Build one with the hammer, then hover it and press E to write on it.")
    title = f"🪧 Signs{' in ' + server_name if server_name else ''}: {len(signs)}"
    if q:
        title += f" with “{search.strip()[:40]}”"
    return {"title": title, "color": 0x8D6E63, "description": "\n".join(lines)[:4000] if signs else empty,
            "footer": {"text": "What's written on the signs in the world · from the last world save"}}


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


# -- production stations (/muninn stations) ---------------------------------------------
def _item_label(item_id: str) -> str:
    import items
    name = items.name_of(stable_hash(item_id))
    return item_id if name.startswith("unknown item") else name


def _kinds_of(names: list) -> str:
    """ "mead base: minor healing, mead base: tasty" : each kind once."""
    return ", ".join(_item_label(n).lower() for n in dict.fromkeys(names))


def _tally(names: list) -> str:
    """ "13 scrap iron, 4 copper ore" from a list of item IDs, most first."""
    counts: dict = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{n} {_item_label(i).lower()}" for i, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def station_report(found: dict) -> dict:
    """{"places": [(place name, [station pieces])], "no_fuel": [(place, kind, fuel)], "collect":
    {what: (amount, collectors)}, "mead": [(place, piece)] ready, "brewing": [(place, piece, seconds left)]}."""
    working = [p for p in found.get("pieces", []) if p["kind"] in QUEUE_STATIONS or p["kind"] in COLLECTORS
               or p["kind"] == "fermenter"]
    places, placed = [], set()
    wanted = {id(w) for w in working}
    found_bases = [b for b in bases(found)[0] if any(id(p) in wanted for p in b["members"])]
    names = [base_name(b) for b in found_bases]
    for b, name in zip(found_bases, names):
        here = [p for p in b["members"] if id(p) in wanted]
        if names.count(name) > 1:                          # two "Ingrid's base": say which
            name += f" (x {b['x']:.0f}, z {b['z']:.0f})"
        places.append((name, here))
        placed.update(id(p) for p in here)
    for p in working:
        if id(p) not in placed:
            places.append((where(p["x"], p["z"]), [p]))
    clock = found.get("clock")
    out: dict = {"places": places, "no_fuel": [], "collect": {}, "mead": [], "brewing": []}
    for place, here in places:
        for p in here:
            fuel = QUEUE_STATIONS.get(p["kind"])
            if fuel and p.get("queue") and p.get("fuel", 0) < 1:
                out["no_fuel"].append((place, p["kind"], fuel))
            if p["kind"] in COLLECTORS and p.get("level"):
                amount, hives = out["collect"].get(COLLECTORS[p["kind"]], (0, 0))
                out["collect"][COLLECTORS[p["kind"]]] = (amount + p["level"], hives + 1)
            if p["kind"] == "fermenter" and p.get("content"):
                left = FERMENT_SECONDS - (clock - p["started"]) if clock and p.get("started") else None
                if left is not None and left <= 0:
                    out["mead"].append((place, p))
                else:
                    out["brewing"].append((place, p, left))
    return out


def _station_line(kind: str, here: list, clock: Optional[float]) -> str:
    emoji = {k[0]: k[1] for k in BASE_PIECES.values()}.get(kind, "")
    head = f"{emoji} {_count(kind, len(here)) if len(here) > 1 else kind[0].upper() + kind[1:]}"
    if kind in QUEUE_STATIONS:
        queue = [i for p in here for i in p.get("queue", [])]
        bits = [f"{len(queue)} waiting ({_tally(queue)})" if queue else "empty"]
        fuel = QUEUE_STATIONS[kind]
        if fuel:
            dry = sum(1 for p in here if p.get("queue") and p.get("fuel", 0) < 1)
            loaded = sum(int(p.get("fuel", 0)) for p in here)
            if dry:
                bits.append(f"⚠️ {dry} with no {fuel}" if len(here) > 1 else f"⚠️ no {fuel}")
            if loaded:
                bits.append(f"{loaded} {fuel} loaded")
        return f"{head}: " + " · ".join(bits)
    if kind == "fermenter":
        ready = [p for p in here if p.get("content") and clock and p.get("started")
                 and clock - p["started"] >= FERMENT_SECONDS]
        brewing = [p for p in here if p.get("content") and p not in ready]
        bits = []
        if ready:
            bits.append(f"{len(ready)} ready ({_kinds_of([p['content'] for p in ready])})")
        if brewing:
            lefts = [FERMENT_SECONDS - (clock - p["started"]) for p in brewing if clock and p.get("started")]
            soon = f", the first ready in about {max(1, round(min(lefts) / 60))} min" if lefts else ""
            bits.append(f"{len(brewing)} fermenting ({_kinds_of([p['content'] for p in brewing])}){soon}")
        return f"{head}: " + (" · ".join(bits) or "empty")
    total = sum(p.get("level", 0) for p in here)
    return f"{head}: " + (f"{total} {COLLECTORS[kind]} to collect" if total else "nothing yet")


def render_stations(found: dict, server_name: str = "", limit: int = 8) -> dict:
    report = station_report(found)
    if not report["places"]:
        return {"title": "🏭 Stations", "color": 0xE67E22,
                "description": "No smelters, kilns, fermenters or beehives in the world yet."}
    clock = found.get("clock")
    top = []
    if report["no_fuel"]:
        by: dict = {}
        for place, kind, fuel in report["no_fuel"]:
            by.setdefault(fuel, []).append(place)
        for fuel, where_ in by.items():
            counted = ", ".join(f"{pl}{f' ×{where_.count(pl)}' if where_.count(pl) > 1 else ''}"
                                for pl in dict.fromkeys(where_))
            top.append(f"⚠️ **Needs {fuel}:** {len(where_)} with something waiting and no {fuel} ({counted})")
    for what, (amount, n) in report["collect"].items():
        top.append(f"🍯 **Ready to collect:** {amount} {what} in {n} {'hive' if what == 'honey' else 'collector'}"
                   f"{'s' if n != 1 else ''}")
    if report["mead"]:
        top.append(f"🍺 **Ready to tap:** {len(report['mead'])} fermenter{'s' if len(report['mead']) != 1 else ''} "
                   f"({', '.join(dict.fromkeys(pl for pl, _ in report['mead']))})")
    lines = top + ([""] if top else [])
    for place, here in report["places"][:limit]:
        lines.append(f"**{place}**")                       # base names come escaped already
        kinds: dict = {}
        for p in here:
            kinds.setdefault(p["kind"], []).append(p)
        order = list(QUEUE_STATIONS) + ["fermenter"] + list(COLLECTORS)
        lines += [_station_line(k, kinds[k], clock) for k in order if k in kinds]
        lines.append("")
    if len(report["places"]) > limit:
        lines.append(f"…and {len(report['places']) - limit} more places with stations")
    return {"title": f"🏭 Stations{' in ' + server_name if server_name else ''}", "color": 0xE67E22,
            "description": "\n".join(lines).strip()[:4000],
            "footer": {"text": "From the last world save (every 30 minutes) · a station only works while someone "
                               "is nearby, and catches up when they come back"}}


# -- the explored map (/muninn explored) -------------------------------------------------
def explored_bounds(explored: bytes, margin: int = 40) -> Optional[tuple]:
    """(first col, first row, last col, last row) around the explored cells, with a margin."""
    rows = [r for r in range(MAP_SIZE) if 1 in explored[r * MAP_SIZE:(r + 1) * MAP_SIZE]]
    if not rows:
        return None
    cols = [explored.find(1, r * MAP_SIZE, (r + 1) * MAP_SIZE) - r * MAP_SIZE for r in rows]
    ends = [explored.rfind(1, r * MAP_SIZE, (r + 1) * MAP_SIZE) - r * MAP_SIZE for r in rows]
    return (max(0, min(cols) - margin), max(0, rows[0] - margin),
            min(MAP_SIZE - 1, max(ends) + margin), min(MAP_SIZE - 1, rows[-1] + margin))


def explored_map(found: dict, title: str = "What we've explored") -> Optional[bytes]:
    """A PNG of the explored map shared on the cartography table, zoomed to the explored
    part, with the bases, portals and the start. None without Pillow or a shared map."""
    explored = found.get("explored")
    if not explored:
        return None
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    box = explored_bounds(explored)
    if not box:
        return None
    c0, r0, c1, r1 = box
    side = max(c1 - c0, r1 - r0, 170) + 1                      # at least ~2 km across
    cc, rc = (c0 + c1) / 2, (r0 + r1) / 2
    c0 = int(max(0, min(MAP_SIZE - side, cc - side / 2)))
    r0 = int(max(0, min(MAP_SIZE - side, rc - side / 2)))
    size, top = 800, 50
    cells = Image.frombytes("L", (MAP_SIZE, MAP_SIZE), explored).crop((c0, r0, c0 + side, r0 + side))
    fog, land = (30, 32, 36), (196, 182, 140)
    mask = cells.point(lambda v: 255 if v else 0).transpose(Image.FLIP_TOP_BOTTOM).resize((size, size), Image.NEAREST)
    picture = Image.composite(Image.new("RGB", (size, size), land), Image.new("RGB", (size, size), fog), mask)
    img = Image.new("RGB", (size, size + top), (43, 45, 49))
    img.paste(picture, (0, top))
    draw = ImageDraw.Draw(img, "RGBA")
    try:
        font, small = ImageFont.load_default(size=20), ImageFont.load_default(size=13)
    except TypeError:                                              # Pillow before 10.1
        font = small = ImageFont.load_default()

    def px(x, z):
        col, row = x / MAP_CELL + MAP_SIZE / 2, z / MAP_CELL + MAP_SIZE / 2
        return (col - c0) / side * size, top + size - (row - r0) / side * size

    share = explored_share(explored)
    draw.text((16, 14), f"{title} · {share['percent']:.1f}% of the world", fill=(242, 243, 245), font=font)
    ex, ey = px(-WORLD_RADIUS, WORLD_RADIUS)
    fx, fy = px(WORLD_RADIUS, -WORLD_RADIUS)
    draw.ellipse([ex, ey, fx, fy], outline=(237, 66, 69, 120), width=2)          # the world's edge
    inside = lambda x, y: 0 <= x <= size and top <= y <= top + size            # noqa: E731
    labelled: list = []
    for p in found.get("portals", []):
        x, y = px(p["x"], p["z"])
        if inside(x, y):
            draw.polygon([(x, y - 4), (x + 4, y), (x, y + 4), (x - 4, y)], fill=(126, 87, 194))
    sx, sy = px(0, 0)
    if inside(sx, sy):
        draw.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], outline=(87, 242, 135), width=2)
        draw.text((sx + 9, sy - 7), "start", fill=(87, 242, 135), font=small, stroke_width=2, stroke_fill=(30, 32, 36))
        labelled.append((sx, sy))
    for b in bases(found)[0][:12]:
        x, y = px(b["x"], b["z"])
        if not inside(x, y):
            continue
        draw.rectangle([x - 5, y - 5, x + 5, y + 5], fill=(250, 166, 26), outline=(30, 32, 36))
        name = base_name(b, str)
        if b["pin"] or b["beds"] or b["builders"]:              # "nobody's named" isn't worth a label
            if all(math.hypot(x - a, y - c) > 40 for a, c in labelled):
                labelled.append((x, y))
                left = x > size - 170                            # near the right edge: write it to the left
                draw.text((x - 8 if left else x + 8, y), name, fill=(255, 255, 255), font=small,
                          anchor="rm" if left else "lm", stroke_width=2, stroke_fill=(30, 32, 36))
    km = side * MAP_CELL / 1000
    draw.text((size - 12, top + size - 10), f"{km:.1f} km across", fill=(242, 243, 245), font=small, anchor="rb",
              stroke_width=2, stroke_fill=(30, 32, 36))
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def render_explored(found: dict, server_name: str = "") -> dict:
    share = explored_share(found.get("explored"))
    if not share:
        return {"title": "🗺️ The explored map", "color": 0xC4B68C,
                "description": "No map shared on a cartography table yet. Build one (it needs a workbench "
                               "nearby), then interact with it to record your map. Everyone who records "
                               "adds what they've explored."}
    named = [b for b in bases(found)[0] if b["pin"]]
    lines = [f"**{share['percent']:.1f}%** of the world explored: {share['km2']:,.1f} km² of about "
             f"{math.pi * (WORLD_RADIUS / 1000) ** 2:,.0f} km²."]
    houses = sum(1 for p in found.get("pins", []) if p["icon"] == "house")
    lines.append(f"{len(found.get('pins', []))} pins on the table ({houses} houses) · "
                 f"{len(bases(found)[0])} bases, {len(named)} named by their house pin.")
    return {"title": f"🗺️ What we've explored{' in ' + server_name if server_name else ''}", "color": 0xC4B68C,
            "description": "\n".join(lines),
            "footer": {"text": "The map shared on the cartography table · record yours there to add to it"}}


# -- the weekly world digest ---------------------------------------------------------------
def snapshot(found: dict) -> dict:
    """What to remember of the world for next week's comparison (JSON-friendly)."""
    named, (_, unnamed_pieces) = builder_counts(found)
    return {"pieces": sum(found["builders"].values()), "builders": dict(named), "unnamed": unnamed_pieces,
            "portals": sorted(p["tag"] for p in found["portals"]),
            "ships": [[s["kind"], round(s["x"]), round(s["z"])] for s in found["ships"]],
            # With a time of death, owner and time say which tombstone it is (it may drift);
            # without one, its spot does.
            "tombstones": [[t["owner"], round(t["died"])] if t["died"] else [t["owner"], 0, round(t["x"]), round(t["z"])]
                           for t in found["tombstones"]],
            "tames": [[a["kind"], a["name"]] for a in found["tames"]],
            "explored": (explored_share(found.get("explored")) or {}).get("cells", 0)}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 or word.endswith(('lox', 'asksvin')) else 's'}"


def _match_ships(old: list, new: list, moved_m: float = 200) -> tuple:
    """(new ships, gone ships, moved count). Ships of a kind are paired with last week's
    closest pairs first, so a ship that stayed put keeps its match; a pair further apart
    than moved_m means it sailed somewhere."""
    pairs = sorted((math.hypot(o[1] - n[1], o[2] - n[2]), i, j)
                   for i, o in enumerate(old) for j, n in enumerate(new) if o[0] == n[0])
    used_old, used_new, moved = set(), set(), 0
    for dist, i, j in pairs:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        if dist > moved_m:
            moved += 1
    added = [n[0] for j, n in enumerate(new) if j not in used_new]
    gone = [o[0] for i, o in enumerate(old) if i not in used_old]
    return added, gone, moved


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
    # Carts are kept with the ships for /muninn ships, but a cart isn't a ship that sailed.
    ship_new, ship_gone, moved = _match_ships([s for s in old.get("ships", []) if s[0] != "cart"],
                                              [s for s in new["ships"] if s[0] != "cart"])
    bits = [f"{_plural(ship_new.count(k), k)} built" for k in sorted(set(ship_new))]
    bits += [f"{_plural(ship_gone.count(k), k)} gone" for k in sorted(set(ship_gone))]
    if moved:
        bits.append(f"{moved} {'ship' if moved == 1 else 'ships'} sailed somewhere new")
    if bits:
        lines.append("⛵ " + " · ".join(bits))
    from collections import Counter
    old_graves = Counter(tuple(g) for g in old.get("tombstones", []))
    new_graves = Counter(tuple(g) for g in new["tombstones"])
    recovered = sum((old_graves - new_graves).values())
    fresh = list((new_graves - old_graves).elements())
    if recovered or fresh:
        parts = [f"{recovered} recovered"] if recovered else []
        if fresh:
            parts.append(f"{len(fresh)} new ({', '.join(sorted({g[0] for g in fresh}))})")
        still = sum((new_graves & old_graves).values())
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
    before, after = old.get("explored", 0), new.get("explored", 0)
    if before and after > before:                      # only once there was a map to compare with
        world = math.pi * (WORLD_RADIUS / MAP_CELL) ** 2
        lines.append(f"🗺️ The map grew by **{(after - before) * MAP_CELL * MAP_CELL / 1e6:,.1f} km²**: "
                     f"{100 * after / world:.1f}% of the world explored")
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
    import items
    totals = sorted(stock(found).items(), key=lambda kv: -kv[1])
    print(f"{len(found['containers'])} containers ({len(searchable(found, tombstones=False))} searchable), "
          f"{len(totals)} kinds of item; the most:")
    for item, n in totals[:15]:
        print(f"  {items.name_of(item):<24} {n}")
    print(f"{len(found['signs'])} signs:")
    for s in found["signs"][:20]:
        print(f"  {s['text'][:40]:<40} {where(s['x'], s['z'])}")
    icons: dict = {}
    for pin in found["pins"]:
        icons[pin["icon"]] = icons.get(pin["icon"], 0) + 1
    print(f"{len(found['pins'])} pins on the cartography tables: "
          + ", ".join(f"{n} {k}" for k, n in sorted(icons.items(), key=lambda kv: -kv[1])) + "; the named houses:")
    for pin in sorted((p for p in found["pins"] if p["icon"] == "house" and p["name"]), key=lambda p: p["name"].lower()):
        print(f"  {pin['name'][:30]:<30} {where(pin['x'], pin['z'])}")
    share = explored_share(found["explored"])
    if share:
        on_map = sum(1 for p in found["pins"] if is_explored(found["explored"], p["x"], p["z"]))
        print(f"Explored map: {share['cells']} cells, {share['km2']:.1f} km², {share['percent']:.1f}% of the world; "
              f"{on_map} of {len(found['pins'])} pins are on explored ground")
    else:
        print("No explored map shared on a cartography table")
    kinds: dict = {}
    for piece in found["pieces"]:
        kinds[piece["kind"]] = kinds.get(piece["kind"], 0) + 1
    found_bases, outposts = bases(found)
    print(f"{len(found_bases)} bases ({outposts} lone workbenches left out), from "
          + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])) + ":")
    for b in found_bases[:20]:
        name = b["pin"] or ", ".join(b["beds"]) or ", ".join(n for n, _ in b["builders"][:2]) or "(no name)"
        print(f"  {name[:30]:<30} {b['pieces']:>4} beds, stations and chests  {where(b['x'], b['z'])}")
    print("Stations:")
    for line in render_stations(found)["description"].replace("**", "").splitlines():
        print(f"  {line}")
    print(f"{len(found['tames'])} tamed animals:")
    for a in sorted(found["tames"], key=lambda a: (a["kind"], a["name"].lower())):
        print(f"  {a['kind']:<12} {a['name'] or '(no name)':<16} {'*' * a['stars']:<3} {where(a['x'], a['z'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
