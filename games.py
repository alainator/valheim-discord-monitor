"""
Games built on what the log already tells us. Each one is off until the config turns it on:

  * Odin's heckling: the death post is swapped for a roast when a death is worth one (a
    streak, the day's first, a long run ended). No extra posts, and it doesn't need the bot.
  * Odin's Wager: a round opens when the first player arrives. People bet Valor, a fake
    currency with no use outside this game, on who dies first and how many deaths there
    will be. Odds come from the server's own death history.

Functions take an sqlite3 connection (stats_db.connect), like community.py.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from typing import Optional

NOBODY = "*nobody"                       # the "nobody dies" pick; no character can have this name
FIRST, TOTAL = "first", "total"          # the two bets in a round
OVER, UNDER = 0, 1                       # the choices in the total bet

HECKLES = {
    "first": ["**{player}** opens the day's death count. Odin marks the ledger.",
              "First blood today, and it belongs to **{player}**.",
              "**{player}** volunteers to test today's dangers. They're dangerous.",
              "The Valkyries had a quiet morning, until **{player}**."],
    "streak": ["**{player}** has died {streak} times in {minutes} minutes. Odin requests a pattern change.",
               "That's {streak} in {minutes} minutes for **{player}**. The Valkyries are charging for parking.",
               "**{player}** is on a {streak}-death streak. Even Loki says that's enough.",
               "{streak} deaths in {minutes} minutes. **{player}** has a season ticket to Valhalla."],
    "quick": ["**{player}** lasted {seconds} seconds. A fine effort.",
              "**{player}** barely unpacked. Back in {seconds} seconds.",
              "{seconds} seconds of freedom for **{player}**. Odin barely looked up."],
    "survivor": ["**{player}** survived {hours} hours before falling. The skalds will sing of it, briefly.",
                 "{hours} hours without dying, then **{player}** remembered who they are.",
                 "Odin raises his horn: {hours} hours alive. **{player}** fell, but it was a good run."],
    "any": ["**{player}** has died. Odin is watching, and taking notes.",
            "**{player}** has been reassigned to the ground.",
            "**{player}** tried that. It didn't work.",
            "**{player}** has gone to check on the tombstone.",
            "A moment of silence for **{player}**'s inventory.",
            "**{player}** has died. Huginn saw everything. Muninn will remember."],
}


class Heckler:
    """Picks a roast for a death, or nothing for an ordinary one. Times are the log's own,
    so a streak survives a slow poll. State is kept in memory: after a restart the streaks
    start again, which only costs a joke."""

    def __init__(self, cfg: Optional[dict] = None, rng: Optional[random.Random] = None):
        cfg = cfg or {}
        self.chance = float(cfg.get("chance", 0.35))
        self.streak_seconds = float(cfg.get("streak_minutes", 10)) * 60
        self.quick_seconds = float(cfg.get("quick_seconds", 90))
        self.survivor_seconds = float(cfg.get("survivor_hours", 2)) * 3600
        custom = cfg.get("lines") or {}
        self.lines = {k: list(custom.get(k) or v) for k, v in HECKLES.items()}
        self.rng = rng or random.Random()
        self.deaths: dict = {}               # player -> times of their recent deaths
        self.alive_since: dict = {}          # player -> when they logged in or last died
        self.day = None                      # the log's date of the day's first death
        self.last_line: dict = {}            # kind -> the line used last, to avoid repeats

    def observe(self, ev) -> None:
        """Feed it login and death events. A death worth a roast gets ev.extra["heckle"]."""
        ts = ev.extra.get("ts") or int(time.time())
        if ev.kind == "login":
            self.alive_since[ev.player] = ts
        elif ev.kind == "death":
            line = self._react(ev.player, ts)
            if line:
                ev.extra["heckle"] = line

    def _react(self, player: str, ts: int) -> Optional[str]:
        recent = [t for t in self.deaths.get(player, ()) if ts - t <= self.streak_seconds] + [ts]
        self.deaths[player] = recent
        since = self.alive_since.get(player)
        self.alive_since[player] = ts
        day = ts // 86400
        first_today = day != self.day
        self.day = day
        alive = ts - since if since is not None else None
        if len(recent) >= 2:
            return self._line("streak", streak=len(recent),
                              minutes=max(1, round((ts - recent[0]) / 60)))
        if alive is not None and alive <= self.quick_seconds:
            return self._line("quick", seconds=alive)
        if alive is not None and alive >= self.survivor_seconds:
            return self._line("survivor", hours=f"{alive / 3600:.1f}".rstrip("0").rstrip("."))
        if first_today:
            return self._line("first")
        return self._line("any") if self.rng.random() < self.chance else None

    def _line(self, kind: str, **fields) -> str:
        options = [x for x in self.lines[kind] if x != self.last_line.get(kind)] or self.lines[kind]
        line = self.rng.choice(options)
        self.last_line[kind] = line
        # {player} is left for Discord.post, which escapes the name.
        for key, value in fields.items():
            line = line.replace("{" + key + "}", str(value))
        return line


# ---------------------------------------------------------------------------
# Odin's Wager: odds
# ---------------------------------------------------------------------------
def _odds(chance: float) -> float:
    """What a win pays per Valor bet, with 5% kept back so the books can't be gamed for free."""
    return min(10.0, max(1.2, round(0.95 / max(chance, 0.001), 1)))


def death_history(conn, days: int = 30) -> dict:
    """Deaths in the server's last `days` of play, measured back from the newest session."""
    newest = conn.execute("SELECT MAX(login_at) FROM play_sessions").fetchone()[0]
    if not newest:
        return {"players": {}, "per_day": [], "played_days": 0}
    since = newest - days * 86400
    players = {r[0]: r[1] for r in conn.execute(
        "SELECT player, COUNT(*) FROM deaths WHERE died_at >= ? GROUP BY player", (since,))}
    played = [r[0] for r in conn.execute(
        "SELECT DISTINCT login_at / 86400 FROM play_sessions WHERE login_at >= ?", (since,))]
    by_day = {r[0]: r[1] for r in conn.execute(
        "SELECT died_at / 86400, COUNT(*) FROM deaths WHERE died_at >= ? GROUP BY 1", (since,))}
    return {"players": players, "per_day": [by_day.get(d, 0) for d in played], "played_days": len(played)}


def build_markets(conn, online: list, most_players: int = 6) -> dict:
    """The round's bets: who dies first (anyone playing lately, and nobody) and a line for
    the total. Rare deaths pay more; both sides of the total pay the same."""
    history = death_history(conn)
    recent = [r[0] for r in conn.execute(
        "SELECT player, MAX(login_at) AS last FROM play_sessions GROUP BY player "
        "ORDER BY last DESC LIMIT ?", (most_players,))]
    players = list(dict.fromkeys(list(online) + recent))[:most_players]
    per_day = history["per_day"]
    quiet = sum(1 for n in per_day if n == 0)
    nobody = min(0.7, max(0.1, (quiet + 1) / (len(per_day) + 2)))
    weights = {p: history["players"].get(p, 0) + 1 for p in players}
    total = sum(weights.values()) or 1
    first = [{"pick": p, "odds": _odds((1 - nobody) * w / total)}
             for p, w in sorted(weights.items(), key=lambda x: -x[1])]
    first.append({"pick": NOBODY, "odds": _odds(nobody)})
    line = int(statistics.median(per_day)) + 0.5 if per_day else 2.5
    return {FIRST: first, TOTAL: {"line": line, "odds": 1.9}}


# ---------------------------------------------------------------------------
# Odin's Wager: Valor
# ---------------------------------------------------------------------------
def account(conn, user_id, start: int = 100) -> dict:
    conn.execute("INSERT OR IGNORE INTO valor(user_id, balance, updated_at) VALUES (?, ?, ?)",
                 (str(user_id), start, int(time.time())))
    conn.commit()
    return dict(conn.execute("SELECT * FROM valor WHERE user_id = ?", (str(user_id),)).fetchone())


def pity(conn, user_id, floor: int, gift: int, now: Optional[float] = None) -> int:
    """Someone too broke to bet gets a gift, at most once a day, so nobody's locked out.
    Returns how much they were given."""
    now = int(now if now is not None else time.time())
    a = account(conn, user_id)
    if a["balance"] >= floor or now - a["last_pity"] < 86400:
        return 0
    conn.execute("UPDATE valor SET balance = balance + ?, last_pity = ?, updated_at = ? WHERE user_id = ?",
                 (gift, now, now, str(user_id)))
    conn.commit()
    return gift


def leaderboard(conn, limit: int = 10) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM valor ORDER BY balance DESC, biggest_win DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------------------
# Odin's Wager: rounds
# ---------------------------------------------------------------------------
def _round(row) -> Optional[dict]:
    if not row:
        return None
    r = dict(row)
    r["markets"] = json.loads(r["markets"])
    return r


def open_round(conn, markets: dict, closes_at: int, now: Optional[float] = None) -> int:
    now = int(now if now is not None else time.time())
    cur = conn.execute("INSERT INTO wager_rounds(opened_at, closes_at, markets) VALUES (?, ?, ?)",
                       (now, closes_at, json.dumps(markets)))
    conn.commit()
    return cur.lastrowid


def get_round(conn, round_id: int) -> Optional[dict]:
    return _round(conn.execute("SELECT * FROM wager_rounds WHERE id = ?", (round_id,)).fetchone())


def current_round(conn) -> Optional[dict]:
    return _round(conn.execute("SELECT * FROM wager_rounds WHERE status = 'open' "
                               "ORDER BY id DESC LIMIT 1").fetchone())


def last_opened(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(opened_at), 0) FROM wager_rounds").fetchone()[0]


def set_round_message(conn, round_id: int, channel_id, message_id) -> None:
    conn.execute("UPDATE wager_rounds SET channel_id = ?, message_id = ? WHERE id = ?",
                 (str(channel_id), str(message_id), round_id))
    conn.commit()


def mark_closed_shown(conn, round_id: int) -> None:
    conn.execute("UPDATE wager_rounds SET closed_shown = 1 WHERE id = ?", (round_id,))
    conn.commit()


def round_bets(conn, round_id: int) -> list:
    return [dict(r) for r in conn.execute("SELECT * FROM wager_bets WHERE round_id = ?", (round_id,))]


def betting_open(r: Optional[dict], now: Optional[float] = None) -> bool:
    now = now if now is not None else time.time()
    return bool(r) and r["status"] == "open" and not r["first_death"] and now < r["closes_at"]


def pick_of(r: dict, market: str, index: int) -> Optional[tuple]:
    """(what the bet is on, what it pays) for a button, or None for one that doesn't exist."""
    if market == FIRST and 0 <= index < len(r["markets"][FIRST]):
        option = r["markets"][FIRST][index]
        return option["pick"], option["odds"]
    if market == TOTAL and index in (OVER, UNDER):
        return ("over", "under")[index], r["markets"][TOTAL]["odds"]
    return None


def place_bet(conn, round_id: int, user_id, market: str, index: int, stake: int,
              own_characters: tuple = (), start: int = 100, now: Optional[float] = None) -> tuple:
    """Take a bet. `stake` 0 means everything they have. Returns (True, what was staked)
    or (False, why not)."""
    now = int(now if now is not None else time.time())
    r = get_round(conn, round_id)
    if not betting_open(r, now):
        return False, "Betting has closed on this round."
    picked = pick_of(r, market, index)
    if not picked:
        return False, "That bet isn't on the board."
    pick, odds = picked
    if market == FIRST and pick in own_characters:
        return False, "Nobody bets on their own death here. Odin has seen that trick."
    if conn.execute("SELECT 1 FROM wager_bets WHERE round_id = ? AND user_id = ? AND market = ?",
                    (round_id, str(user_id), market)).fetchone():
        return False, "You've already bet on that this round."
    balance = account(conn, user_id, start)["balance"]
    stake = balance if stake == 0 else stake
    if stake <= 0 or stake > balance:
        return False, f"You only have **{balance}** Valor."
    conn.execute("UPDATE valor SET balance = balance - ?, updated_at = ? WHERE user_id = ?",
                 (stake, now, str(user_id)))
    conn.execute("INSERT INTO wager_bets(round_id, user_id, market, pick, stake, odds, placed_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (round_id, str(user_id), market, pick, stake, odds, now))
    conn.commit()
    return True, stake


def _settle(conn, round_id: int, market: str, winner: str, now: int) -> list:
    """Pay one bet's winners, once. Returns the settled bets."""
    done = []
    for b in conn.execute("SELECT * FROM wager_bets WHERE round_id = ? AND market = ? AND payout IS NULL",
                          (round_id, market)).fetchall():
        b = dict(b)
        b["payout"] = int(b["stake"] * b["odds"]) if b["pick"] == winner else 0
        conn.execute("UPDATE wager_bets SET payout = ? WHERE round_id = ? AND user_id = ? AND market = ?",
                     (b["payout"], round_id, b["user_id"], market))
        conn.execute("UPDATE valor SET balance = balance + ?, wins = wins + ?, losses = losses + ?, "
                     "biggest_win = MAX(biggest_win, ?), updated_at = ? WHERE user_id = ?",
                     (b["payout"], 1 if b["payout"] else 0, 0 if b["payout"] else 1, b["payout"], now, b["user_id"]))
        done.append(b)
    return done


def record_death(conn, round_id: int, player: str, now: Optional[float] = None) -> Optional[list]:
    """Count a death. The first one ends betting and settles who-dies-first: returns those
    settled bets for that death, None for any later one."""
    now = int(now if now is not None else time.time())
    r = get_round(conn, round_id)
    if not r or r["status"] != "open":
        return None
    first = not r["first_death"]
    conn.execute("UPDATE wager_rounds SET deaths = deaths + 1, first_death = COALESCE(first_death, ?) "
                 "WHERE id = ?", (player, round_id))
    settled = _settle(conn, round_id, FIRST, player, now) if first else None
    conn.commit()
    return settled


def finish_round(conn, round_id: int, now: Optional[float] = None) -> Optional[dict]:
    """End a round and pay what's left. None if it's already over."""
    now = int(now if now is not None else time.time())
    r = get_round(conn, round_id)
    if not r or r["status"] != "open":
        return None
    settled = []
    if not r["first_death"]:
        settled += _settle(conn, round_id, FIRST, NOBODY, now)
    side = "over" if r["deaths"] > r["markets"][TOTAL]["line"] else "under"
    settled += _settle(conn, round_id, TOTAL, side, now)
    conn.execute("UPDATE wager_rounds SET status = 'done', finished_at = ? WHERE id = ?", (now, round_id))
    conn.commit()
    return {"round": get_round(conn, round_id), "side": side, "bets": settled}


# ---------------------------------------------------------------------------
# Odin's Wager: what the posts say
# ---------------------------------------------------------------------------
def pick_label(pick: str) -> str:
    return "Nobody dies" if pick == NOBODY else pick


def render_round(r: dict, bets: list, open_now: bool) -> dict:
    t = r["markets"][TOTAL]
    lines = ["**Who dies first?**"]
    lines += [f"{'🕊️' if o['pick'] == NOBODY else '💀'} {pick_label(o['pick'])} · pays **{o['odds']}×**"
              for o in r["markets"][FIRST]]
    lines += ["", f"**Total deaths this session:** over or under **{t['line']}** · pays **{t['odds']}×**", ""]
    lines.append(f"Bets close <t:{r['closes_at']}:R>, or at the first death." if open_now
                 else "Bets are closed. Odin is watching.")
    if bets:
        lines.append(f"🪙 {len(bets)} bet{'s' if len(bets) != 1 else ''} · {sum(b['stake'] for b in bets)} Valor at stake")
    return {"title": f"🎲 Odin's Wager · round {r['id']}", "description": "\n".join(lines), "color": 0xC27C0E,
            "footer": {"text": "Valor is a fake currency. It only buys bragging rights."}}


def render_first_blood(player: str, bets: list) -> str:
    lines = [f"🩸 **First blood: {player}.** Betting is closed."]
    lines += [f"<@{b['user_id']}> called it: **+{b['payout']}** Valor" for b in bets if b["payout"]]
    return "\n".join(lines)


def render_result(result: dict) -> dict:
    r = result["round"]
    line = r["markets"][TOTAL]["line"]
    lines = [f"**{r['deaths']}** death{'s' if r['deaths'] != 1 else ''} (line {line}): **{result['side']}** wins."]
    if not r["first_death"]:
        lines.append("🕊️ Nobody died. Odin is disappointed.")
    won = [b for b in result["bets"] if b["payout"]]
    lines += [f"<@{b['user_id']}> **+{b['payout']}** Valor" for b in sorted(won, key=lambda b: -b["payout"])[:10]]
    if result["bets"] and not won:
        lines.append("The house takes it all.")
    return {"title": f"⚖️ Odin's Wager · round {r['id']} is settled", "description": "\n".join(lines),
            "color": 0xF1C40F}


def render_how_to_bet(r: Optional[dict], guild_id=None, now: Optional[float] = None) -> str:
    """How betting works, and what's happening with the current round if there is one."""
    how = ("**How to bet:** a round opens when someone joins the server. Press a pick on its "
           "post, then choose your stake. There's no command.")
    if not r:
        return f"{how}\n🎲 No round right now."
    if not betting_open(r, now):
        return f"{how}\n🎲 Round {r['id']} is under way, and betting has closed."
    where = (f"[Bet here](https://discord.com/channels/{guild_id}/{r['channel_id']}/{r['message_id']})"
             if guild_id and r.get("message_id") else "Find its post to bet")
    return f"{how}\n🎲 Round {r['id']} is open. {where}, closes <t:{r['closes_at']}:R>."


def render_leaderboard(rows: list, you: Optional[dict] = None, how: str = "") -> dict:
    lines = [f"**{i}.** <@{r['user_id']}> · **{r['balance']}** Valor · {r['wins']}W {r['losses']}L"
             for i, r in enumerate(rows, 1)]
    board = "\n".join(lines) or "Nobody has placed a bet yet."
    embed = {"title": "🪙 Valor", "color": 0xC27C0E,
             "description": f"{board}\n\n{how}" if how else board}
    if you:
        embed["footer"] = {"text": f"You: {you['balance']} Valor · best win {you['biggest_win']}"}
    return embed
