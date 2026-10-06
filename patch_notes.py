"""
Valheim patch notes from Steam's public news feed, for the admin bot.

Iron Gate posts every patch as a Steam announcement tagged "patchnotes". The feed needs no
API key (ISteamNews/GetNewsForApp). Its text is Steam's BBCode, which is turned into
Discord markdown here and cut to fit an embed, with a link to the full notes. Stdlib only.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request

APP_ID = 892970  # Valheim
FEED = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_PAGE = "https://store.steampowered.com/news/app/{app}/view/{gid}"
LIMIT = 1800              # characters of notes in a post; the rest is behind the link

RE_PATCH_TITLE = re.compile(r"\b(patch|hotfix)\b", re.I)
RE_PUBLIC_TEST = re.compile(r"public[ -]?test|\bPTB\b|\bbeta\b", re.I)


def fetch(count: int = 10, timeout: float = 20.0) -> list:
    """The latest Steam announcements for Valheim, newest first."""
    url = FEED + "?" + urllib.parse.urlencode({"appid": APP_ID, "count": count,
                                               "feeds": "steam_community_announcements"})
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-discord-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", errors="replace"))
    return list((data.get("appnews") or {}).get("newsitems") or [])


def is_public_test(item: dict) -> bool:
    return bool(RE_PUBLIC_TEST.search(item.get("title") or ""))


def is_patch(item: dict, public_test: bool = False) -> bool:
    """A patch or hotfix post. Public test patches only when asked for: they don't reach a
    server on the normal branch."""
    title = item.get("title") or ""
    if not ("patchnotes" in (item.get("tags") or []) or RE_PATCH_TITLE.search(title)):
        return False
    return public_test or not is_public_test(item)


def link(item: dict) -> str:
    return STEAM_PAGE.format(app=item.get("appid") or APP_ID, gid=item.get("gid", ""))


def to_markdown(text: str) -> str:
    """Steam BBCode -> Discord markdown: headings and bold become bold, list items bullets,
    links markdown links; images, videos and unknown tags are dropped."""
    t = html.unescape(text or "").replace("\r\n", "\n")
    t = re.sub(r"\[(img|previewyoutube|video)[^\]]*\].*?\[/\1\]", "", t, flags=re.I | re.S)
    t = re.sub(r"\{STEAM_CLAN_IMAGE\}\S*", "", t)
    t = re.sub(r"\[h[1-6]\](.*?)\[/h[1-6]\]", lambda m: f"\n**{m.group(1).strip()}**\n", t, flags=re.I | re.S)
    t = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", lambda m: f"\x01{m.group(2).strip()}\x02({m.group(1).strip()})",
               t, flags=re.I | re.S)
    t = re.sub(r"\[url\](.*?)\[/url\]", r"\1", t, flags=re.I | re.S)
    for tag, md in (("b", "**"), ("i", "*"), ("u", "__"), ("strike", "~~")):
        t = re.sub(rf"\[{tag}\](.*?)\[/{tag}\]", lambda m, md=md: f"{md}{m.group(1).strip()}{md}" if m.group(1).strip()
                   else "", t, flags=re.I | re.S)
    t = re.sub(r"\[\*\]\s*", "\n• ", t)
    t = re.sub(r"\[/?p\]", "\n", t, flags=re.I)
    t = re.sub(r"^[ \t]*[*-][ \t]+", "• ", t, flags=re.M)        # plain "* item" lines
    t = re.sub(r"\[quote[^\]]*\](.*?)\[/quote\]", lambda m: "\n".join("> " + ln for ln in m.group(1).strip().split("\n")),
               t, flags=re.I | re.S)
    t = re.sub(r"\[/?[a-z0-9*]+(=[^\]]*)?\]", "", t, flags=re.I)  # [list], [hr], [/*], anything else
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"^(\*\*[^\n]+\*\*)\n\n+(?=• )", r"\1\n", t, flags=re.M)   # a heading sits on its list
    return t.replace("\x01", "[").replace("\x02", "]").strip()   # links, kept from the tag cleanup


def shorten(text: str, limit: int = LIMIT) -> tuple:
    """(text, cut): at most `limit` characters, cut at a line break when there is one."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip(), True


def embed(item: dict, limit: int = LIMIT) -> dict:
    """The Discord embed for one patch post."""
    body, cut = shorten(to_markdown(item.get("contents") or ""), limit)
    url = link(item)
    tail = f"\n\n[Read the full patch notes on Steam]({url})" if cut else f"\n\n[On Steam]({url})"
    title = ("🧪 " if is_public_test(item) else "🛠️ ") + (item.get("title") or "Valheim patch")[:240]
    out = {"title": title, "url": url, "description": (body + tail)[:4096], "color": 0x4C7A9E,
           "footer": {"text": "Valheim patch notes from Iron Gate"}}
    if item.get("date"):
        from datetime import datetime, timezone
        out["timestamp"] = datetime.fromtimestamp(int(item["date"]), timezone.utc).isoformat()
    return out


def new_patches(items: list, seen: set, public_test: bool = False) -> list:
    """Patch posts not in `seen`, oldest first (the order to post them in)."""
    found = [i for i in items if is_patch(i, public_test) and str(i.get("gid")) not in seen]
    return sorted(found, key=lambda i: int(i.get("date") or 0))
