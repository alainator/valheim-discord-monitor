#!/usr/bin/env python3
"""
Render the public Valheim stats page (a single self-contained index.html) from the
SQLite database that the monitor fills.

    python stats_site.py --db valheim_stats.db --out /var/www/valheimstats/index.html

The monitor also calls render() directly on an interval. The file is written
atomically (temp + os.replace) so a web server never serves a half-written page.
Player names are user-controlled and this page is public, so every dynamic value
is HTML-escaped.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import time
from typing import Optional

import stats_db

try:  # icon URL normalisation lives with the rest of the Steam knowledge
    import steam as _steam
except Exception:  # noqa: BLE001 - stats_site can be run standalone
    _steam = None


def _ico(url):
    """Repair an achievement icon URL at render time (fixes rows stored before the fix)."""
    return _steam.icon_url(url) if (_steam and url) else url

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BANNER = os.path.join(HERE, "assets", "banner.webp")


# ---------------------------------------------------------------------------
# Formatting helpers.  Log timestamps were parsed as the server's wall clock and
# stored via timegm(), so time.gmtime(epoch) recovers that same wall clock.
# ---------------------------------------------------------------------------
def fmt_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds < 0:
        return "0m"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def fmt_hours(seconds: Optional[float]) -> str:
    return f"{(seconds or 0) / 3600:.1f}"


def fmt_date(epoch: Optional[int]) -> str:
    if not epoch:
        return "—"
    return time.strftime("%b %-d, %Y", time.gmtime(epoch))


def fmt_date_short(epoch: Optional[int]) -> str:
    if not epoch:
        return "\u2014"
    return time.strftime("%b %-d", time.gmtime(epoch))


def fmt_datetime(epoch: Optional[int]) -> str:
    if not epoch:
        return "—"
    return time.strftime("%b %-d, %-I:%M %p", time.gmtime(epoch))


def fmt_ago(epoch: Optional[int], now: Optional[int] = None) -> str:
    if not epoch:
        return "—"
    now = now or int(time.time())
    s = max(0, now - epoch)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def esc(v) -> str:
    return html.escape(str(v), quote=True)


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------
def banner_data_uri(cfg: dict) -> Optional[str]:
    path = (cfg.get("stats_site") or {}).get("banner", DEFAULT_BANNER)
    if not path or not os.path.exists(path):
        return None
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"webp": "image/webp", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/png")
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


def rank_rows(rows, cols, empty="No sagas recorded yet."):
    """cols: list of (header, fn(row)->str, css_class)."""
    if not rows:
        return f'<tr><td class="empty" colspan="{len(cols) + 1}">{esc(empty)}</td></tr>'
    out = []
    for i, r in enumerate(rows, 1):
        medal = {1: "gold", 2: "silver", 3: "bronze"}.get(i, "")
        cells = [f'<td class="rank {medal}">{i}</td>']
        for _, fn, cls in cols:
            cells.append(f'<td class="{cls}">{fn(r)}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(out)


def leaderboard(title, subtitle, rows, cols, empty):
    head = "".join(f'<th class="{cls}">{esc(h)}</th>' for h, _, cls in cols)
    return f"""
    <section class="board">
      <div class="board-head">
        <h2>{esc(title)}</h2>
        <p>{esc(subtitle)}</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th class="rank">#</th>{head}</tr></thead>
          <tbody>
            {rank_rows(rows, cols, empty)}
          </tbody>
        </table>
      </div>
    </section>"""


def render_html(db_path: str, cfg: dict) -> str:
    server_name = cfg.get("server_name", "Valheim Server")
    tz_label = (cfg.get("stats_site") or {}).get("timezone_label", "server time")
    top_n = int((cfg.get("stats_site") or {}).get("top_n", 10))
    now_real = int(time.time())          # true epoch — for Steam data, which uses real epochs

    conn = stats_db.connect(db_path)
    try:
        lb = stats_db.player_leaderboard(conn, top_n)
        deaths = stats_db.top_deaths(conn, top_n)
        visits = stats_db.top_sessions(conn, top_n)
        longest = stats_db.longest_single_sessions(conn, top_n)
        srv = stats_db.server_stats(conn)
        online = stats_db.currently_online(conn)
        recent = stats_db.recent_activity(conn, 12)
        ach = stats_db.achievements_leaderboard(conn, top_n)
        unlocks = stats_db.recent_unlocks(conn, 12)
        last_event = conn.execute("SELECT value FROM meta WHERE key='last_event_at'").fetchone()
        off_row = conn.execute("SELECT value FROM meta WHERE key='log_clock_offset'").fetchone()
    finally:
        conn.close()
    # Log timestamps are the SERVER's wall clock stored as if UTC. Shift "now" onto that
    # same clock so "x ago" is right and the Updated line reads in server time.
    try:
        clock_offset = int(off_row["value"]) if off_row and off_row["value"] else 0
    except (TypeError, ValueError):
        clock_offset = 0
    now = now_real - clock_offset
    last_event_at = int(last_event["value"]) if last_event and last_event["value"] else None

    max_play = max((r["total_seconds"] or 0 for r in lb), default=0) or 1

    # --- playtime board with inline ember bars ---
    def play_bar(r):
        pct = 100 * (r["total_seconds"] or 0) / max_play
        return (f'<div class="bar-cell"><span class="bar" style="width:{pct:.1f}%"></span>'
                f'<span class="bar-val">{esc(fmt_duration(r["total_seconds"]))}</span></div>')

    play_board = leaderboard(
        "Most Time in the Tenth Realm", "Total hours across all sessions", lb,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Playtime", play_bar, "num bar-col"),
         ("Sessions", lambda r: esc(r["sessions"]), "num hide-sm")],
        "No one has set foot in this world yet.")

    death_board = leaderboard(
        "Fallen Most Often", "Deaths — Odin keeps the tally", deaths,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Deaths", lambda r: f'<span class="death-num">{esc(r["deaths"])}</span>', "num")],
        "No deaths recorded — a cautious clan.")

    visit_board = leaderboard(
        "Most Visits", "Separate play sessions", visits,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Sessions", lambda r: esc(r["sessions"]), "num")],
        "No visits recorded yet.")

    longest_board = leaderboard(
        "Longest Single Session", "One unbroken stretch online", longest,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Length", lambda r: esc(fmt_duration(r["duration_seconds"])), "num"),
         ("When", lambda r: esc(fmt_date_short(r["login_at"])), "num hide-sm")],
        "No sessions recorded yet.")

    # --- server stat tiles ---
    tiles = [
        ("Total Play Hours", fmt_hours(srv.get("total_seconds")), "hours logged across everyone"),
        ("Vikings Seen", str(srv.get("unique_players", 0)), "distinct player names"),
        ("Peak Online", str(srv.get("peak_online", 0)),
         f'at once · {fmt_date(srv.get("peak_online_at"))}' if srv.get("peak_online_at") else "at once"),
        ("Total Sessions", str(srv.get("total_sessions", 0)), "logins recorded"),
        ("Total Deaths", str(srv.get("total_deaths", 0)), "and counting"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="tile-val">{esc(v)}</div>'
        f'<div class="tile-label">{esc(label)}</div><div class="tile-sub">{esc(sub)}</div></div>'
        for label, v, sub in tiles)

    # --- currently online panel ---
    if online:
        online_items = "".join(
            f'<li><span class="pip"></span><span class="name">{esc(o["player"])}</span>'
            f'<span class="since">since {esc(fmt_datetime(o["login_at"]))}</span></li>'
            for o in online)
        online_html = (f'<div class="online-head"><span class="pip"></span>'
                       f'<strong>{len(online)}</strong> in the Tenth Realm now</div>'
                       f'<ul class="online-list">{online_items}</ul>')
    else:
        online_html = ('<div class="online-head offline"><span class="pip"></span>'
                       'The halls are quiet</div>'
                       '<p class="online-empty">No vikings are online right now.</p>')

    # --- recent activity ---
    kind_word = {"login": "arrived", "logout": "departed", "death": "fell"}
    kind_cls = {"login": "in", "logout": "out", "death": "die"}
    recent_html = "".join(
        f'<li class="ev {kind_cls.get(r["kind"], "")}">'
        f'<span class="ev-name">{esc(r["player"])}</span> '
        f'<span class="ev-kind">{esc(kind_word.get(r["kind"], r["kind"]))}</span>'
        f'<span class="ev-time">{esc(fmt_ago(r["at"], now))}</span></li>'
        for r in recent) or '<li class="ev empty">Nothing has happened yet.</li>'

    # --- achievements tab ---
    def ach_card(r):
        persona = r.get("persona") or (r.get("names") or "").split(",")[0] or "Unknown viking"
        names = r.get("names") or ""
        chars = ", ".join(dict.fromkeys(n for n in names.split(",") if n and n != persona))
        avatar = r.get("avatar")
        # a failed avatar collapses to the same blank tile rather than a broken-image glyph
        avatar_html = (f'<img class="ava" src="{esc(avatar)}" alt="" loading="lazy" '
                       f'onerror="this.onerror=null;this.className=\'ava ava-blank\';'
                       f'this.src=\'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==\';">'
                       if avatar else '<div class="ava ava-blank"></div>')
        prof = r.get("profile_url")
        unlocked, total = r.get("unlocked"), r.get("total")
        if unlocked is None or not total:
            # communityvisibilitystate 3 means the *profile* is public, so a blocked
            # read is the separate "Game details" setting — a single dropdown to flip.
            # Naming the right setting is what actually gets people to opt in.
            if r.get("error") == "private":
                if r.get("visibility") == 3:
                    note, hint = "Game details are private", "Steam → Privacy Settings → Game details → Public"
                else:
                    note, hint = "Profile is private", "Steam → Privacy Settings → My profile → Public"
            else:
                note, hint = "No achievement data yet", ""
            body = (f'<div class="ach-note">{esc(note)}</div>'
                    + (f'<div class="ach-hint">{esc(hint)}</div>' if hint else ""))
        else:
            pct = round(100 * unlocked / total) if total else 0
            last = ""
            if r.get("last_unlock_name"):
                last = (f'<div class="ach-last">Latest: <b>{esc(r["last_unlock_name"])}</b> '
                        f'· {esc(fmt_date((r.get("last_unlock_at") or 0) - clock_offset or None))}</div>')
            body = (f'<div class="ach-count"><b>{unlocked}</b> / {total} '
                    f'<span class="ach-pct">{pct}%</span></div>'
                    f'<div class="ach-bar"><span style="width:{pct}%"></span></div>{last}')
        name_line = (f'<a class="ach-name" href="{esc(prof)}" target="_blank" rel="noopener">{esc(persona)}</a>'
                     if prof else f'<span class="ach-name">{esc(persona)}</span>')
        char_line = f'<div class="ach-char">as {esc(chars)}</div>' if chars else ""
        return f'<div class="ach-card">{avatar_html}<div class="ach-body">{name_line}{char_line}{body}</div></div>'

    if ach:
        ach_cards = "".join(ach_card(r) for r in ach)
        ach_html = f'<div class="ach-grid">{ach_cards}</div>'
        if unlocks:
            items = "".join(
                (f'<li>' + (f'<img class="ach-ico" src="{esc(_ico(u["icon"]))}" alt="" loading="lazy" '
                            f'data-fallback="{esc(u["icon"])}" '
                            f'onerror="if(this.dataset.fallback){{this.src=this.dataset.fallback;'
                            f'this.dataset.fallback=\'\';}}else{{this.style.display=\'none\';}}">'
                            if u.get("icon") else "")
                 + f'<span class="ach-uname">{esc(u["name"])}</span>'
                   f'<span class="ach-uwho">{esc(u.get("persona") or "")}</span>'
                   f'<span class="ach-uwhen">{esc(fmt_ago(u["unlocktime"], now_real))}</span></li>')
                for u in unlocks)
            ach_html += (f'<section class="board recent"><div class="board-head"><h2>Recent Unlocks</h2>'
                         f'<p>Latest achievements earned</p></div><ul class="unlock-feed">{items}</ul></section>')
    else:
        ach_html = ('<div class="empty-tab">No Steam achievements yet. They appear here as Steam '
                    'players connect and their public profiles are read.</div>')

    refresh = int((cfg.get("stats_site") or {}).get("refresh_seconds", 120))
    updated = fmt_datetime(now)

    banner = banner_data_uri(cfg)
    if banner:
        masthead = (f'<div class="masthead"><img src="{banner}" alt="{esc(server_name)}"></div>')
    else:
        masthead = f'<div class="masthead nomast"><h1 class="mast-title">{esc(server_name)}</h1></div>'

    return TEMPLATE.format(
        title=esc(f"{server_name} — Stats"),
        server_name=esc(server_name),
        masthead=masthead,
        online_html=online_html,
        tiles=tiles_html,
        play_board=play_board,
        death_board=death_board,
        visit_board=visit_board,
        longest_board=longest_board,
        recent=recent_html,
        achievements=ach_html,
        updated=esc(updated),
        tz_label=esc(tz_label),
        refresh=refresh,
        last_event_ago=esc(fmt_ago(last_event_at, now)),
        year=time.strftime("%Y", time.gmtime(now)),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IM+Fell+English+SC&family=Alegreya+Sans:ital,wght@0,400;0,500;0,700;1,400&display=swap" rel="stylesheet">
<style>
  :root {{
    --ground:#0e1417; --ground2:#080c0e;
    --stone:#171f20; --stone2:#1e2827; --edge:#33403c; --edge-warm:#4a3a2c;
    --bone:#e9e0cd; --muted:#a39c86; --faint:#726c5c;
    --gold:#d7a24a; --gold-dim:#9a6f27; --burg:#8f3030; --burg-soft:#a94a45;
    --moss:#8bbf6a; --blood:#c04b45;
    --shadow:0 3px 10px rgba(0,0,0,.5);
  }}
  * {{ box-sizing:border-box; }}
  html, body {{ margin:0; }}
  body {{
    background:
      radial-gradient(1100px 500px at 50% 0%, #14201f 0%, transparent 60%),
      linear-gradient(180deg, var(--ground) 0%, var(--ground2) 100%);
    background-attachment:fixed;
    color:var(--bone);
    font-family:"Alegreya Sans", "Segoe UI", system-ui, sans-serif;
    font-size:16px; line-height:1.55; min-height:100vh;
  }}
  .wrap {{ max-width:1060px; margin:0 auto; padding:0 20px 64px; }}
  a {{ color:var(--gold); }}
  h1, h2, .eyebrow, .tile-label, thead th, .display {{
    font-family:"IM Fell English SC", "IM Fell English", Georgia, serif;
    font-weight:400;
  }}

  /* Masthead — the carved banner runs full-bleed and fades into the page */
  .masthead {{ position:relative; width:100%; height:clamp(96px, 19vw, 196px); overflow:hidden;
               border-bottom:1px solid var(--edge-warm); }}
  .masthead img {{ width:100%; height:100%; object-fit:cover; object-position:center; display:block; }}
  .masthead::after {{ content:""; position:absolute; inset:0; pointer-events:none;
    background:linear-gradient(180deg, transparent 62%, rgba(14,20,23,.55) 88%, var(--ground) 100%); }}
  .masthead.nomast {{ display:flex; align-items:center; justify-content:center; background:var(--stone); }}
  .mast-title {{ font-size:clamp(1.8rem,5vw,3rem); color:var(--gold); margin:0; letter-spacing:.02em; }}

  .subhead {{ text-align:center; padding:20px 0 4px; }}
  .subhead .eyebrow {{ text-transform:uppercase; letter-spacing:.34em; font-size:.72rem; color:var(--burg-soft); margin:0; }}
  .subhead .server {{ font-family:"IM Fell English SC", Georgia, serif; font-size:1.5rem; color:var(--bone); margin:.15em 0 0; }}
  .tagline {{ color:var(--muted); margin:.15em 0 0; font-style:italic; }}

  /* Online banner */
  .online {{ margin:22px auto 0; max-width:520px;
    background:linear-gradient(180deg, var(--stone2), var(--stone));
    border:1px solid var(--edge); border-left:3px solid var(--burg); border-radius:6px;
    padding:15px 20px; box-shadow:var(--shadow); }}
  .online-head {{ font-family:"IM Fell English SC", Georgia, serif; font-size:1.15rem;
    display:flex; align-items:center; gap:10px; justify-content:center; }}
  .online-head strong {{ color:var(--moss); }}
  .online-head.offline {{ color:var(--muted); }}
  .pip {{ width:9px; height:9px; border-radius:50%; background:var(--moss); box-shadow:0 0 8px var(--moss); flex:none; }}
  .offline .pip {{ background:var(--faint); box-shadow:none; }}
  .online-list {{ list-style:none; margin:12px 0 0; padding:0; display:flex; flex-direction:column; gap:6px; }}
  .online-list li {{ display:flex; align-items:center; gap:10px; font-size:.98rem; }}
  .online-list .name {{ color:var(--bone); }}
  .online-list .since {{ color:var(--faint); font-size:.82rem; margin-left:auto; }}
  .online-empty {{ text-align:center; color:var(--faint); margin:8px 0 0; font-size:.92rem; }}

  /* Stat tiles */
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:14px; margin:30px 0 10px; }}
  .tile {{ background:var(--stone); border:1px solid var(--edge); border-top:2px solid var(--gold-dim);
    border-radius:6px; padding:18px 16px; text-align:center; }}
  .tile-val {{ font-family:"Alegreya Sans", sans-serif; font-weight:700; font-size:2.1rem; color:var(--gold);
    font-variant-numeric:tabular-nums; line-height:1.05; }}
  .tile-label {{ text-transform:uppercase; letter-spacing:.12em; font-size:.82rem; color:var(--bone); margin-top:6px; }}
  .tile-sub {{ color:var(--faint); font-size:.78rem; margin-top:2px; }}

  /* Boards */
  .board-lead {{ margin-top:24px; }}
  .board-lead .bar-col {{ width:52%; }}
  .boards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px,1fr)); gap:22px; margin-top:22px; }}
  .board {{ background:var(--stone); border:1px solid var(--edge); border-radius:8px; overflow:hidden; box-shadow:var(--shadow); }}
  .board-head {{ padding:15px 20px 12px; border-bottom:1px solid var(--edge);
    background:linear-gradient(180deg, rgba(143,48,48,.12), transparent); }}
  .board-head h2 {{ font-size:1.35rem; margin:0; color:var(--bone); letter-spacing:.01em; }}
  .board-head p {{ margin:1px 0 0; color:var(--muted); font-size:.84rem; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:.97rem; }}
  th, td {{ padding:10px 14px; text-align:left; }}
  thead th {{ text-transform:uppercase; letter-spacing:.1em; font-size:.74rem; color:var(--faint);
    border-bottom:1px solid var(--edge); }}
  tbody tr {{ border-bottom:1px solid rgba(51,64,60,.5); }}
  tbody tr:last-child {{ border-bottom:none; }}
  tbody tr:nth-child(odd) td {{ background:rgba(215,162,74,.03); }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .rank {{ width:2.4em; color:var(--faint); font-variant-numeric:tabular-nums; font-weight:700; }}
  .rank.gold {{ color:var(--gold); }}
  .rank.silver {{ color:#cfc9ba; }}
  .rank.bronze {{ color:#bd7c4c; }}
  .name {{ color:var(--bone); }}
  .name-col {{ white-space:nowrap; }}
  .death-num {{ color:var(--blood); font-weight:700; }}
  .empty {{ color:var(--faint); text-align:center; font-style:italic; padding:22px; }}

  /* Gold bars in the playtime board */
  .bar-col {{ width:46%; }}
  .bar-cell {{ position:relative; display:flex; align-items:center; justify-content:flex-end; gap:8px; min-width:120px; }}
  .bar {{ position:absolute; left:0; top:50%; transform:translateY(-50%); height:62%;
    background:linear-gradient(90deg, var(--gold-dim), var(--gold)); border-radius:2px; opacity:.4; }}
  .bar-val {{ position:relative; z-index:1; font-variant-numeric:tabular-nums; }}

  /* Recent activity */
  .recent {{ margin-top:30px; background:var(--stone); border:1px solid var(--edge); border-radius:8px; padding:18px 22px; }}
  .recent h2 {{ font-size:1.3rem; margin:0 0 10px; }}
  .feed {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; }}
  .ev {{ display:flex; align-items:baseline; gap:8px; padding:7px 0; border-bottom:1px solid rgba(51,64,60,.4); font-size:.95rem; }}
  .ev:last-child {{ border-bottom:none; }}
  .ev-name {{ color:var(--bone); }}
  .ev-kind {{ color:var(--muted); }}
  .ev.in .ev-kind {{ color:var(--moss); }}
  .ev.out .ev-kind {{ color:var(--faint); }}
  .ev.die .ev-kind {{ color:var(--blood); }}
  .ev-time {{ margin-left:auto; color:var(--faint); font-size:.82rem; }}

  footer {{ text-align:center; color:var(--faint); font-size:.82rem; margin-top:40px; line-height:1.7; }}
  footer .dot {{ opacity:.5; }}

  /* Tabs */
  .tabs {{ display:flex; gap:6px; justify-content:center; margin:26px 0 4px; flex-wrap:wrap; }}
  .tab-btn {{ font-family:"IM Fell English SC", Georgia, serif; font-size:1rem; letter-spacing:.02em;
    color:var(--muted); background:var(--stone); border:1px solid var(--edge); border-bottom:none;
    border-radius:7px 7px 0 0; padding:10px 20px; cursor:pointer; }}
  .tab-btn:hover {{ color:var(--bone); }}
  .tab-btn[aria-selected="true"] {{ color:var(--gold); background:var(--stone2);
    border-color:var(--gold-dim); box-shadow:0 -2px 0 var(--gold-dim) inset; }}
  .tab-rule {{ height:1px; background:var(--edge); margin:0 0 22px; }}
  .tab-panel[hidden] {{ display:none; }}

  /* Achievements */
  .ach-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(300px,1fr)); gap:16px; }}
  .ach-card {{ display:flex; gap:14px; background:var(--stone); border:1px solid var(--edge);
    border-top:2px solid var(--gold-dim); border-radius:8px; padding:16px; }}
  .ava {{ width:56px; height:56px; border-radius:6px; flex:none; background:var(--stone2); object-fit:cover; }}
  .ava-blank {{ border:1px solid var(--edge); }}
  .ach-body {{ flex:1; min-width:0; }}
  .ach-name {{ font-family:"IM Fell English SC", Georgia, serif; font-size:1.2rem; color:var(--bone);
    text-decoration:none; }}
  a.ach-name:hover {{ color:var(--gold); }}
  .ach-char {{ color:var(--faint); font-size:.8rem; margin:1px 0 8px; }}
  .ach-count {{ font-variant-numeric:tabular-nums; color:var(--bone); }}
  .ach-count b {{ color:var(--gold); font-size:1.15rem; }}
  .ach-pct {{ color:var(--muted); font-size:.85rem; margin-left:4px; }}
  .ach-bar {{ height:7px; background:var(--ground); border-radius:4px; overflow:hidden; margin:7px 0; }}
  .ach-bar span {{ display:block; height:100%; background:linear-gradient(90deg, var(--gold-dim), var(--gold)); }}
  .ach-last {{ color:var(--muted); font-size:.8rem; }}
  .ach-last b {{ color:var(--bone); font-weight:600; }}
  .ach-note {{ color:var(--faint); font-style:italic; font-size:.9rem; margin-top:6px; }}
  .ach-hint {{ color:var(--faint); font-size:.75rem; margin-top:4px; opacity:.7; letter-spacing:.02em; }}
  .unlock-feed {{ list-style:none; margin:0; padding:0; }}
  .unlock-feed li {{ display:flex; align-items:center; gap:10px; padding:7px 20px;
    border-bottom:1px solid rgba(51,64,60,.4); }}
  .unlock-feed li:last-child {{ border-bottom:none; }}
  .ach-ico {{ width:26px; height:26px; border-radius:4px; flex:none; }}
  .ach-uname {{ color:var(--bone); }}
  .ach-uwho {{ color:var(--muted); font-size:.85rem; }}
  .ach-uwhen {{ margin-left:auto; color:var(--faint); font-size:.8rem; }}
  .empty-tab {{ text-align:center; color:var(--faint); font-style:italic; padding:40px 20px;
    background:var(--stone); border:1px solid var(--edge); border-radius:8px; }}

  @media (max-width:520px) {{
    .hide-sm {{ display:none; }}
    th, td {{ padding:9px 14px; }}
    .wrap {{ padding:0 14px 48px; }}
    .tab-btn {{ padding:9px 14px; font-size:.92rem; }}
  }}
  @media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto; }} }}
</style>
</head>
<body>
  {masthead}
  <div class="wrap">
    <header class="subhead">
      <p class="eyebrow">Chronicle of</p>
      <p class="server">{server_name}</p>
      <p class="tagline">Sagas of the bold, tallies of the fallen &middot; updated live</p>
      <div class="online">{online_html}</div>
    </header>

    <div class="tabs" role="tablist">
      <button class="tab-btn" role="tab" data-tab="server" aria-selected="true">Server</button>
      <button class="tab-btn" role="tab" data-tab="vikings" aria-selected="false">Vikings</button>
      <button class="tab-btn" role="tab" data-tab="achievements" aria-selected="false">Achievements</button>
    </div>
    <div class="tab-rule"></div>

    <section class="tab-panel" data-panel="server">
      <div class="tiles">{tiles}</div>
      <div class="recent">
        <h2>Recent Deeds</h2>
        <ul class="feed">{recent}</ul>
      </div>
    </section>

    <section class="tab-panel" data-panel="vikings" hidden>
      <div class="board-lead">{play_board}</div>
      <div class="boards">
        {death_board}
        {longest_board}
        {visit_board}
      </div>
    </section>

    <section class="tab-panel" data-panel="achievements" hidden>
      {achievements}
    </section>

    <footer>
      Updated {updated} ({tz_label}) &middot; last activity {last_event_ago}<br>
      <span class="dot">Refreshes automatically &middot; {year}</span>
    </footer>
  </div>
  <script>
    (function() {{
      var btns = Array.prototype.slice.call(document.querySelectorAll('.tab-btn'));
      var panels = Array.prototype.slice.call(document.querySelectorAll('.tab-panel'));
      function show(name) {{
        btns.forEach(function(b) {{ b.setAttribute('aria-selected', b.dataset.tab === name); }});
        panels.forEach(function(p) {{ p.hidden = (p.dataset.panel !== name); }});
      }}
      btns.forEach(function(b) {{
        b.addEventListener('click', function() {{
          show(b.dataset.tab);
          try {{ history.replaceState(null, '', '#' + b.dataset.tab); }} catch (e) {{ location.hash = b.dataset.tab; }}
        }});
      }});
      var start = (location.hash || '').replace('#', '');
      if (['server','vikings','achievements'].indexOf(start) >= 0) show(start);
    }})();
  </script>
</body>
</html>"""


def render(db_path: str, out_path: str, cfg: dict) -> None:
    """Render the page and write it atomically to out_path."""
    html_text = render_html(db_path, cfg)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_text)
    os.replace(tmp, out_path)


def main():
    ap = argparse.ArgumentParser(description="Render the Valheim stats page from the database.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True, help="Path to index.html to write")
    ap.add_argument("--config", help="Optional config.json (for server_name, stats_site options)")
    ap.add_argument("--server-name", default=None)
    args = ap.parse_args()
    cfg = {}
    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
    if args.server_name:
        cfg["server_name"] = args.server_name
    render(args.db, args.out, cfg)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
