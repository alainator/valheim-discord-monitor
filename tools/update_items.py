#!/usr/bin/env python3
"""Rebuild data/items.json, Valheim's item IDs and their names, from the Valheim Wiki.

Chests in the world save store each item as the hash of its ID ("Wood", "BronzeNails"), so
/muninn find and stock need the list of IDs to show names. The wiki's item, weapon and
armour infoboxes have every ID. Run after a big game update:

    python3 tools/update_items.py
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import wiki  # noqa: E402

TEMPLATES = ("Template:Infobox item", "Template:Infobox weapon", "Template:Infobox armor", "Template:Infobox armour")


def get(**params):
    url = wiki.API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-discord-monitor/1.0 (item list)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    titles = set()
    for tpl in TEMPLATES:
        cont: dict = {}
        while True:
            d = get(action="query", list="embeddedin", eititle=tpl, eilimit=500, einamespace=0, **cont)
            titles |= {e["title"] for e in d["query"]["embeddedin"]}
            if "continue" not in d:
                break
            cont = d["continue"]
    items: dict = {}
    pages = sorted(titles)
    for k in range(0, len(pages), 50):
        d = get(action="query", prop="revisions", rvprop="content", rvslots="main", titles="|".join(pages[k:k + 50]))
        for pg in d["query"]["pages"].values():
            text = (pg.get("revisions") or [{}])[0].get("slots", {}).get("main", {}).get("*", "")
            for m in re.finditer(r"\{\{\s*infobox[ _](?:item|weapon|armou?r)\b", text, re.I):   # tabbed pages: several
                body = text[m.start() + 2:wiki._template_end(text, m.start()) - 2]
                params = {}
                for part in wiki._split_params(body)[1:]:
                    if "=" in part:
                        key, value = part.split("=", 1)
                        params[key.strip().lower()] = value.strip()
                ident = re.match(r"[A-Za-z0-9_]+", params.get("id", ""))
                if ident:
                    items.setdefault(ident.group(), wiki.clean(params.get("title") or "") or pg["title"])
        time.sleep(0.3)
    path = os.path.join(ROOT, "data", "items.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}"
                                   for k, v in sorted(items.items())) + "\n}\n")
    print(f"{len(items)} items from {len(pages)} pages -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
