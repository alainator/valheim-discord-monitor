import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402


def lines(*msgs):
    return [f"2026-09-28 15:{i:02d}:00 {m}" for i, m in enumerate(msgs)]


class UpdateLogTest(unittest.TestCase):
    def run_lines(self, w, *msgs):
        return [out for ln in lines(*msgs) for out in w.handle(ln)]

    def test_update_waits_then_installs(self):
        w = updater.UpdateWatcher({})
        out = self.run_lines(
            w, "Local build: 1 | Remote build: 2", "Update detected (1 -> 2).",
            "Update available but 2 player(s) connected. Deferring restart.",
            "Local build: 1 | Remote build: 2", "Update detected (1 -> 2).",
            "Update available but 1 player(s) connected. Deferring restart.",   # same build: no repeat
            "Local build: 1 | Remote build: 2", "Update detected (1 -> 2).",
            "No players connected. Restarting to apply update.", "Restart issued.")
        self.assertEqual([t for t, _ in out], ["public", "public"])
        self.assertIn("build 1 → 2", out[0][1])
        self.assertIn("2 players online now", out[0][1])
        self.assertIn("Installing the Valheim update (build 1 → 2)", out[1][1])

    def test_quiet_checks_post_nothing_but_a_requested_check_answers(self):
        w = updater.UpdateWatcher({})
        self.assertEqual(self.run_lines(w, "Local build: 7 | Remote build: 7", "No update available."), [])
        out = self.run_lines(w, "Update check requested from Discord.", "Local build: 7 | Remote build: 7",
                             "No update available.")
        self.assertEqual(out, [("admin", "✅ Update check: no update available (build 7).")])

    def test_errors_go_to_admins_once(self):
        w = updater.UpdateWatcher({}, clock=lambda: 1000)
        msg = "ERROR: could not determine remote build id after 3 attempts, aborting"
        self.assertEqual(len(self.run_lines(w, msg, msg, "WARN: steamcmd query attempt 1 failed")), 1)

    def test_request_and_status_files(self):
        with tempfile.TemporaryDirectory() as d:
            w = updater.UpdateWatcher({"bot_dir": d}, clock=lambda: 1234)
            self.assertIsNone(w.request("check"))
            self.assertIn("hasn't been picked up", w.request("restart"))
            with open(os.path.join(d, "request")) as f:
                self.assertEqual(f.read(), "check\n")
            self.assertIn("unknown request", w.request("rm -rf /"))
            w.write_status(3, False)
            with open(os.path.join(d, "status.json")) as f:
                self.assertEqual(json.load(f), {"count": 3, "down": False, "updated_at": 1234})
            w.write_status(3, True)                      # down: count must not look like "empty"
            with open(os.path.join(d, "status.json")) as f:
                self.assertIsNone(json.load(f)["count"])
        self.assertIn("isn't mounted", updater.UpdateWatcher({"bot_dir": "/nonexistent"}).request("check"))


class CountdownTest(unittest.TestCase):
    def test_warns_then_restarts_early_when_everyone_leaves(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            bot = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d},
                                     "Alheim")
            posts, online = [], [2]
            w = updater.UpdateWatcher({"bot_dir": d})
            live = mock.Mock()
            live.snapshot = lambda: {"count": online[0]}
            bot.attach(live=live, updater=w, announce=posts.append)
            clock = [0.0]

            async def run():
                loop = asyncio.get_running_loop()
                real_sleep = asyncio.sleep

                async def fake_sleep(n):
                    clock[0] += n
                    if clock[0] >= 250:
                        online[0] = 0                    # everyone logs out after ~4 min
                    await real_sleep(0)
                with mock.patch.object(loop, "time", lambda: clock[0]), \
                     mock.patch.object(admin_bot.asyncio, "sleep", fake_sleep), \
                     mock.patch.object(bot, "post_admin"):
                    bot._countdown_cancel = asyncio.Event()
                    await bot._restart_countdown(10, "installing the update", "admin")
            asyncio.run(run())
            self.assertIn("restarts in **10 minutes** (installing the update)", posts[0])
            self.assertIn("restarting now", posts[-1])
            self.assertEqual(len(posts), 2)              # restarted early: no 5/1-minute warnings
            with open(os.path.join(d, "request")) as f:
                self.assertEqual(f.read(), "restart\n")


if __name__ == "__main__":
    unittest.main()
