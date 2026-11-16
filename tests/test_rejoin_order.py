"""Runs the real monitor against a log file and a local fake webhook: held leaves must go out
in order when a backlog is read in one poll (a monitor restart with players on)."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RejoinOrderTest(unittest.TestCase):
    def setUp(self):
        self.posts = []
        posts = self.posts

        class Hook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append(body.get("content") or "")
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.srv.shutdown()
        self.dir.cleanup()

    def test_a_backlog_keeps_leaves_in_order(self):
        work, t0 = self.dir.name, time.time() - 3600
        log = os.path.join(work, "console.log")

        def at(off):
            return time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime(t0 + off))

        def join(owner, off):
            return [f"{at(off)}: PlayFab listen socket child connected to remote player AAAA1111BBBB2222",
                    f'{at(off)}: Player joined server "T" that has join code 111111, now 1 player(s)',
                    f"{at(off)}: Got character ZDOID from Bjorn : {owner}:1"]

        def leave(owner, off):
            return [f"{at(off)}: Destroying abandoned non persistent zdo {owner}:1 owner {owner}",
                    f'{at(off)}: Player connection lost server "T" that has join code 111111, now 0 player(s)']

        with open(log, "w") as f:
            f.write(f"{at(0)}: DungeonDB Start\n{at(0)}: Game server connected\n")
        cfg = {"server_name": "T", "poll_interval_seconds": 0.5, "state_file": os.path.join(work, "state.json"),
               "events": ["login", "logout", "session_summary", "server_online"],
               "log_archive": {"enabled": False},
               "discord": {"webhook_url": f"http://127.0.0.1:{self.srv.server_port}/h", "embeds": False,
                           "rejoin_grace_seconds": 180},
               "source": {"type": "file", "path": log}}
        with open(os.path.join(work, "config.json"), "w") as f:
            json.dump(cfg, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DISCORD", "LOWMS", "STEAM"))}
        p = subprocess.Popen([sys.executable, os.path.join(REPO, "valheim_discord_monitor.py"),
                              "--config", "config.json", "--from-start"],
                             cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            while not self.posts and time.time() < deadline:      # "back online": it's tailing
                time.sleep(0.2)
            self.posts.clear()
            # Leaves after 29 min, back 4 min later (too late to be a rejoin), drops again
            # after 10 min and is back 20 s later (a rejoin). All in one poll.
            lines = (join(111, 60) + leave(111, 1800) + join(222, 2040) + leave(222, 2640) + join(333, 2660))
            with open(log, "a") as f:
                f.write("\n".join(lines) + "\n")
            while len(self.posts) < 3 and time.time() < deadline:
                time.sleep(0.2)
            time.sleep(1.5)                                        # anything extra would show up
        finally:
            p.terminate()
            p.wait(timeout=10)
        self.assertEqual(len(self.posts), 3, self.posts)
        self.assertIn("Bjorn** has arrived", self.posts[0])
        self.assertIn("Bjorn** left T after 29m", self.posts[1])
        self.assertIn("Bjorn** has arrived", self.posts[2])


if __name__ == "__main__":
    unittest.main()
