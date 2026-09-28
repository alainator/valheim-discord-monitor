# Deploying the public stats page

The monitor records every login, logout and death into a small SQLite database and
regenerates a self-contained `index.html` on an interval. You serve that one file
from any web server, e.g. at `https://valheim.example.com`.

Nothing but Python's standard library is needed for the database and the page
(`sqlite3` is built in). No database server, no framework.

## 1. Configure

In `config.json`, alongside the existing `source`/`discord` blocks:

```json
"database": { "path": "valheim_stats.db", "enabled": true },
"stats_site": {
  "output": "site/index.html",
  "render_interval_seconds": 60,
  "refresh_seconds": 120,
  "top_n": 10,
  "timezone_label": "server time"
}
```

- **`database.path`:** the .db file, relative to the monitor's working directory or absolute.
- **`stats_site.output`:** where to write the page.
  - **With Docker Compose**, keep it inside the repo folder, which is mounted at `/app`:
    `site/index.html` ends up in `<repo>/site/index.html` on the host. `site/` is git-ignored.
    Serve that folder.
  - **Without Docker**, it can be any path the monitor's user can write, e.g.
    `/var/www/valheimstats/index.html`:
    ```bash
    sudo mkdir -p /var/www/valheimstats
    sudo chown "$USER": /var/www/valheimstats      # the user the monitor runs as
    ```
- **`render_interval_seconds`:** how often the running monitor rewrites the page, and only
  when something changed.
- **`refresh_seconds`:** how often browsers auto-refresh.

## 2. (Optional) Backfill history

Seed the database from a log you already have. No Discord posts are sent:

```bash
# Docker Compose
docker compose exec valheim-discord-monitor \
  python valheim_discord_monitor.py --config config.json --backfill /logs/valheim_console.log

# Without Docker
python3 valheim_discord_monitor.py --config config.json --backfill /path/to/valheim_console.log
```

A self-hosted server started with `-logFile` rewrites its log on every restart, so this
only covers the current server session. On LOW.MS, download the log from the panel's
Console tab or Files tab.

Backfilling the same log twice inserts its sessions twice, so do one clean backfill.
Then restart the monitor.

## 3. Restart the monitor

```bash
docker compose up -d --force-recreate && docker compose logs -f     # Docker Compose
sudo systemctl restart valheim-monitor                              # systemd
```

You should see `... recording stats` in the startup line, then
`Rendered stats page -> site/index.html` shortly after. Confirm the file exists:

```bash
ls -l site/index.html
```

## 4. DNS

Add an A record for the subdomain pointing at the public IP of the machine that serves
the page:

```
valheim.example.com.   A   <your server IP>
```

## 5. Web server

Point `root` at the folder holding `index.html`: `<repo>/site` for Docker Compose, or
`/var/www/valheimstats`.

### Caddy (automatic HTTPS)
```
valheim.example.com {
    root * /path/to/valheim-discord-monitor/site
    file_server
}
```

### nginx
```nginx
server {
    listen 80;
    server_name valheim.example.com;
    root /path/to/valheim-discord-monitor/site;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```
Then TLS with certbot:
```bash
sudo certbot --nginx -d valheim.example.com
```

The web server's user needs read access to that folder, and to each folder above it.

## 6. Belt-and-suspenders regeneration (optional)

The monitor already regenerates the page. To keep the page refreshing while the monitor
is stopped, add a cron entry that renders straight from the database:

```cron
*/2 * * * * cd /path/to/valheim-discord-monitor && /usr/bin/python3 stats_site.py --db valheim_stats.db --out site/index.html --config config.json
```

## Notes

- **Safe to publish:** the page is static HTML with no inputs, so it can be public with no
  authentication. Player names are HTML-escaped.
- **Times** are the server's own clock (the log's timestamps). Set
  `stats_site.timezone_label` to whatever you want printed in the footer.
- **Backups:** the database is a single file; copy it to back up all history.
  - WAL mode is on, so copy `*.db-wal` / `*.db-shm` too if the monitor is running, or stop it first.
  - With Docker Compose the database is in the repo folder next to `config.json`.
- **Players already online when the monitor (re)starts** have no login row, so that
  in-progress session isn't counted until their next visit. Everything after the start is exact.
- **One-off commands are safe to run next to the live monitor:** `--refresh-steam`,
  `--render-site`, `steam.py` and `stats_site.py` don't close the sessions of players who
  are online.
