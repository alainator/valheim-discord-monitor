# Host side: auto-updates and restarts from Discord

For a **self-hosted** server where a cron job runs `check_update.sh` (compare the
installed build with Steam's, restart when nobody is on; the systemd unit's
`ExecStartPre` runs `steamcmd app_update`, so every restart installs the update).

The monitor runs in Docker and never gets host privileges. It works with the host
through one shared folder, `/home/valheim/bot/`:

| File | Written by | Read by | Purpose |
|---|---|---|---|
| `status.json` | monitor, every poll | `check_update.sh` | The live player count, so the updater never restarts on people |
| `request` | monitor (`/valheim restart`, `/valheim update-check`) | `valheim-bot-request.sh` | Ask the host to check now or restart |

The monitor also reads `update_check.log` to post "update available", "installing" and
checker errors, and it posts "✅ Valheim updated: l-1.0.16 → l-1.0.17" when the server
comes back on a new version.

## 1. Shared folder

```bash
sudo -u valheim mkdir -p /home/valheim/bot
```

## 2. Make `check_update.sh` use the monitor's player count

In `/home/valheim/check_update.sh`, replace the player-count lines (from
`LAST_COUNT_LINE=` down to `PLAYER_COUNT=${PLAYER_COUNT:-0}`) with this block:

```bash
# --- Player count: the Discord monitor's live count when it's fresh (< 2 min old),
# --- else the last "now N player(s)" line in the console log.
CONSOLE_LOG="/home/valheim/logs/valheim_console.log"
STATUS_FILE="/home/valheim/bot/status.json"
PLAYER_COUNT=""
if [ -f "$STATUS_FILE" ] && [ $(( $(date +%s) - $(stat -c %Y "$STATUS_FILE") )) -lt 120 ]; then
    PLAYER_COUNT=$(grep -oE '"count": *[0-9]+' "$STATUS_FILE" | grep -oE '[0-9]+$' || true)
fi
if [ -z "$PLAYER_COUNT" ]; then
    LAST_COUNT_LINE=$(tail -n 2000 "$CONSOLE_LOG" 2>/dev/null | grep -oE 'now [0-9]+ player\(s\)' | tail -n1 || true)
    PLAYER_COUNT=$(echo "$LAST_COUNT_LINE" | grep -oE '[0-9]+' || true)
fi
PLAYER_COUNT=${PLAYER_COUNT:-0}
```

Edit it as the `valheim` user (`sudo -u valheim nano /home/valheim/check_update.sh`).

- **Why:** the console log is rotated daily with `copytruncate`. Right after a rotation
  there's no "now N player(s)" line to find, so the old check read 0 and could restart on
  people who were playing. The monitor's count survives rotations.
- **Fallback:** if the monitor is stopped, `status.json` goes stale and the old check is used.
- **Unknown count:** while the monitor doesn't know the count yet (just after it starts),
  `count` is `null` in `status.json` and the old check is used.
- **`|| true`:** the script runs with `set -euo pipefail`, so a search that finds nothing
  would otherwise stop the script before it can restart.

Check it:
```bash
sudo -u valheim bash /home/valheim/check_update.sh; sudo tail -3 /home/valheim/update_check.log
```

## 3. The request handler

```bash
sudo install -o valheim -g valheim -m 755 host/valheim-bot-request.sh /home/valheim/valheim-bot-request.sh
sudo install -m 644 host/valheim-bot-request.path host/valheim-bot-request.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now valheim-bot-request.path
systemctl status valheim-bot-request.path        # "active (waiting)"
```

The handler runs **as `valheim`** and can only do two things:
- **`check`:** run `check_update.sh` now.
- **`restart`:** `sudo /bin/systemctl restart valheimserver.service`. That is the one
  command valheim's existing sudoers rule allows, so no new permissions are needed.

It logs each request to `update_check.log` (`Update check requested from Discord.`,
`Restart requested from Discord.`).

Test it without Discord:
```bash
echo check | sudo -u valheim tee /home/valheim/bot/request
sleep 5; sudo tail -3 /home/valheim/update_check.log
```

## 4. Monitor configuration

Add the two updater volumes to `docker-compose.override.yml`, next to
`docker-compose.yml`. Keep your own settings there so `git pull` never conflicts:
```yaml
services:
  valheim-discord-monitor:
    volumes:
      - /home/valheim:/valheim_home:ro        # update_check.log + world-settings.env (read-only)
      - /home/valheim/bot:/bot                # status.json + requests
```
The whole home folder is mounted (read-only) rather than the log file alone because
logrotate replaces `update_check.log` weekly, which a single-file mount wouldn't follow.

`config.json`:
```json
"updater": { "log": "/valheim_home/update_check.log", "bot_dir": "/bot" }
```
Add `"update"` to `events` for the public update posts. Then:
`docker compose up -d --force-recreate`.

## Discord commands (admins)

- **`/valheim update-check`** runs the check now. The result goes to the admin channel.
- **`/valheim restart [minutes] [reason]`** restarts the server, which installs any waiting
  update.
  - With people online it warns the public channel ("restarts in 5 minutes"), again at
    5 and 1 minutes, then restarts.
  - It restarts early if everyone leaves.
  - With nobody online it restarts immediately.
- **`/valheim restart-cancel`** stops a countdown.

If a request isn't picked up within 30 seconds, the admin channel is told to check
`systemctl status valheim-bot-request.path`.

Keep the cron job: it still does the checking every 15 minutes. The commands just add
"now" and "with a warning".

## World settings from Discord (preset, modifiers, setkeys)

The same request handler can change the world's **preset**, **modifiers** and **setkeys**.
Admins use `/valheim settings`, `/valheim preset`, `/valheim modifier` and
`/valheim setkey`.

**How it stays safe:**
- The service file (root-owned) is edited once, by you, to read the settings from a small
  file owned by `valheim`: `/home/valheim/world-settings.env`.
- The bot never writes that file. It sends a request, and `valheim-world-settings.py`
  (running as `valheim`) writes the file after checking every value against Valheim's
  known presets, modifiers and setkeys.
- Anything else is refused, so a request can't add other launch options. Server name,
  world, port, save folder and log path stay manual edits.
- Changes take effect at the next restart. The bot offers a "Restart in 5 min" button.

### One-time setup

Run these from the repo folder, after `git pull`.

1. **Install the updated handler and the settings writer:**
   ```bash
   sudo install -o valheim -g valheim -m 755 host/valheim-bot-request.sh /home/valheim/valheim-bot-request.sh
   sudo install -o valheim -g valheim -m 755 world_settings.py /home/valheim/valheim-world-settings.py
   ```
2. **Move your current modifiers into the settings file.** Copy every `-preset …`,
   `-modifier … …` and `-setkey …` from your `ExecStart` line, for example:
   ```bash
   systemctl cat valheimserver | grep ExecStart          # see what you have now
   echo 'WORLD_ARGS=-modifier raids less' | sudo -u valheim tee /home/valheim/world-settings.env
   ```
3. **Point the service at it:**
   ```bash
   sudo systemctl edit --full valheimserver.service
   ```
   - In `[Service]`, add:
     ```ini
     EnvironmentFile=-/home/valheim/world-settings.env
     ```
   - In `ExecStart`, replace the modifiers you moved (e.g. `-modifier raids less`) with
     `$WORLD_ARGS`, written without quotes so each option becomes its own argument:
     ```ini
     ExecStart=/home/valheim/valheimserver/valheim_server.x86_64 ... -crossplay $WORLD_ARGS -logFile /home/valheim/logs/valheim_console.log
     ```
   - Save. `systemctl edit` reloads systemd for you.
4. **Check it** at a time nobody's playing:
   ```bash
   sudo -u valheim python3 /home/valheim/valheim-world-settings.py --file /home/valheim/world-settings.env show
   sudo systemctl restart valheimserver
   pgrep -af valheim_server.x86_64 | tr ' ' '\n' | grep -A2 -E '^-(preset|modifier|setkey)$'
   ```
   The last command should list the same modifiers as before.

The monitor reads the settings file through the read-only `/valheim_home` mount you
already set up for the updater. `/valheim settings` shows what's in it.

### Using it

| Command | Example |
|---|---|
| `/valheim settings` | Shows the current preset, modifiers and setkeys, and every allowed value |
| `/valheim modifier <name> <value>` | `raids` → `more`. Set `normal` to go back to the default |
| `/valheim preset <name>` | `hard`. `default` removes the preset |
| `/valheim setkey <key> on\|off` | `passivemobs` → `on` |

After a change the bot confirms once the file has been written, e.g. "✅ Saved: modifier
raids → more. It takes effect at the next server restart", and offers a **Restart in 5 min
(with warning)** button. `update_check.log` records every change
(`World setting from Discord (…): WORLD_ARGS=…`).
