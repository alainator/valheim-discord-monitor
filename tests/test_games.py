import asyncio
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import games  # noqa: E402
import stats_db  # noqa: E402
import valheim_discord_monitor as vdm  # noqa: E402

try:
    import discord  # noqa: F401
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None

DAY = 86400
T0 = 20000 * DAY


def death(player, ts):
    return vdm.Event("death", player, {"ts": ts})


class HecklerTest(unittest.TestCase):
    def heckle(self, h, player, ts):
        ev = death(player, ts)
        h.observe(ev)
        return ev.extra.get("heckle")

    def test_days_first_death(self):
        self.assertIn(self.heckle(games.Heckler({"chance": 0}, random.Random(1)), "Ingrid", T0), games.HECKLES["first"])

    def test_ordinary_death_gets_nothing_at_chance_zero(self):
        h = games.Heckler({"chance": 0}, random.Random(1))
        self.heckle(h, "Ingrid", T0)
        self.assertIsNone(self.heckle(h, "Bjorn", T0 + 60))

    def test_streak_counts_and_fills_in_the_numbers(self):
        h = games.Heckler({"lines": {"streak": ["{player}: {streak} in {minutes}"]}})
        self.heckle(h, "Ingrid", T0)
        self.heckle(h, "Ingrid", T0 + 120)
        self.assertEqual(self.heckle(h, "Ingrid", T0 + 240), "{player}: 3 in 4")

    def test_streak_only_counts_the_window(self):
        h = games.Heckler({"chance": 0}, random.Random(1))
        self.heckle(h, "Ingrid", T0)
        self.assertIsNone(self.heckle(h, "Ingrid", T0 + 11 * 60))

    def test_quick_death_after_arriving(self):
        h = games.Heckler({"lines": {"quick": ["{seconds}s"]}})
        h.observe(vdm.Event("login", "Bjorn", {"ts": T0}))
        h.day = T0 // DAY
        self.assertEqual(self.heckle(h, "Bjorn", T0 + 40), "40s")

    def test_long_survival(self):
        h = games.Heckler({"lines": {"survivor": ["{hours}h"]}})
        h.observe(vdm.Event("login", "Bjorn", {"ts": T0}))
        self.assertEqual(self.heckle(h, "Bjorn", T0 + 9000), "2.5h")

    def test_never_repeats_a_line_back_to_back(self):
        h = games.Heckler({"chance": 1})
        last = None
        for i in range(30):
            h.day = (T0 + i * 3600) // DAY
            line = self.heckle(h, "Ingrid", T0 + i * 3600)
            self.assertNotEqual(line, last)
            last = line

    def post(self, ev):
        d = vdm.Discord("https://example.invalid/hook", use_embeds=False, show_count=False)
        with mock.patch.object(vdm.Discord, "send") as send:
            d.post(ev, "S", {"death"})
        return send.call_args.args[0]["content"]

    def test_the_death_post_uses_the_roast_with_the_name_escaped(self):
        ev = death("A_b", T0)
        ev.extra["heckle"] = "roast {player}"
        text = self.post(ev)
        self.assertIn("roast " + vdm._escape_md("A_b"), text)
        self.assertNotIn("has died", text)

    def test_no_roast_means_the_normal_death_post(self):
        self.assertIn("has died", self.post(death("Ingrid", T0)))


class WagerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.conn = stats_db.connect(os.path.join(self.dir.name, "s.db"))
        self.addCleanup(self.conn.close)

    def history(self):
        for day in range(10):
            for player in ("Ingrid", "Bjorn"):
                self.conn.execute("INSERT INTO play_sessions(player, login_at, last_seen_at) VALUES (?, ?, ?)",
                                  (player, T0 + day * DAY, T0 + day * DAY + 3600))
            for i in range(day % 4):
                self.conn.execute("INSERT INTO deaths(player, died_at) VALUES (?, ?)",
                                  ("Bjorn" if i else "Ingrid", T0 + day * DAY + i))
        self.conn.commit()

    def round(self, now=1000):
        self.history()
        return games.open_round(self.conn, games.build_markets(self.conn, ["Ingrid"]), now + 900, now)

    def pick(self, rid, name):
        options = games.get_round(self.conn, rid)["markets"][games.FIRST]
        return [o["pick"] for o in options].index(name)

    def balance(self, uid):
        return games.account(self.conn, uid)["balance"]

    def test_who_dies_more_pays_less(self):
        self.history()
        m = games.build_markets(self.conn, [])
        odds = {o["pick"]: o["odds"] for o in m[games.FIRST]}
        self.assertLess(odds["Ingrid"], odds["Bjorn"])
        self.assertIn(games.NOBODY, odds)
        self.assertEqual(m[games.TOTAL]["line"] % 1, 0.5)

    def test_an_empty_server_still_has_a_board(self):
        m = games.build_markets(self.conn, ["Ingrid"])
        self.assertEqual([o["pick"] for o in m[games.FIRST]], ["Ingrid", games.NOBODY])
        self.assertEqual(m[games.TOTAL]["line"], 2.5)

    def test_bet_takes_the_stake_and_a_win_pays_the_quoted_odds(self):
        rid = self.round()
        i = self.pick(rid, "Bjorn")
        odds = games.get_round(self.conn, rid)["markets"][games.FIRST][i]["odds"]
        self.assertEqual(games.place_bet(self.conn, rid, 1, games.FIRST, i, 10, now=1000), (True, 10))
        self.assertEqual(self.balance(1), 90)
        bets = games.record_death(self.conn, rid, "Bjorn", now=1100)
        self.assertEqual(bets[0]["payout"], int(10 * odds))
        self.assertEqual(self.balance(1), 90 + int(10 * odds))

    def test_a_lost_bet_stays_lost(self):
        rid = self.round()
        games.place_bet(self.conn, rid, 1, games.FIRST, self.pick(rid, "Bjorn"), 10, now=1000)
        games.record_death(self.conn, rid, "Ingrid", now=1100)
        self.assertEqual(self.balance(1), 90)
        self.assertEqual(games.account(self.conn, 1)["losses"], 1)

    def test_nobody_wins_only_when_the_round_ends_with_no_deaths(self):
        rid = self.round()
        games.place_bet(self.conn, rid, 1, games.FIRST, self.pick(rid, games.NOBODY), 10, now=1000)
        result = games.finish_round(self.conn, rid, now=2000)
        self.assertGreater(result["bets"][0]["payout"], 0)

    def test_over_and_under(self):
        rid = self.round()
        line = games.get_round(self.conn, rid)["markets"][games.TOTAL]["line"]
        games.place_bet(self.conn, rid, 1, games.TOTAL, games.OVER, 10, now=1000)
        games.place_bet(self.conn, rid, 2, games.TOTAL, games.UNDER, 10, now=1000)
        for _ in range(int(line) + 1):
            games.record_death(self.conn, rid, "Ingrid", now=1100)
        result = games.finish_round(self.conn, rid, now=2000)
        self.assertEqual(result["side"], "over")
        self.assertEqual(self.balance(1), 90 + 19)
        self.assertEqual(self.balance(2), 90)

    def test_everyone_is_paid_only_once(self):
        rid = self.round()
        games.place_bet(self.conn, rid, 1, games.FIRST, self.pick(rid, "Ingrid"), 10, now=1000)
        games.record_death(self.conn, rid, "Ingrid", now=1100)
        paid = self.balance(1)
        self.assertIsNone(games.record_death(self.conn, rid, "Ingrid", now=1200))
        games.finish_round(self.conn, rid, now=2000)
        self.assertIsNone(games.finish_round(self.conn, rid, now=2100))
        self.assertEqual(self.balance(1), paid)

    def test_no_bets_after_the_first_death_or_the_deadline(self):
        rid = self.round()
        games.record_death(self.conn, rid, "Ingrid", now=1100)
        self.assertFalse(games.place_bet(self.conn, rid, 1, games.TOTAL, games.OVER, 10, now=1100)[0])
        rid2 = games.open_round(self.conn, games.build_markets(self.conn, []), 1900, 1000)
        self.assertFalse(games.place_bet(self.conn, rid2, 1, games.TOTAL, games.OVER, 10, now=1900)[0])
        self.assertEqual(self.balance(1), 100)

    def test_one_bet_per_market_and_never_more_than_you_have(self):
        rid = self.round()
        self.assertTrue(games.place_bet(self.conn, rid, 1, games.TOTAL, games.OVER, 10, now=1000)[0])
        self.assertFalse(games.place_bet(self.conn, rid, 1, games.TOTAL, games.UNDER, 10, now=1000)[0])
        self.assertFalse(games.place_bet(self.conn, rid, 1, games.FIRST, 0, 500, now=1000)[0])
        self.assertEqual(self.balance(1), 90)

    def test_all_in(self):
        rid = self.round()
        self.assertEqual(games.place_bet(self.conn, rid, 1, games.TOTAL, games.OVER, 0, now=1000), (True, 100))
        self.assertEqual(self.balance(1), 0)
        self.assertFalse(games.place_bet(self.conn, rid, 1, games.FIRST, 0, 0, now=1000)[0])

    def test_nobody_bets_on_their_own_death(self):
        rid = self.round()
        i = self.pick(rid, "Ingrid")
        ok, why = games.place_bet(self.conn, rid, 1, games.FIRST, i, 10, ("Ingrid",), now=1000)
        self.assertFalse(ok)
        self.assertIn("own death", why)
        self.assertEqual(self.balance(1), 100)

    def test_a_bet_that_does_not_exist(self):
        rid = self.round()
        self.assertFalse(games.place_bet(self.conn, rid, 1, games.FIRST, 99, 10, now=1000)[0])
        self.assertFalse(games.place_bet(self.conn, rid, 1, "nope", 0, 10, now=1000)[0])

    def test_pity_is_for_the_broke_and_once_a_day(self):
        games.account(self.conn, 1, start=5)
        self.assertEqual(games.pity(self.conn, 1, 10, 25, now=100000), 25)
        self.assertEqual(self.balance(1), 30)
        self.conn.execute("UPDATE valor SET balance = 0 WHERE user_id = '1'")
        self.assertEqual(games.pity(self.conn, 1, 10, 25, now=100000 + DAY - 1), 0)
        self.assertEqual(games.pity(self.conn, 1, 10, 25, now=100000 + DAY), 25)
        games.account(self.conn, 2)
        self.assertEqual(games.pity(self.conn, 2, 10, 25), 0)

    def test_posts_render(self):
        rid = self.round()
        games.place_bet(self.conn, rid, 1, games.TOTAL, games.OVER, 10, now=1000)
        r = games.get_round(self.conn, rid)
        self.assertIn("Who dies first", games.render_round(r, games.round_bets(self.conn, rid), True)["description"])
        self.assertIn("settled", games.render_result(games.finish_round(self.conn, rid, now=2000))["title"])
        self.assertIn("<@1>", games.render_leaderboard(games.leaderboard(self.conn))["description"])


@unittest.skipIf(discord is None, "discord.py not installed")
class WagerBotTest(unittest.TestCase):
    def make(self, d, on=True):
        import admin_bot
        bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d,
                                  "wager": {"enabled": on}}, "S")
        bot.db_path = os.path.join(d, "s.db")
        self.addCleanup(lambda: bot._db and bot._db.close())
        channel = mock.MagicMock(id=7)
        message = mock.MagicMock(id=8)
        message.edit = mock.AsyncMock()
        channel.send = mock.AsyncMock(return_value=message)
        channel.get_partial_message = mock.MagicMock(return_value=message)
        bot.client = mock.MagicMock()
        bot.client.get_channel = mock.MagicMock(return_value=channel)
        return bot, channel, message

    def press(self, bot, handler, arg, uid=42):
        it = mock.MagicMock()
        it.user.id = uid
        it.response.send_message = mock.AsyncMock()
        it.response.edit_message = mock.AsyncMock()
        asyncio.run(handler(it, arg))
        return it

    def test_a_whole_round(self):
        import games as g
        with tempfile.TemporaryDirectory() as d:
            bot, channel, message = self.make(d)
            asyncio.run(bot._wager_event("login", "Ingrid", ["Ingrid"]))
            channel.send.assert_awaited_once()
            r = g.current_round(bot.db)
            self.assertEqual(r["message_id"], "8")
            self.assertEqual(len(bot._wager_view(r).children), 4)

            asyncio.run(bot._wager_event("login", "Bjorn", ["Ingrid", "Bjorn"]))
            channel.send.assert_awaited_once()          # the round is already open

            it = self.press(bot, bot._on_wager_pick, f"{r['id']}-{g.FIRST}-0")
            labels = [c.label for c in it.response.send_message.call_args.kwargs["view"].children]
            self.assertIn("All in (100)", labels)
            it = self.press(bot, bot._on_wager_stake, f"{r['id']}-{g.FIRST}-0-10")
            self.assertIn("Bet placed", it.response.edit_message.call_args.kwargs["content"])

            asyncio.run(bot._wager_event("death", "Ingrid", ["Ingrid"]))
            self.assertIn("First blood", channel.send.call_args.args[0])

            bot._game_count = 0
            bot.wager_quiet_minutes = 0
            asyncio.run(bot._wager_tick())
            asyncio.run(bot._wager_tick())
            self.assertIsNone(g.current_round(bot.db))
            self.assertEqual(g.get_round(bot.db, r["id"])["status"], "done")

    def test_a_stale_button_is_turned_away(self):
        with tempfile.TemporaryDirectory() as d:
            bot, *_ = self.make(d)
            it = self.press(bot, bot._on_wager_pick, "99-first-0")
            self.assertIn("closed", it.response.send_message.call_args.args[0])

    def test_off_means_off(self):
        with tempfile.TemporaryDirectory() as d:
            bot, channel, _ = self.make(d, on=False)
            bot.loop, bot.ready = mock.MagicMock(), mock.MagicMock()
            with mock.patch("asyncio.run_coroutine_threadsafe") as run:
                bot.notify_game("login", "Ingrid", ["Ingrid"])
            run.assert_not_called()
            asyncio.run(bot._wager_tick())
            channel.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
