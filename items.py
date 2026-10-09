"""Valheim item IDs and their names (data/items.json, from the wiki: tools/update_items.py).

The world save stores a chest's items as hashes of their IDs; this turns them back into
names. An ID the list doesn't know (a new item) shows as "unknown item".
"""

from __future__ import annotations

import json
import os
import struct
from typing import Optional

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "items.json")
_by_hash: Optional[dict] = None


def _table() -> dict:
    global _by_hash
    if _by_hash is None:
        import world_objects
        try:
            with open(_PATH, encoding="utf-8") as f:
                names = json.load(f)
        except (OSError, ValueError):
            names = {}
        _by_hash = {world_objects.stable_hash(k): (k, v) for k, v in names.items()}
    return _by_hash


def name_of(item_hash: int) -> str:
    """The item's name ("Bronze nails"), or "unknown item (1a2b3c4d)"."""
    hit = _table().get(item_hash)
    return hit[1] if hit else f"unknown item ({struct.pack('<I', item_hash).hex()})"


def id_of(item_hash: int) -> Optional[str]:
    """The item's ID ("BronzeNails"), or None."""
    hit = _table().get(item_hash)
    return hit[0] if hit else None
