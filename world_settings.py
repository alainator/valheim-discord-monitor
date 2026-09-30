#!/usr/bin/env python3
"""
Valheim world settings (preset, modifiers, setkeys) kept in a small env file that the
server's systemd unit reads:

    # /etc/systemd/system/valheimserver.service
    EnvironmentFile=-/home/valheim/world-settings.env
    ExecStart=... -crossplay $WORLD_ARGS -logFile ...

    # /home/valheim/world-settings.env
    WORLD_ARGS=-modifier raids less

This one file is both:
  * a library for the Discord bot (the allowed values, parsing, the /odin settings view), and
  * the host-side writer, installed as /home/valheim/valheim-world-settings.py and run as
    the valheim user by valheim-bot-request.sh. It only ever writes the WORLD_ARGS line,
    and only with values from the tables below, so a request can't smuggle in other
    launch options.

    python3 valheim-world-settings.py --file /home/valheim/world-settings.env show
    python3 valheim-world-settings.py --file ... set modifier raids less
    python3 valheim-world-settings.py --file ... set preset hard
    python3 valheim-world-settings.py --file ... set setkey nomap on
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile

# From Valheim's dedicated-server launch options. "normal" / "default" mean "not set".
PRESETS = ("default", "casual", "easy", "hard", "hardcore", "immersive", "hammer")
MODIFIERS = {
    "combat": ("veryeasy", "easy", "normal", "hard", "veryhard"),
    "deathpenalty": ("casual", "veryeasy", "easy", "normal", "hard", "hardcore"),
    "resources": ("muchless", "less", "normal", "more", "muchmore"),
    "raids": ("none", "muchless", "less", "normal", "more", "muchmore"),
    "portals": ("casual", "normal", "hard", "veryhard"),
}
SETKEYS = ("nomap", "playerevents", "passivemobs", "nobuildcost")

DESCRIPTIONS = {
    "combat": "How hard enemies hit and how tough they are",
    "deathpenalty": "What you lose when you die",
    "resources": "How much you get from mining, chopping and drops",
    "raids": "How often raids (random events) happen",
    "portals": "What may go through portals",
    "nomap": "No map or minimap",
    "playerevents": "Raids are based on each player's progress, not the world's",
    "passivemobs": "Enemies don't attack unless provoked",
    "nobuildcost": "Building costs nothing",
}


class State:
    def __init__(self, preset=None, modifiers=None, setkeys=None, other=None):
        self.preset = preset
        self.modifiers = dict(modifiers or {})
        self.setkeys = set(setkeys or ())
        self.other = list(other or [])   # anything we don't manage, kept as is

    def copy(self) -> "State":
        return State(self.preset, self.modifiers, self.setkeys, self.other)

    def __eq__(self, o):
        return (isinstance(o, State) and (self.preset, self.modifiers, self.setkeys, self.other)
                == (o.preset, o.modifiers, o.setkeys, o.other))


def parse_args(text: str) -> State:
    st, toks = State(), shlex.split(text or "")
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "-preset" and i + 1 < len(toks):
            st.preset = toks[i + 1].lower()
            i += 2
        elif t == "-modifier" and i + 2 < len(toks):
            st.modifiers[toks[i + 1].lower()] = toks[i + 2].lower()
            i += 3
        elif t == "-setkey" and i + 1 < len(toks):
            st.setkeys.add(toks[i + 1].lower())
            i += 2
        else:
            st.other.append(t)
            i += 1
    return st


def format_args(st: State) -> str:
    parts = []
    if st.preset and st.preset != "default":
        parts += ["-preset", st.preset]
    for k in MODIFIERS:                                   # stable order
        if k in st.modifiers and st.modifiers[k] != "normal":
            parts += ["-modifier", k, st.modifiers[k]]
    for k in SETKEYS:
        if k in st.setkeys:
            parts += ["-setkey", k]
    return " ".join(parts + [shlex.quote(o) for o in st.other])


def apply(st: State, kind: str, key: str, value: str = "") -> State:
    """A new State with one change. Raises ValueError for anything not allowed."""
    kind, key, value = kind.lower(), key.lower(), (value or "").lower()
    new = st.copy()
    if kind == "preset":
        if key not in PRESETS:
            raise ValueError(f"unknown preset {key!r} (allowed: {', '.join(PRESETS)})")
        new.preset = None if key == "default" else key
    elif kind == "modifier":
        if key not in MODIFIERS:
            raise ValueError(f"unknown modifier {key!r} (allowed: {', '.join(MODIFIERS)})")
        if value not in MODIFIERS[key]:
            raise ValueError(f"{key} can be {', '.join(MODIFIERS[key])}, not {value!r}")
        if value == "normal":
            new.modifiers.pop(key, None)
        else:
            new.modifiers[key] = value
    elif kind == "setkey":
        if key not in SETKEYS:
            raise ValueError(f"unknown setting {key!r} (allowed: {', '.join(SETKEYS)})")
        if value not in ("on", "off"):
            raise ValueError("setkey needs on or off")
        (new.setkeys.add if value == "on" else new.setkeys.discard)(key)
    else:
        raise ValueError(f"unknown kind {kind!r} (preset, modifier or setkey)")
    return new


def describe(st: State) -> list:
    """Human-readable lines for the current settings."""
    lines = [f"Preset: **{st.preset or 'default'}**"]
    for k in MODIFIERS:
        lines.append(f"{k}: **{st.modifiers.get(k, 'normal')}**")
    for k in SETKEYS:
        lines.append(f"{k}: **{'on' if k in st.setkeys else 'off'}**")
    if st.other:
        lines.append("Other (left alone): `" + " ".join(st.other) + "`")
    return lines


# -- the env file ---------------------------------------------------------------
def read_file(path: str) -> State:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("WORLD_ARGS="):
                    value = line[len("WORLD_ARGS="):].strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    return parse_args(value)
    except FileNotFoundError:
        pass
    return State()


def write_file(path: str, st: State) -> str:
    args = format_args(st)
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".world-settings.", dir=folder)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Valheim world settings, read by valheimserver.service (EnvironmentFile).\n"
                "# Changed from Discord with /odin modifier, /odin preset, /odin setkey.\n"
                f"WORLD_ARGS={args}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return args


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Show or change Valheim world settings (WORLD_ARGS).")
    ap.add_argument("--file", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("kind")
    s.add_argument("key")
    s.add_argument("value", nargs="?", default="")
    a = ap.parse_args(argv)
    st = read_file(a.file)
    if a.cmd == "show":
        print(format_args(st) or "(no world settings: all normal)")
        return 0
    try:
        new = apply(st, a.kind, a.key, a.value)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 2
    print(f"WORLD_ARGS={write_file(a.file, new)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
