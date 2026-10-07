"""
/valheim wiki: a short card from the Valheim Wiki (valheim.fandom.com) for an item, creature,
boss, biome, building or place.

The wiki's MediaWiki API needs no key. A page's infobox (the stats table at its top) gives the
key facts, and its first paragraph the summary. The text is CC BY-SA, so every card credits
the wiki and links the page. Stdlib only; answers are cached for a few hours.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

API = "https://valheim.fandom.com/api.php"
SITE = "Valheim Wiki"
CACHE_SECONDS = 6 * 3600
_cache: dict = {}                     # (kind, key) -> (when, value)

# infobox kind -> [(parameter, label)], in the order shown. "health" and "weak/resist" are
# built from several parameters (see _fields).
FIELDS = {
    "creature": [("type", "Type"), ("location", "🗺️ Found in"), ("summon", "🔮 Summoned with"), ("health", "❤️ Health"),
                 ("weaknesses", "⚔️ Weaknesses"), ("drops", "🎁 Drops"), ("tameable", "🐾 Tameable"),
                 ("faction", "Faction")],
    "item": [("type", "Type"), ("source", "🔨 Made at / from"), ("materials", "🧱 Materials"),
             ("health", "❤️ Health"), ("stamina", "⚡ Stamina"), ("eitr", "🔷 Eitr"), ("duration", "⏳ Duration"),
             ("effect", "✨ Effect"), ("weight", "⚖️ Weight"), ("stack", "📦 Stack"), ("teleport", "🌀 Portals")],
    "weapon": [("type", "Type"), ("source", "🔨 Made at"), ("damage", "⚔️ Damage"), ("materials 1", "🧱 Materials"),
               ("block armor", "🛡️ Block"), ("crafting level", "Station level")],
    "armor": [("type", "Type"), ("source", "🔨 Made at"), ("armor", "🛡️ Armor"), ("materials 1", "🧱 Materials"),
              ("effect", "✨ Effect"), ("crafting level", "Station level")],
    "structure": [("type", "Type"), ("materials", "🧱 Materials"), ("durability", "Durability"), ("size", "Size"),
                  ("effects", "✨ Effects")],
    "biome": [("boss", "👑 Boss"), ("hostile", "⚔️ Hostile"), ("passive", "🦌 Passive"), ("unique", "💎 Resources"),
              ("dungeons", "🏚️ Dungeons"), ("npcs", "🧙 NPCs")],
    "location": [("type", "Type"), ("location", "🗺️ Found in"), ("inhabitants", "⚔️ Inside"),
                 ("resources", "💎 Resources")],
}
DAMAGE_TYPES = ("slash", "pierce", "blunt", "fire", "frost", "lightning", "poison", "spirit", "chop", "pickaxe")
RESISTANCES = (("veryweak", "very weak to"), ("weak", "weak to"), ("resistant", "resistant to"),
               ("veryresistant", "very resistant to"), ("immune", "immune to"))


# -- the API ----------------------------------------------------------------------------
def _get(params: dict, timeout: float = 10.0):
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-discord-monitor/1.0 (Discord bot)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _cached(kind: str, key, fetch):
    """fetch() once per CACHE_SECONDS. "Not found" (None) isn't kept, so a page created or
    a title corrected later is found right away."""
    now = time.time()
    hit = _cache.get((kind, key))
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = fetch()
    if len(_cache) > 500:
        _cache.clear()
    if value is not None:
        _cache[(kind, key)] = (now, value)
    return value


def search(text: str, limit: int = 10, timeout: float = 2.5) -> list:
    """Page titles matching what's typed so far, for autocomplete (Discord waits 3 s)."""
    text = text.strip()
    if not text:
        return []
    data = _cached("search", (text.lower(), limit), lambda: _get(
        {"action": "opensearch", "search": text, "limit": limit, "namespace": 0}, timeout))
    return list(data[1]) if isinstance(data, list) and len(data) > 1 else []


def page(title: str) -> Optional[dict]:
    """{"title", "url", "image", "wikitext"} for a page (following redirects), or None if
    there's no such page."""
    def fetch():
        parsed = _get({"action": "parse", "page": title, "prop": "wikitext", "redirects": 1})
        if "error" in parsed:
            return None
        real = parsed["parse"]["title"]
        info = _get({"action": "query", "prop": "pageimages|info", "inprop": "url", "pithumbsize": 300,
                     "titles": real, "redirects": 1})
        pg = next(iter(((info.get("query") or {}).get("pages") or {}).values()), {})
        return {"title": real, "url": pg.get("fullurl") or f"https://valheim.fandom.com/wiki/{urllib.parse.quote(real)}",
                "image": (pg.get("thumbnail") or {}).get("source"), "wikitext": parsed["parse"]["wikitext"]["*"]}
    # Exact title: wiki titles are case-sensitive after the first letter ("Deer trophy" is a
    # page, "Deer Trophy" isn't), so they mustn't share a cache entry.
    return _cached("page", title.strip(), fetch)


def lookup(text: str) -> Optional[dict]:
    """The page for what someone typed: that exact title, else the wiki's best match."""
    found = page(text)
    if found:
        return found
    titles = search(text, 1, timeout=10)
    return page(titles[0]) if titles else None


# -- wikitext ---------------------------------------------------------------------------
def _template_end(text: str, start: int) -> int:
    """The index just past the template opened by the "{{" at `start`."""
    depth, i = 0, start
    while i < len(text) - 1:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
        elif two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)


def _split_params(body: str) -> list:
    """Split a template's body on the "|" that aren't inside {{…}} or [[…]]."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        two = body[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            cur.append(two)
            i += 2
        elif two in ("}}", "]]"):
            depth -= 1
            cur.append(two)
            i += 2
        elif body[i] == "|" and depth == 0:
            parts.append("".join(cur))
            cur, i = [], i + 1
        else:
            cur.append(body[i])
            i += 1
    parts.append("".join(cur))
    return parts


def infobox(wikitext: str) -> tuple:
    """(kind, {parameter: raw value}) of the page's first infobox, e.g. ("creature", {...});
    ("", {}) without one."""
    m = re.search(r"\{\{\s*infobox[ _]", wikitext, re.I)
    if not m:
        return "", {}
    body = wikitext[m.start() + 2:_template_end(wikitext, m.start()) - 2]
    name, *params = _split_params(body)
    kind = re.sub(r"^infobox[ _]+", "", name.strip(), flags=re.I).replace("_", " ").lower()
    out = {}
    for p in params:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return kind, out


def _drop_templates(text: str, keep=None) -> str:
    """Remove every {{…}} (nested too); `keep(name, params)` may return text to put instead."""
    out, i = [], 0
    while True:
        j = text.find("{{", i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        end = _template_end(text, j)
        if keep:
            name, *params = _split_params(text[j + 2:end - 2])
            out.append(keep(name.strip().lower(), [p.strip() for p in params]) or "")
        i = end


def _item_link(name: str, params: list) -> str:
    """{{item link|Raspberries|8}} -> "Raspberries x8"; other templates vanish."""
    if name == "item link" and params:
        return params[0] + (f" x{params[1]}" if len(params) > 1 and params[1].isdigit() else "")
    return ""


def clean(value: str, bold: bool = False) -> str:
    """Wiki markup -> plain text (or Discord markdown with bold=True)."""
    t = re.sub(r"<!--.*?-->|<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", value, flags=re.S)
    t = re.sub(r"</?nowiki>|</?span[^>]*>|</?small>|</?center>", "", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = _drop_templates(t, _item_link)
    t = re.sub(r"\[\[(?:File|Image|Category):[^\]]*\]\]", "", t, flags=re.I)
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", t)
    t = re.sub(r"'''(.*?)'''", r"**\1**" if bold else r"\1", t)
    t = re.sub(r"''(.*?)''", r"*\1*" if bold else r"\1", t)
    t = re.sub(r"<[^>]+>", "", t)
    return t.strip()


def _lines(value: str) -> list:
    return [ln.strip().lstrip("*#: ").strip() for ln in clean(value).split("\n") if ln.strip().lstrip("*#: ").strip()]


def intro(wikitext: str, limit: int = 350) -> str:
    """The page's opening paragraph, as Discord markdown, cut at a sentence."""
    head = re.split(r"\n=+[^=\n]+=+", wikitext, maxsplit=1)[0]
    head = re.sub(r"</?(tabber|div)[^>]*>", "\n", head, flags=re.I)
    head = _drop_templates(head)
    head = re.sub(r"^\s*(\|-\||[\w :'-]+=)\s*$", "", head, flags=re.M)      # tabber leftovers
    paras = [p for p in (clean(x, bold=True) for x in re.split(r"\n\s*\n", head)) if p and not p.startswith("|")]
    text = re.sub(r"\s+", " ", paras[0]).strip() if paras else ""
    if len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        text = text[:cut + 1] if cut > limit // 3 else text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def _fields(kind: str, box: dict) -> list:
    fields = []
    for key, label in FIELDS.get(kind, []):
        if key == "health" and kind == "creature":
            hp = [clean(box.get(f"health {n}star", "")) for n in range(3)]
            hp = [h for h in hp if h]
            value = " / ".join(hp) + (f" (0–{len(hp) - 1}★)" if len(hp) > 1 else "")
        elif key == "weaknesses":
            value = "\n".join(f"{label_.capitalize()}: {', '.join(_lines(box[k]))}" for k, label_ in RESISTANCES
                              if _lines(box.get(k, "")))
        elif key in ("duration", "cooldown") and clean(box.get(key, "")).isdigit():
            seconds = int(clean(box[key]))
            value = f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} s"
        elif key == "damage":
            value = ", ".join(f"{clean(box[d])} {d}" for d in DAMAGE_TYPES if clean(box.get(d, "")))
        else:
            parts = _lines(box.get(key, ""))
            value = ", ".join(parts) if len(parts) > 1 and all(len(p) < 40 for p in parts) else "\n".join(parts)
        if value:
            fields.append({"name": label, "value": value[:1024], "inline": len(value) < 40})
    return fields[:12]


def card(pg: dict) -> dict:
    """The Discord embed for a page."""
    kind, box = infobox(pg["wikitext"])
    desc = intro(pg["wikitext"])
    quote = clean(box.get("description") or box.get("description 0star") or "")
    if quote and kind != "structure":
        desc = f"> *{quote}*\n\n{desc}".strip()
    out = {"title": f"📖 {pg['title']}", "url": pg["url"], "color": 0x8B5A2B,
           "description": (desc or "No summary on the wiki yet.")[:4096] + f"\n\n[Read more on the {SITE}]({pg['url']})",
           "fields": _fields(kind, box),
           "footer": {"text": f"From the {SITE} (valheim.fandom.com) · CC BY-SA"}}
    if pg.get("image"):
        out["thumbnail"] = {"url": pg["image"]}
    return out
