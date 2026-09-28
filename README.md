# Valheim → Discord monitor (no mods)

Posts Valheim server activity to a Discord channel without installing anything
on the game server, so Steam achievements keep working. Two modes:

| Mode | Needs | Events |
|---|---|---|
| **Count mode** (`a2s` or `steamapi`) | Only the server's IP — polls the Steam query port (game port + 1), or Steam's master server via the Web API when that port is firewalled | player joined / left (count only), server online / offline |
| **Log mode** (`lowms`, `nexus`, `ftp`, `sftp`, `file`, `http`) | Read access to `valheim_console.log` — on LOW.MS via the public API (`lowms`) | named **login**, **logout**, **death**, respawn |

Log mode is the one you want: names and deaths. On LOW.MS use the `lowms`
source: the public API now serves the console with a plain API key (no FTP/SFTP
exists there, and the older `nexus` source had to sign in to the panel as you). Count mode is the fallback for hosts where nothing else
works — note the Steam-based sources report a stale count on crossplay
servers, because relayed players never register with Steam.

Python 3.9+, no third-party packages for the core monitor. Optional extras: SFTP
(`paramiko`) and the Discord admin bot (`discord.py`, see `requirements.txt`).

## Count mode (quick start)

1. Check the query port answers from wherever the monitor will run:
   ```bash
   python a2s_probe.py YOUR.SERVER.IP          # prints "name — 2/10 players (v0.220.5 …)"
   ```
   Valheim's query port is the game port + 1 (2456 → 2457). **No reply?** The
   host is firewalling UDP queries (LOW.MS does). Use the `steamapi` source
   instead — see below.
2. Create a Discord webhook (channel → Edit Channel → Integrations → Webhooks).
3. `cp config.example.json config.json`, fill in `host` and `webhook_url`.
4. `python valheim_discord_monitor.py --test-webhook`, then
   `python valheim_discord_monitor.py`.

The monitor polls every `poll_interval_seconds` (15 s default), posts when the
count changes, and marks the server offline after `offline_after` consecutive
failed queries (3 default) so a single dropped packet doesn't cause a false
alarm. Nothing is posted on start-up. Messages use `{who}` ("A viking" / "2
vikings"), `{count}`, `{max}` and `{server}` placeholders.

### `steamapi` — when the query port is firewalled

The game server sends its player count to Steam's master server itself
(outbound heartbeats), so Steam can tell you the count even when nothing
inbound reaches the server. Two requirements:

1. The server is set **Public** in your host's panel (on LOW.MS: Manage →
   General → Public Server). This only lists it in the community browser; the
   password still applies.
2. A free Steam Web API key from <https://steamcommunity.com/dev/apikey>
   (any domain name will do).

Config:
```json
"source": { "type": "steamapi", "host": "YOUR.SERVER.IP", "game_port": 2456, "api_key": "..." }
```
or leave `api_key` out and set the `STEAM_API_KEY` environment variable.
Check it with `python valheim_discord_monitor.py --probe`. Steam refreshes
its listing on the server's heartbeat, so counts can lag a minute or so.

Limitations: A2S carries no names (Valheim returns an empty player list) and no
death information. **Crossplay servers:** both `a2s` and `steamapi` see only
players who connected directly through Steam, so with crossplay on the count
stays frozen — use the `nexus` source instead. Two players swapping within one poll interval shows as no
change.

## Log mode

Vanilla Valheim already prints everything needed to `valheim_console.log`
(on LOW.MS: **Files → game → valheim_console.log**). From your server:

| Event   | Log line |
|---------|----------|
| Login   | `Got character ZDOID from Bjorn : 1234567890:67` (first non-zero ZDOID for a name) |
| Death   | `Got character ZDOID from Bjorn : 0:0` |
| Respawn | next non-zero ZDOID for that name |
| Logout  | `Destroying abandoned non persistent zdo … owner 1234567890` (owner id matches the player), or `Closing socket …` on direct-Steam servers, or `Player disconnected … now 0 player(s)` |

The monitor tails the log (by byte offset, so restarts never re-post), runs the
lines through a small state machine, and posts an embed to a Discord webhook.

### Setup

1. **Discord webhook** — in your Discord server: channel → Edit Channel →
   Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. Copy `config.example.json` to `config.json`, paste the webhook URL and set
   `server_name`.
3. Pick a log source (below) and fill in the `source` block.
4. Test:
   ```bash
   python valheim_discord_monitor.py --test-webhook        # posts a hello to Discord
   python valheim_discord_monitor.py --replay sample_console.log   # parser dry-run, prints events
   ```
5. Run it:
   ```bash
   python valheim_discord_monitor.py --config config.json
   ```
   Secrets can be given as environment variables instead of in the file:
   `DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `VALHEIM_LOG_USER`, `VALHEIM_LOG_PASSWORD`, `NEXUS_TOKEN`.

By default the monitor starts at the **end** of the log (only new events post).
Use `--from-start` once if you want it to replay the existing file.

### Log sources

#### `ftp`
For hosts that offer FTP. Set `host`, and `path` to the console log (on
LOW.MS it is `game/valheim_console.log`; the layout may put it under a
`<ip>_<port>/` directory — run discovery to find out):

```bash
python valheim_discord_monitor.py --config config.json --discover
```
This walks the FTP tree and prints every `*.log` / `*console*` file with its size.
Set `"tls": true` if the host requires FTPS.

LOW.MS's Nexus panel does not currently offer FTP/SFTP (confirmed with their
support, Sept 2026) — use count mode there.

#### `lowms` (LOW.MS public API — recommended on LOW.MS)
The documented [LOW.MS public API](https://api.prod.nexus.low.ms/v1/docs) reads the
console directly:

```json
"source": { "type": "lowms", "server_id": "YOUR-SERVER-UUID", "lines": 300 },
"events": ["login", "logout", "death", "server_restart", "server_online", "server_offline"]
```

Create a key under Panel → Account → **API Keys** with the `console:read` scope
(pin it to this server), and put it in `LOWMS_API_KEY` or `source.api_key`. Same log
lines as the `nexus` source below — up to 500 per call instead of 300 — but a stable,
documented endpoint with no browser sign-in, so it can't break when the panel's login
page changes or the account gets an MFA or CAPTCHA challenge. **Prefer this.**

The one thing it can't do is install game updates (the public API has no update
endpoint), so the `maintenance` block's update half needs the `nexus` login;
backups work fine with the key alone.

#### `nexus` (LOW.MS panel — legacy; use `lowms` instead)
The panel's Console tab reads the live log from
`GET https://api.prod.nexus.low.ms/user/servers/<id>/daemon/console?lines=N`
using the panel's own Auth0 session token (the public `lowms_…` API keys are
rejected there). *Note:* LOW.MS's public API has since added
`GET /v1/servers/{id}/console` (scope `console:read`), which could replace this
browser sign-in for reading the log; the panel session is still needed for game
updates, which the public API doesn't offer. The `nexus`
source signs in to the panel **with your own account** through a headless
browser, captures that token, caches it, and signs in again whenever it
expires or is rejected — so you get the full log, and with it named login /
logout / death events, on a host that offers no file access.

One-time setup on the machine that runs the monitor:
```bash
pip install playwright
python3 -m playwright install --with-deps chromium     # needs sudo for the system deps
```
Config:
```json
"source": {
  "type": "nexus",
  "server_id": "YOUR-SERVER-UUID",
  "lines": 300,
  "login": { "email": "you@example.com", "password": "…" }
},
"events": ["login", "logout", "death"]
```
Or keep the credentials out of the file with `NEXUS_EMAIL` / `NEXUS_PASSWORD`.
The server id is the UUID in the panel URL. Check it works before starting
the monitor:
```bash
NEXUS_EMAIL=… NEXUS_PASSWORD=… python3 nexus_login.py --server-id YOUR-SERVER-UUID --check
```
That signs in, prints the token expiry, and echoes three console lines. The
token and browser profile live in `~/.valheim-monitor/`; on a login failure a
screenshot and page HTML are dropped there for diagnosis. Two caveats: enabling
MFA on the LOW.MS account will break the automatic sign-in, and this relies on
an internal panel endpoint LOW.MS could change.

#### `file`
For running the monitor on the same machine as the server, or on any log
file you sync locally.

#### `sftp` / `http`
SFTP needs `pip install paramiko`. `http` polls any URL that returns the raw
log text (supports `Range` requests if the server does).

## Join-attempt alerts & Discord admin bot

If the server uses `bannedlist.txt` or `permittedlist.txt`, Valheim turns away anyone
banned or not permitted and logs:

```
Player Stranger : Steam_76561198000000000 is blacklisted or not in whitelist.
```

Log mode turns that line into a **`join_refused`** event. Nothing is logged when
neither list is in use (nobody gets refused), so you only hear about it when the
lists are doing their job. There are two ways to receive it:

- **Webhook only.** Add `"join_refused"` to `events` and a line like "**Stranger**
  tried to join My Server but isn't allowed in." goes to your normal channel. The
  platform ID isn't included, because that channel is usually public.
- **Admin bot (recommended).** A small Discord bot posts each refusal to a
  **private admin channel** with the player's name, platform ID, Steam profile
  link and why they were refused, plus three buttons:

  | Button | Does |
  |---|---|
  | **Permit** | Removes the ID from `bannedlist.txt` and, if you use a permitted list, adds it to `permittedlist.txt` |
  | **Ban** | Adds the ID to `bannedlist.txt` and removes it from `permittedlist.txt` |
  | **Ignore** | Closes the notice |

  Only the Discord users and roles you list can press them. There are also slash
  commands for IDs you already know: `/valheim permit`, `/valheim ban`,
  `/valheim unban`, `/valheim unpermit`, `/valheim lists`.

  **Permit** never *starts* a permitted list. With an empty `permittedlist.txt`
  the server is open to everyone who isn't banned, and adding the first ID would lock
  everyone else out, so in that case Permit only unbans.

The bot edits the list files directly, so it has to run **on the same machine as
the server** (self-hosted, `file` source) with write access to the save dir. It
writes the ID exactly as the server printed it (`Steam_…` / `Xbox_…` on current
builds, a bare SteamID64 on older ones), and matches `Steam_7656…` and bare
`7656…` as the same player. Community docs say list edits apply without a restart
(the next join attempt is checked against the file). If a change doesn't seem to take
effect, `sudo systemctl restart valheimserver`.

### Setting up the bot

1. <https://discord.com/developers/applications> → **New Application** → **Bot**
   → **Reset Token** and copy it (goes in `DISCORD_BOT_TOKEN` or `admin_bot.token`).
   It needs **no privileged intents**.
2. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`, bot
   permissions **View Channel**, **Send Messages**, **Embed Links**. Open the URL and add
   the bot to your Discord server.
3. Create a private channel (e.g. `#valheim-admin`) that the bot can see.
4. Turn on Discord's **Developer Mode** (User Settings → Advanced), then
   right-click to **Copy ID** for: your server (`guild_id`), the channel
   (`channel_id`), and yourself (`admin_user_ids`), or an admin role (`admin_role_ids`).
5. Fill in the `admin_bot` block (see `config.selfhosted.example.json`) with
   `save_dir` pointing at the folder with the list files (inside Docker:
   `/valheim_save_data`, per `docker-compose.yml`).
6. `pip install -r requirements.txt` (the Docker image already has it) and restart
   the monitor. The log shows `admin_bot: connected as …`. Setting `guild_id` makes the
   slash commands appear immediately; without it they can take up to an hour.

A player who keeps retrying only triggers one notice per `repeat_cooldown_seconds`
(10 min).

## Player stats & public web page

The monitor can record every login, logout and death into a small **SQLite**
database (`sqlite3`, built into Python — no server, one file) and regenerate a
self-contained public web page of leaderboards from it: most play time, most
deaths, longest session, most visits, plus server totals (total hours, unique
players, peak players online at once). Enable it with `database` and
`stats_site` blocks in `config.json`:

```json
"database": { "path": "valheim_stats.db", "enabled": true },
"stats_site": { "output": "/var/www/valheimstats/index.html", "render_interval_seconds": 60 }
```

- `play_sessions` — one row per session: `player`, `login_at`, `logout_at`,
  `deaths`, and a generated `duration_seconds` column (time in game, capped at the
  last log line seen while a session is still open).
- `deaths` — one row per death. `concurrency` — the online count over time, for
  "most online at once".

Useful commands:
```bash
python3 valheim_discord_monitor.py --config config.json --backfill server.log   # seed history from a log file
python3 valheim_discord_monitor.py --config config.json --render-site           # write the page once
python3 stats_site.py --db valheim_stats.db --out /var/www/valheimstats/index.html --config config.json
```

The page is static HTML — no scripts, no inputs, no auth needed — so it is safe to
host publicly. **Full deployment (DNS, nginx/Caddy, backfill): see
[DEPLOY_STATS.md](DEPLOY_STATS.md).**

The page is laid out as three tabs (client-side, no server round-trip): **Server**
(totals + recent activity), **Vikings** (the play/death/longest/visits
leaderboards), and **Achievements** (below). The active tab is remembered in the
URL hash so a refresh keeps you where you were.

## Steam achievements

For players who connect through **Steam** (not Xbox/GamePass), the monitor can
show their public Valheim achievement progress on the page — an Achievements tab
with a card per player (avatar, unlocked/total, a progress bar, their latest
unlock) plus a "Recent Unlocks" feed across everyone.

How the link is made: nothing to configure per player. When a Steam player
connects, the server log carries a handshake line
(`PlayFab socket … received local Platform ID Steam_7656…`) that the monitor
correlates to that player's character login, storing the character↔SteamID
mapping. A background refresh then pulls, from the **public** Steam Web API:

- the game's achievement catalogue once a day (names/icons) — `GetSchemaForGame`
- each player's profile (persona, avatar) — `GetPlayerSummaries`
- each player's unlocked achievements — `GetPlayerAchievements`

Enable it with a `steam` block in `config.json`:
```json
"steam": { "enabled": true, "api_key": "…", "refresh_seconds": 1800, "top_n": 25 }
```
Leave `api_key` out and set the `STEAM_API_KEY` environment variable instead
(the same free key the `steamapi` source uses, from
<https://steamcommunity.com/dev/apikey>). With more than `top_n` linked players
the most-recently-seen ones are refreshed. The refresh runs on its own slow
cadence (`refresh_seconds`, 30 min default) to stay well within API limits,
independent of the page render.

```bash
python3 valheim_discord_monitor.py --config config.json --refresh-steam   # fetch once, render, exit
python3 steam.py --db valheim_stats.db --key $STEAM_API_KEY                # standalone refresh
```

Notes:
- **Only Steam players appear.** Xbox/GamePass players never send a Steam
  Platform ID, so they can't be linked — this is a Steam-only feature.
- **A player must make their profile (and game details) public** for
  achievements to show. Steam defaults game details to public, but a private
  profile is stored with a "profile is private" note on the card so they know to
  flip the setting, rather than being hidden.
- Links populate **going forward**, as Steam players connect — historical logins
  can't be backfilled because old logs were filtered to event lines and no longer
  carry the handshake.

## Unattended updates & nightly backups (LOW.MS)

With a `maintenance` block, the monitor keeps the server patched and backed up
**only while nobody is playing**. Every 15 minutes (`check_interval_seconds`):

1. **Is it empty?** Valheim writes `Connections N ZDOS` to its log every 10
   minutes. The server counts as empty only when that line is recent (under
   `count_max_age_seconds`, 13 min), reads `0`, nobody has logged in since, and the
   monitor tracks nobody online. A stale or missing count means *not empty*, so a
   dropped log feed never looks like an empty server. After the monitor restarts, it
   waits for a fresh count before doing anything.
2. **Backup:** inside the `backup.window` (02:00–06:00 in `timezone`, default
   `America/Los_Angeles` — the panel's own clock) and not yet done that night:
   **stop → back up → start**. LOW.MS notes that a backup of a running server skips
   any file Valheim has locked, so it stops first. When the backup allowance is full,
   the oldest *unpinned* backup is deleted and the backup retried
   (`delete_oldest_when_full`).
3. **Update:** the panel is asked hourly (`update.check_interval_seconds`) whether an
   update is waiting — that check runs **whether or not anyone is playing**, because it
   only reads. When one is waiting it is remembered and installed **as soon as the server
   is empty**, not at the next quarter-hour. If a backup is also due, the order is
   stop → backup → update → start, so every update has a fresh backup taken just before it.

A queued update waits for `update.empty_settle_seconds` (180) of *continuous* emptiness
before installing, so a crossplay player who drops and reconnects doesn't get a server
restart in the face; if someone rejoins during that window the timer restarts. A failed
update backs off for `update.retry_cooldown_seconds` (1 hour) instead of retrying on
every poll. When an update is found while people are playing, Discord says so once, and
the install announces itself when it happens.

The server is **always started again** afterwards, even when a step fails. Discord
gets one "down for maintenance" line and one "finished" (or "had a problem") line;
the usual "restarting / back online / offline" posts for that restart are held
back. A real crash afterwards still alerts normally, and if the server doesn't come
back, the "offline" alert still fires once the quiet period ends.

**Which API does what.** Backups, stop/start and job status use the documented
[LOW.MS Public API](https://api.prod.nexus.low.ms/v1/docs) with a `lowms_` key.
The public API has **no update endpoint** (re-checked Sept 2026), so the update check
(`update-info`) and install (`update_server`) use the same private panel endpoints as
the panel's own **Update** button. Those need a panel sign-in: either the `nexus`
source's session, or `maintenance.panel_login` (or `NEXUS_EMAIL` / `NEXUS_PASSWORD`)
when the log source doesn't have one — which is the case with the `lowms` source. If
LOW.MS changes those endpoints, updates stop and log a warning while backups carry on.

Setup:
1. Panel → Account → **API Keys**: create a key with scopes `backups:read`,
   `backups:write`, `servers:power`, pinned to this server.
2. Put it in the environment (`LOWMS_API_KEY`) or `maintenance.api_key`, and set
   `"enabled": true` in the `maintenance` block (see `config.example.json`).
3. Check it (read-only: verifies the key's scopes, lists backups, shows update
   status and whether it's inside the window):
   ```bash
   python3 valheim_discord_monitor.py --config config.json --maintenance-check
   ```
4. Optional first night: `"dry_run": true` logs what it *would* do without doing it.
5. Turn **off** LOW.MS's own *Update* / *Backup* scheduled tasks (Settings →
   Scheduled tasks), which run whether or not anyone is online.

## Running it permanently

**Docker Compose (self-hosted Linux server)**

The included `docker-compose.yml` is for a server set up like the
[Pi My Life Up guide](https://pimylifeup.com/valheim-dedicated-server-linux/): a `valheim`
user, server in `/home/valheim/valheimserver`, `-savedir /home/valheim/valheim_save_data`.
The monitor reads the console log from `/home/valheim/logs` and, for the admin bot,
edits the list files in the save dir.

1. The guide's systemd unit sends the console to the journal only. To get it into a file
   the monitor can tail, add to `[Service]` in `/etc/systemd/system/valheimserver.service`:
   ```ini
   StandardOutput=append:/home/valheim/logs/valheim_console.log
   StandardError=inherit
   ```
   then `sudo -u valheim mkdir -p /home/valheim/logs && sudo systemctl daemon-reload && sudo systemctl restart valheimserver`.
   (`append:` needs systemd 240+. The file grows over time; truncating it is safe because the monitor notices and starts over.)
2. Configure and start:
   ```bash
   cp config.selfhosted.example.json config.json    # fill in webhook, bot token, ids
   docker compose up -d --build
   docker compose logs -f
   ```
   Put secrets in a `.env` file next to the compose file (`DISCORD_WEBHOOK_URL=…`,
   `DISCORD_BOT_TOKEN=…`) instead of `config.json` if you like. Both are git-ignored.
   Set `TZ=America/Los_Angeles` (etc.) there too so times line up with the server.

After `git pull`, run `docker compose up -d --build` again.

**Plain Docker**
```bash
docker build -t valheim-discord-monitor .
docker run -d --name valheim-monitor --restart unless-stopped \
  -v "$PWD":/app -v /home/valheim/logs:/logs:ro valheim-discord-monitor
```

**systemd** (`/etc/systemd/system/valheim-monitor.service`)
```ini
[Unit]
Description=Valheim Discord monitor
After=network-online.target

[Service]
WorkingDirectory=/opt/valheim-monitor
ExecStart=/usr/bin/python3 valheim_discord_monitor.py --config config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Windows** — Task Scheduler → "At startup" → `pythonw.exe valheim_discord_monitor.py --config config.json`,
or run it in a terminal.

## Options

| Config key | Default | Meaning |
|---|---|---|
| `events` | mode default | Log mode: `login`, `logout`, `death`, `respawn`, `server_up`, `join_refused`. Count mode: `player_joined`, `player_left`, `server_online`, `server_offline`. |
| `poll_interval_seconds` | 15 | How often to poll. |
| `source.offline_after` | 3 | Count mode: failed queries in a row before "offline". |
| `source.api_key` | — | `steamapi` only; or `STEAM_API_KEY` env var. |
| `steam.enabled` | false | Pull public Steam achievements onto the page. |
| `steam.api_key` | — | Steam Web API key; or `STEAM_API_KEY` env var. |
| `steam.refresh_seconds` | 1800 | How often to refresh Steam data. |
| `steam.top_n` | 25 | Most-recently-seen linked players to refresh. |
| `maintenance.enabled` | false | Unattended updates + nightly backups (LOW.MS). |
| `maintenance.api_key` | — | `lowms_` key; or `LOWMS_API_KEY` env var. |
| `maintenance.check_interval_seconds` | 900 | How often to check (only acts when empty). |
| `maintenance.timezone` | America/Los_Angeles | Clock for the backup window. |
| `maintenance.backup.window` | 02:00-06:00 | Nightly backup window (once per night). |
| `maintenance.backup.stop_server` | true | Stop the server for a consistent backup. |
| `maintenance.backup.delete_oldest_when_full` | true | Delete the oldest unpinned backup when the allowance is full. |
| `maintenance.update.enabled` | true | Install game updates as soon as the server is empty. |
| `maintenance.update.check_interval_seconds` | 3600 | How often to ask whether an update is waiting (runs even with players online). |
| `maintenance.update.empty_settle_seconds` | 180 | Continuous emptiness required before installing. |
| `maintenance.update.retry_cooldown_seconds` | 3600 | Wait this long after a failed update before retrying. |
| `maintenance.panel_login` | — | Panel email/password for updates when the source has no session (e.g. `lowms`); or `NEXUS_EMAIL` / `NEXUS_PASSWORD`. |
| `maintenance.dry_run` | false | Log the plan without doing anything. |
| `admin_bot.enabled` | false | Post refused join attempts to a private channel with Permit / Ban buttons. |
| `admin_bot.token` | — | Discord bot token; or `DISCORD_BOT_TOKEN` env var. |
| `admin_bot.guild_id` / `channel_id` | — | Your Discord server, and the admin channel for notices. |
| `admin_bot.admin_user_ids` / `admin_role_ids` | — | Who may press the buttons and use `/valheim`. |
| `admin_bot.save_dir` | — | Folder holding `permittedlist.txt` / `bannedlist.txt` (must be writable). |
| `admin_bot.repeat_cooldown_seconds` | 600 | One notice per player per this many seconds. |
| `discord.embeds` | true | Coloured embed vs plain text. |
| `discord.show_player_count` | true | Footer with the current online count. |
| `discord.messages` | see example | Per-event templates; `{player}`, `{server}`, `{who}`, `{count}`, `{max}` placeholders. |
| `state_file` | `monitor_state.json` | Where the read offset is remembered. |

## Notes
- Names come from the character, not the Steam account.
- On a PlayFab/crossplay server a disconnect (clean or timeout) shows up as the
  `Destroying abandoned … owner <id>` line; the monitor emits one logout per player.
- If the server restarts, the log is truncated and the monitor resets automatically.

## License

MIT — see [LICENSE](LICENSE).
