import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host", "valheim-bot-request.sh")

try:
    import discord
except ImportError:                      # the bot is optional; CI installs discord.py
    discord = None


class RestoreRequestTest(unittest.TestCase):
    def test_backup_id(self):
        self.assertEqual(updater.backup_id("Alheim_backup_auto-20260928-170645"), "20260928170645")
        self.assertEqual(updater.backup_id("Alheim_backup_prerestore-20260930-231500"), "20260930231500")
        self.assertEqual(updater.backup_id("Alheim_backup_20260928170645"), "20260928170645")
        self.assertIsNone(updater.backup_id("Alheim"))
        self.assertIsNone(updater.backup_id("Alheim_backup_old"))

    def test_only_well_formed_requests(self):
        with tempfile.TemporaryDirectory() as d:
            w = updater.UpdateWatcher({"bot_dir": d})
            self.assertIn("unknown", w.request("restore ../../etc"))
            self.assertIn("unknown", w.request("restore 123"))
            self.assertIsNone(w.request("restore 20260928170645"))
            with open(os.path.join(d, "request")) as f:
                self.assertEqual(f.read(), "restore 20260928170645\n")

    def test_done_line_is_posted(self):
        w = updater.UpdateWatcher({})
        out = w.handle("2026-09-30 23:15:00 Restore done: Alheim_backup_auto-20260928-170645 "
                       "(the world before it is saved as Alheim_backup_prerestore-20260930-231500)")
        self.assertEqual([t for t, _ in out], ["admin", "public"])
        self.assertIn("Alheim_backup_prerestore-20260930-231500", out[0][1])
        err = w.handle("2026-09-30 23:15:00 ERROR: restore: the server didn't stop; nothing changed")
        self.assertEqual(err, [("admin", "⚠️ The update checker failed: restore: the server didn't stop; "
                                         "nothing changed")])


@unittest.skipIf(shutil.which("bash") is None, "needs bash")
class HostScriptTest(unittest.TestCase):
    """Runs host/valheim-bot-request.sh against a fake worlds_local, with stand-ins for
    sudo and systemctl."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.worlds = os.path.join(self.d, "worlds_local")
        self.bot = os.path.join(self.d, "bot")
        self.bin = os.path.join(self.d, "bin")
        for p in (self.worlds, self.bot, self.bin):
            os.makedirs(p)
        self.calls = os.path.join(self.d, "calls")
        for name, body in (("sudo", f'echo "sudo $*" >> {self.calls}'),
                           ("systemctl", "exit 3")):          # is-active: not running
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/bash\n" + body + "\n")
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def world(self, name, text):
        os.makedirs(os.path.join(self.worlds, name))
        with open(os.path.join(self.worlds, name, "_main.1.fwl2"), "w") as f:
            f.write(text)

    def run_request(self, line):
        with open(os.path.join(self.bot, "request"), "w") as f:
            f.write(line + "\n")
        env = {**os.environ, "PATH": self.bin + ":" + os.environ["PATH"], "VDM_BOT_DIR": self.bot,
               "VDM_LOG_FILE": os.path.join(self.d, "log"), "VDM_WORLDS": self.worlds}
        subprocess.run(["bash", SCRIPT], env=env, check=True, timeout=30)
        with open(os.path.join(self.d, "log")) as f:
            return f.read()

    def read(self, name):
        with open(os.path.join(self.worlds, name, "_main.1.fwl2")) as f:
            return f.read()

    def test_restore_swaps_in_the_backup_and_keeps_the_old_world(self):
        self.world("Alheim", "now")
        self.world("Alheim_backup_auto-20260928-170645", "then")
        self.world("Alheim_backup_auto-20260929-170645", "later")
        log = self.run_request("restore 20260928170645")
        self.assertIn("Restore done: Alheim_backup_auto-20260928-170645", log)
        self.assertEqual(self.read("Alheim"), "then")
        kept = [n for n in os.listdir(self.worlds) if "prerestore" in n]
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.read(kept[0]), "now")
        self.assertEqual(self.read("Alheim_backup_auto-20260928-170645"), "then")    # the backup stays
        with open(self.calls) as f:
            self.assertEqual(f.read().split("\n")[:2], ["sudo /bin/systemctl stop valheimserver.service",
                                                         "sudo /bin/systemctl start valheimserver.service"])
        self.assertFalse(os.path.exists(os.path.join(self.bot, "request")))

    def test_legacy_db_fwl_pair(self):
        for stem, text in (("Alheim", "now"), ("Alheim_backup_20260928170645", "then")):
            for ext in ("db", "fwl"):
                with open(os.path.join(self.worlds, f"{stem}.{ext}"), "w") as f:
                    f.write(text)
        log = self.run_request("restore 20260928170645")
        self.assertIn("Restore done", log)
        with open(os.path.join(self.worlds, "Alheim.db")) as f:
            self.assertEqual(f.read(), "then")
        self.assertTrue(any(n.startswith("Alheim_backup_prerestore-") and n.endswith(".fwl")
                            for n in os.listdir(self.worlds)))

    def test_unknown_backup_changes_nothing(self):
        self.world("Alheim", "now")
        log = self.run_request("restore 20990101000000")
        self.assertIn("ERROR: restore: no single backup", log)
        self.assertEqual(self.read("Alheim"), "now")
        self.assertFalse(os.path.exists(self.calls))                 # the server wasn't touched

    def test_bad_characters_are_dropped(self):
        self.world("Alheim", "now")
        log = self.run_request("restore ../../etc/passwd")
        self.assertIn("ERROR: restore: bad backup id", log)

    def test_failed_swap_puts_the_old_world_back(self):
        """The copy worked and the live world was moved aside, but moving the copy in
        failed: the old world goes back, so the server doesn't start on a missing world."""
        self.world("Alheim", "now")
        self.world("Alheim_backup_auto-20260928-170645", "then")
        with open(os.path.join(self.bin, "mv"), "w") as f:      # mv fails for the copy only
            f.write('#!/bin/bash\ncase "$1" in *.restoring) exit 1 ;; esac\nexec /bin/mv "$@"\n')
        os.chmod(os.path.join(self.bin, "mv"), 0o755)
        log = self.run_request("restore 20260928170645")
        self.assertIn("ERROR: restore: couldn't copy", log)
        self.assertEqual(self.read("Alheim"), "now")
        self.assertFalse(any("prerestore" in n or n.endswith(".restoring") for n in os.listdir(self.worlds)))
        with open(self.calls) as f:
            self.assertIn("start valheimserver.service", f.read())

    def test_server_that_wont_stop_is_left_alone(self):
        self.world("Alheim", "now")
        self.world("Alheim_backup_auto-20260928-170645", "then")
        with open(os.path.join(self.bin, "sudo"), "w") as f:
            f.write("#!/bin/bash\nexit 1\n")                            # sudoers doesn't allow stop
        log = self.run_request("restore 20260928170645")
        self.assertIn("couldn't stop the server", log)
        self.assertEqual(self.read("Alheim"), "now")


@unittest.skipIf(discord is None, "discord.py not installed")
class RestorePointsTest(unittest.TestCase):
    def test_lists_valheims_own_backups_with_ids(self):
        import admin_bot
        with tempfile.TemporaryDirectory() as d:
            worlds = os.path.join(d, "worlds_local")
            for name in ("Alheim", "Alheim_backup_auto-20260928-170645", "Alheim_backup_auto-20260929-170645"):
                os.makedirs(os.path.join(worlds, name))
            os.utime(os.path.join(worlds, "Alheim_backup_auto-20260928-170645"), (1000, 1000))
            b = admin_bot.AdminBot({"token": "x", "channel_id": "1", "admin_user_ids": [5], "save_dir": d}, "S")
            points = b.restore_points()
        self.assertEqual([(p[2], p[3]) for p in points], [
            ("Alheim_backup_auto-20260929-170645", "20260929170645"),
            ("Alheim_backup_auto-20260928-170645", "20260928170645")])


if __name__ == "__main__":
    unittest.main()
