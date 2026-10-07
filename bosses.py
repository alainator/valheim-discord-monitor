"""
Boss progress and the in-game day, read from the world save. No mods: Valheim keeps the world's "global keys"
(defeated_eikthyr, defeated_gdking, …) in the save, and those say which bosses are down.

  * Valheim 1.0: worlds_local/<world>/_main.<N>.db2, a small header and then a gzip
    stream; the keys are plain strings inside it. The highest <N> is the newest save.
  * Older servers: worlds_local/<world>.db, uncompressed, keys as plain strings.

Both formats start with the world version (int32) and the game clock in seconds (float64),
so the day is known without unpacking anything; a day lasts DAY_SECONDS.

Plain functions, testable without a server.
"""

from __future__ import annotations

import glob
import math
import os
import re
import struct
import zlib
from typing import Optional

# The main bosses in the order you meet them: key -> (name, biome, emoji).
BOSSES = [
    ("defeated_eikthyr", "Eikthyr", "Meadows", "🦌"),
    ("defeated_gdking", "The Elder", "Black Forest", "🌳"),
    ("defeated_bonemass", "Bonemass", "Swamp", "🦠"),
    ("defeated_dragon", "Moder", "Mountains", "🐉"),
    ("defeated_goblinking", "Yagluth", "Plains", "💀"),
    ("defeated_queen", "The Queen", "Mistlands", "🕷️"),
    ("defeated_fader", "Fader", "Ashlands", "🔥"),
    ("defeated_kall", "Kall Fimbulbringer", "Deep North", "❄️"),
]
BOSS_KEYS = [b[0] for b in BOSSES]
NAMES = {b[0]: b[1] for b in BOSSES}
# Kall's exact key isn't known yet, so any defeated_* key naming Kall or Fimbul counts as him.
RE_KALL = re.compile(rb"defeated_[a-z0-9_]{0,24}?(?:kall|fimbul)")
GZIP = b"\x1f\x8b\x08"
DAY_SECONDS = 1800                  # Valheim's day length (EnvMan.m_dayLengthSec)


def name_of(key: str) -> str:
    """"defeated_gdking" -> "The Elder"."""
    return NAMES.get(key) or key.replace("defeated_", "").replace("_", " ").title()


def _keys_in(data: bytes) -> set:
    """The main bosses' keys in the data. Other defeated_* keys aren't bosses (Valheim
    sets some for other creatures), so they're ignored."""
    # Searched by exact name: the save stores strings length-prefixed with nothing
    # between them, so a pattern could swallow the next string's first byte.
    found = {k for k in BOSS_KEYS if k.encode() in data}
    if RE_KALL.search(data):
        found.add("defeated_kall")
    return found


def _unpack(data: bytes, limit: int = 64 << 20) -> Optional[bytes]:
    """The first gzip stream in a 1.0 save file, unpacked (the file itself if none), or
    None if it can't be unpacked (e.g. read while Valheim was writing it)."""
    start = data.find(GZIP, 0, 4096)
    if start < 0:
        return data
    try:
        return zlib.decompressobj(31).decompress(data[start:], limit)
    except zlib.error:
        return None


def _save_number(path: str) -> int:
    m = re.search(r"_main\.(\d+)\.db2$", path)
    return int(m.group(1)) if m else -1


def world_save(save_dir: str, world: Optional[str] = None) -> Optional[str]:
    """The newest save file of the live world (not a backup): a 1.0 _main.<N>.db2, or an
    older <world>.db. With several worlds and no `world`, the most recently saved one."""
    base = os.path.join(save_dir, "worlds_local")
    found = []
    for path in glob.glob(os.path.join(base, "*", "_main.*.db2")):
        folder = os.path.basename(os.path.dirname(path))
        if "_backup_" in folder or (world and folder != world):
            continue
        try:                                  # Valheim may remove an older save meanwhile
            found.append((os.path.getmtime(path), _save_number(path), path))
        except OSError:
            continue
    for path in glob.glob(os.path.join(base, "*.db")):
        name = os.path.splitext(os.path.basename(path))[0]
        if "_backup_" in name or (world and name != world):
            continue
        try:
            found.append((os.path.getmtime(path), 0, path))
        except OSError:
            continue
    if not found:
        return None
    if world:                                 # one world: its highest save number
        return max(found, key=lambda f: (f[1], f[0]))[2]
    return max(found)[2]


def read_keys(save_dir: str, world: Optional[str] = None) -> Optional[set]:
    """The bosses' defeated_* keys in the live world's save, or None if there's no save
    (or it couldn't be read)."""
    path = world_save(save_dir, world)
    if not path:
        return None
    with open(path, "rb") as f:
        data = f.read()
    if path.endswith(".db2"):
        data = _unpack(data)
        if data is None:
            return None
    return _keys_in(data)


def world_time(save_dir: str, world: Optional[str] = None) -> Optional[float]:
    """The game clock (seconds) in the live world's newest save, or None without a readable
    save. It's written with every save (every 30 minutes and at shutdown)."""
    path = world_save(save_dir, world)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        version, seconds = struct.unpack("<id", head)
    except (OSError, struct.error):
        return None
    if not (0 < version < 1000 and math.isfinite(seconds) and 0 <= seconds < 1e12):
        return None
    return seconds


def day_of(seconds: float) -> int:
    """The day number Valheim shows ("Day 142") for a game clock in seconds. A new world
    starts at 2040 s, on day 1."""
    return int(seconds // DAY_SECONDS)


def progress(keys: set) -> dict:
    """{"down": [names, in order], "count", "total", "next": name or None}."""
    down = [name_of(k) for k in BOSS_KEYS if k in keys]
    nxt = next((name_of(k) for k in BOSS_KEYS if k not in keys), None)
    return {"down": down, "count": len(down), "total": len(BOSSES), "next": nxt}


def channel_name(keys: Optional[set]) -> Optional[str]:
    if keys is None:
        return None
    p = progress(keys)
    tail = f" · next: {p['next']}" if p["next"] else " · all down!"
    return f"🏆 Bosses: {p['count']}/{p['total']}{tail}"[:100]


def render(keys: set, server_name: str = "", when: Optional[dict] = None) -> dict:
    """/muninn bosses: every main boss with ✅ or ⬜, and when it fell (if the bot saw it)."""
    when = when or {}
    lines = []
    for key, name, biome, emoji in BOSSES:
        done = key in keys
        at = f" · <t:{int(when[key])}:d>" if done and when.get(key) else ""
        lines.append(f"{'✅' if done else '⬜'} {emoji} **{name}** ({biome}){at}")
    p = progress(keys)
    return {"title": f"🏆 Boss progress{' in ' + server_name if server_name else ''}: {p['count']}/{p['total']}",
            "description": "\n".join(lines), "color": 0xC27C0E}


def announcement(key: str, keys: set, server_name: str = "") -> dict:
    """The post when a boss falls."""
    p = progress(keys)
    _, name, biome, _ = next(b for b in BOSSES if b[0] == key)
    title = f"⚔️ {name} has fallen!"
    desc = f"The {biome} boss is defeated{' in ' + server_name if server_name else ''}."
    desc += f" **{p['count']}/{p['total']}** bosses down" + (f"; next: **{p['next']}**." if p["next"] else
                                                               ". Every boss is down. Skål! 🍻")
    return {"title": title, "description": desc, "color": 0xC27C0E}
