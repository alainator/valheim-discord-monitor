# Valheim → Discord monitor (no mods)

Posts Valheim server activity to a Discord channel without installing anything
on the game server, so Steam achievements keep working. Two modes:

| Mode | Needs | Events |
|---|---|---|
| **Count mode** (`a2s` or `steamapi`) | Only the server's IP — polls the Steam query port (game port + 1), or Steam's master server via the Web API when that port is firewalled | player joined / left (count only), server online / offline |
| **Log mode** (`lowms`, `nexus`, `ftp`, `sftp`, `file`, `http`) | Read access to `valheim_console.log` — on LOW.MS via the public API (`lowms`) | named **login**, **logout**, **death**, respawn, **refused joins** |

Log mode is the one you want: names and deaths. On LOW.MS use the `lowms`
source: the public API now serves the console with a plain API key (no FTP/SFTP
exists there, and the older `nexus` source had to sign in to the panel as you). Count mode is the fallback for hosts where nothing else
works — note the Steam-based sources report a stale count on crossplay
servers, because relayed players never register with Steam.

Python 3.9+, no third-party packages for the core monitor. Optional extras: SFTP
(`paramiko`) and the Discord admin bot (`discord.py`, see `requirements.txt`).

On top of the Discord posts it can also, with the optional **admin bot**:
- **Alert you when someone is refused** by your ban or permitted list, with **Permit** /
  **Ban** buttons ([join-attempt alerts](#join-attempt-alerts--discord-admin-bot)).
- Show **who's online**: in a voice channel's name ([status
  channel](#status-voice-channel)), in a column of [stat channels](#stat-channels) (join code,
  uptime, deaths this week, next game night…), and in a live message with the join code, uptime,
  version, last save, backup and raid ([status board](#status-board)).
- Answer **`/valheim join`** for anyone: the current join code, the address and how to
  connect ([commands](#discord-commands)).
- **Restart and update the server** from Discord with a countdown warning, and post when a
  Valheim update is waiting or installed ([auto-updates](#auto-updates-and-restarts-from-discord)).
- **Change world settings** (preset, modifiers, setkeys) from Discord, checked on the
  server before anything is written ([world settings](#auto-updates-and-restarts-from-discord)).
- **Community:** `/valheim stats` and `/valheim top`, DMs when friends come online, an
  "In Valheim" role, weekly **title roles** for the leaderboard leaders (Heimdall, Hel,
  Sleipnir, Thor, Bragi), an **Odin** role for the server owner, game-night signups with
  reminders, a world map link, and a smoother first join
  ([players & community](#players--community), [roles & names](#roles--names)).
- **Server health:** warnings before the save disk fills up or saves get slow, and an
  optional daily restart while nobody's on ([server health](#server-health)).
- **Steam achievements in Discord:** unlock posts, achievements in `/valheim stats`, a
  leaderboard and the Bragi title, with a free Steam API key
  ([achievements](#steam-achievements-in-discord)).

And without the bot:
- **Raid alerts, version-mismatch alerts, session summaries, first-visit welcomes,
  milestones and a weekly recap** ([extras](#extras-raids-summaries-milestones-recap-board-backups)).
- **Copy Valheim's world backups to another disk** ([backup copies](#world-backup-copies)).
- Keep **play stats** in SQLite and publish a leaderboard page, with players' **Steam
  achievements**.
- Run **unattended updates and nightly backups** on LOW.MS.

**Running the Valheim server on your own Linux machine?** Start with the
[self-hosted quick start](#self-hosted-linux-server-quick-start).

**What's new** (already running it? `git pull && docker compose up -d --build`):
- **Stat channels:** three bot-made categories of locked voice channels: Heimdall's Watch
  (who's online by name, join code, uptime, saves, backups, disk), The Saga (this week's
  numbers, next game night) and the Hall of Champions (Odin and every title holder). Your
  status channel moves in, and the status board isn't needed any more:
  `"stat_channels": {"enabled": true}` ([stat channels](#stat-channels)).
- **Steam achievements in Discord:** unlock posts from Huginn, achievements in
  `/valheim stats`, a "Most achievements" board in `/valheim top`, and a fifth title role,
  **Bragi**. No web page needed ([details](#steam-achievements-in-discord)).
- **[Roles & names](#roles--names):** one overview of the bot (Muninn), the announcer
  (Huginn), and every role the bot hands out.
- **Sleipnir replaces Huginn** as the "most visits" title. The bot renames the existing
  role, so nothing to do ([title roles](#title-roles)).
- **The bot creates the "In Valheim" role** with `"online_role": true`, instead of you
  making it and copying its ID ([players & community](#players--community)).
- **Odin, the server owner's role:** `"owner_role": true` gives the Discord server's owner
  an "Odin" role near the top of the member list ([players & community](#players--community)).
- **Title roles:** the leader of each `/valheim top` board gets a Norse role (Heimdall,
  Hel, Sleipnir, Thor, Bragi), reassigned weekly. Turn on with `"titles": {"enabled": true}`
  ([title roles](#title-roles)).
- **`/valheim map` works with Valheim 1.0 worlds.** It reads the seed from the world
  folder (`worlds_local/<world>/_main.<N>.fwl2`); set `admin_bot.map.seed` if it can't
  ([players & community](#players--community)).
- **Community commands:** `/valheim stats`, `top`, `notify`, `link`, `request-access`,
  `plan` and `map`, plus an optional "In Valheim" role
  ([players & community](#players--community)).
- **Server health:** low-disk and slow-save warnings, and an optional daily restart while
  nobody's on ([server health](#server-health)).
- **World settings from Discord:** `/valheim settings`, `preset`, `modifier`, `setkey`
  ([host/README.md](host/README.md#world-settings-from-discord-preset-modifiers-setkeys)).
- **`/valheim join`** and a status board with the join code, uptime, version, last save,
  backup and raid ([status board](#status-board)).
- **Secrets belong in `.env`.** If your webhook URL or bot token is in `config.json`, move
  it ([keeping secrets safe](#keeping-secrets-safe)).

**Contents:**
- **Getting started:** [Self-hosted quick start](#self-hosted-linux-server-quick-start) ·
  [Keeping secrets safe](#keeping-secrets-safe) ·
  [Count mode](#count-mode-quick-start) · [Log mode](#log-mode)
- **Admin bot:** [Join alerts & setup](#join-attempt-alerts--discord-admin-bot) ·
  [All Discord commands](#discord-commands) · [Status channel](#status-voice-channel) ·
  [Stat channels](#stat-channels) ·
  [Players & community](#players--community) · [Title roles](#title-roles) ·
  [Roles & names](#roles--names) ·
  [Testing](#testing-it) ·
  [Troubleshooting](#troubleshooting)
- **Extras:** [Raids, summaries, milestones, recap](#extras-raids-summaries-milestones-recap-board-backups) ·
  [Status board](#status-board) · [Backup copies](#world-backup-copies) ·
  [Server health](#server-health) ·
  [Auto-updates, restarts & world settings](#auto-updates-and-restarts-from-discord)
- **Stats & LOW.MS:** [Stats page](#player-stats--public-web-page) ·
  [Steam achievements](#steam-achievements) ([in Discord](#steam-achievements-in-discord)) ·
  [Updates & backups (LOW.MS)](#unattended-updates--nightly-backups-lowms)
- **Reference:** [Running it permanently](#running-it-permanently) · [Options](#options) ·
  [Server admin tips](#server-admin-tips) · [Development](#development) ·
  [About this fork](#about-this-fork)

## Self-hosted Linux server (quick start)

This is the setup for a dedicated server you run yourself, e.g. installed with the
[Pi My Life Up guide](https://pimylifeup.com/valheim-dedicated-server-linux/):
- a `valheim` user
- the server in `/home/valheim/valheimserver`, run by the systemd unit `valheimserver.service`
- `-savedir /home/valheim/valheim_save_data`

The monitor runs in Docker on the same machine. It tails the server's log file and,
if you enable the admin bot, edits the ban and permitted lists in the save dir.

1. **Have the server write its log to a file.** Add `-logFile` to the `ExecStart` line
   in `/etc/systemd/system/valheimserver.service`:
   ```
   ExecStart=/home/valheim/valheimserver/valheim_server.x86_64 … -savedir /home/valheim/valheim_save_data -logFile /home/valheim/logs/valheim_console.log
   ```
   Then create the folder and restart the server:
   ```bash
   sudo -u valheim mkdir -p /home/valheim/logs
   sudo systemctl daemon-reload && sudo systemctl restart valheimserver
   ```
   To check what your server actually uses: `systemctl cat valheimserver | grep ExecStart`.
   If your `-savedir` or log path differs, remap it in `docker-compose.override.yml` with
   the same container path, e.g. `- /srv/valheim/logs:/logs:ro`. Compose replaces a mount
   when the override uses the same target.
2. **Get the code and configure it:**
   ```bash
   git clone https://github.com/alainator/valheim-discord-monitor.git
   cd valheim-discord-monitor
   cp config.selfhosted.example.json config.json
   cp .env.example .env && chmod 600 .env
   ```
   - In `config.json`, set `server_name`.
   - In `.env`, set `DISCORD_WEBHOOK_URL` (channel → Edit Channel → Integrations →
     Webhooks → New Webhook → Copy Webhook URL) and `TZ`.
   - If you don't want the admin bot yet, set `admin_bot.enabled` to `false`.
     Otherwise follow [Setting up the bot](#setting-up-the-bot).

   Keep secrets in `.env` rather than `config.json`. Both files are git-ignored, but a
   `grep` or a pasted snippet of `config.json` can easily leak a token. See
   [keeping secrets safe](#keeping-secrets-safe).
3. **Start it:**
   ```bash
   docker compose up -d --build
   docker compose logs -f
   ```
   A healthy start logs `Monitoring file source for <your server>; posting [...]`,
   plus `; admin bot on` and `admin_bot: connected as …` if the bot is enabled.

**Your own folders go in `docker-compose.override.yml`**, next to `docker-compose.yml`.
Docker Compose merges it in automatically, and git ignores it, so `git pull` never
conflicts with your changes. Leave `docker-compose.yml` as it comes from the repo. For
example:
```yaml
services:
  valheim-discord-monitor:
    volumes:
      - /mnt/backups/valheim:/backups           # backup copies
      - /home/valheim:/valheim_home:ro          # updater log + world settings
      - /home/valheim/bot:/bot                  # restart / settings requests
```
Check the result with `docker compose config | grep -E "source:|target:"`.

**Updating:** `git pull && docker compose up -d --build`.

**After editing `config.json` or `.env`:** `docker compose up -d --force-recreate`. The
monitor reads its settings only at start-up.

**Looking inside `/home/valheim`:** that folder is private to the `valheim` user. Use
`sudo`, and wrap wildcards in `sudo sh -c '…'`. Otherwise your own shell expands the
`*` before `sudo` runs and reports "No such file":
```bash
sudo sh -c 'ls -la /home/valheim/valheim_save_data/*list.txt'
```
The container runs as root, so it can read and edit those files anyway.

**Next steps**, each optional:
1. [Set up the admin bot](#setting-up-the-bot): refused-join alerts and `/valheim` commands.
2. Add a [status voice channel](#status-voice-channel), a [status board](#status-board), and
   the bot-made [stat channels](#stat-channels) (`"stat_channels": {"enabled": true}`).
3. Turn on the [extras](#extras-raids-summaries-milestones-recap-board-backups): raids,
   summaries, milestones, weekly recap. They're just names in `events`.
4. [Copy world backups](#world-backup-copies) to another disk.
5. Link your update script and world settings ([host/README.md](host/README.md)) for
   `/valheim restart`, update posts and `/valheim modifier`.

### Keeping secrets safe

Two values give control of your Discord channel to anyone who has them:
- **The webhook URL** (`DISCORD_WEBHOOK_URL`): anyone with it can post to the channel.
- **The bot token** (`DISCORD_BOT_TOKEN`): anyone with it can act as your bot.

Keep both in `.env` only:
- `.env` wins over `config.json`: when `DISCORD_WEBHOOK_URL` or `DISCORD_BOT_TOKEN` is
  set there, `webhook_url` / `admin_bot.token` in `config.json` are ignored.
- `.env` and `config.json` are git-ignored, so neither is committed. Keep it that way.
- When asking for help, share `config.json` without the secrets, and never paste `.env`.
  To check that a token is loaded without printing it, use the
  [length check](#troubleshooting) instead.

**Moving a secret out of `config.json`:**
1. Add it to `.env`, with no quotes and no spaces around `=`:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/…
   ```
2. In `config.json`, delete the whole `"webhook_url": "…",` line (or `"token": "…",` in
   `admin_bot`). If it was the last line in its block, also delete the comma at the end
   of the line above it. Check with `python3 -m json.tool config.json > /dev/null`.
3. `docker compose up -d --force-recreate`, then `docker compose logs --tail 20`. No
   `No Discord webhook URL configured` error means it was picked up.

**If one has leaked** (pasted in a chat, a screenshot, an issue, a commit), replace it.
The old one keeps working until you do:
- **Webhook:** the channel → Edit Channel → Integrations → Webhooks → select it →
  **Delete Webhook**. Create a new one, copy its URL into `.env`, and recreate the
  container.
- **Bot token:** [Discord developer portal](https://discord.com/developers/applications)
  → your app → Bot → **Reset Token**. Put the new token in `.env` and recreate the
  container. The old token stops working immediately.

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
| Refused join | `Player Stranger : V_7656… is blacklisted or not in whitelist.` (see [admin bot](#join-attempt-alerts--discord-admin-bot)) |
| Restart / online | `OnApplicationQuit` / `ZNet Shutdown` … then `Game server connected` |

The monitor tails the log by byte offset, so its own restarts never re-post. It runs the
lines through a small state machine and posts an embed to a Discord webhook. When the game
server restarts and starts a fresh log, the monitor notices and reads the new log from the
top. For local files that works even if the new log is already longer than the old one,
e.g. when the monitor was down during the restart.

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
   Secrets can be given as environment variables instead of in the file. With Docker
   Compose, put them in `.env`:

   | Variable | Replaces |
   |---|---|
   | `DISCORD_WEBHOOK_URL` | `discord.webhook_url` |
   | `DISCORD_BOT_TOKEN` | `admin_bot.token` |
   | `STEAM_API_KEY` | `steam.api_key` / `source.api_key` (steamapi) |
   | `LOWMS_API_KEY` | `source.api_key` (lowms) / `maintenance.api_key` |
   | `NEXUS_EMAIL`, `NEXUS_PASSWORD`, `NEXUS_TOKEN` | the `nexus` source / panel login |
   | `VALHEIM_LOG_USER`, `VALHEIM_LOG_PASSWORD` | `source.user` / `source.password` (ftp, sftp) |

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

LOW.MS's Nexus panel does not offer FTP/SFTP (confirmed with their support,
Sept 2026). Use the `lowms` source there.

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
Player Stranger : V_76561198000000000 is blacklisted or not in whitelist.
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

  Only the Discord users and roles you list can press them. For IDs you already know,
  there are also slash commands (`/valheim permit`, `ban`, `unban`, `unpermit`,
  `lists`). See [all Discord commands](#discord-commands).

  **Permit** never *starts* a permitted list. With an empty `permittedlist.txt`
  the server is open to everyone who isn't banned, and adding the first ID would lock
  everyone else out, so in that case Permit only unbans.

The bot edits the list files directly, so it has to run **on the same machine as
the server** (self-hosted, `file` source) with write access to the save dir. It
writes Steam IDs **in the style your list files already use**. Valheim 1.0's lists say
`V_7656…`, while the same server's log says `Steam_7656…`, so a Permit writes `V_7656…`
to match. Other platforms are written as the server printed them (`X_`, `S_`, `N_`, or
`Xbox_…` etc. on older builds). `V_7656…`, `Steam_7656…` and a bare `7656…` are matched as
the same player. Community docs say list edits apply without a restart
(the next join attempt is checked against the file). If a change doesn't seem to take
effect, `sudo systemctl restart valheimserver`.

### Discord commands

All commands are under `/valheim`. **Admin** commands only work for the users and roles
in `admin_user_ids` / `admin_role_ids`. Replies to admin commands are only visible to you.

| Command | Who | What it does | Needs |
|---|---|---|---|
| `/valheim join` | Anyone | Join code, address, password (spoiler) and how to connect; only the asker sees it | The bot; `admin_bot.join` for the address |
| `/valheim online` | Anyone | Who's on right now, and since when | The bot |
| `/valheim stats [player]` | Anyone | Play time, rank, visits, longest session, deaths, first/last seen, Steam achievements. No name = your linked character | Stats database |
| `/valheim top [category]` | Anyone | Leaderboard: time played, deaths, visits, longest session, or Steam achievements | Stats database |
| `/valheim titles [refresh]` | Anyone (`refresh`: admin) | Who holds each title role; `refresh` reassigns them now | [`titles`](#title-roles) |
| `/valheim notify <when> [player]` | Anyone | DM me when the first player joins an empty server, or when a given character joins; `off` / `list` | Stats database |
| `/valheim link <character>` / `unlink` | Anyone | Link your Discord account to your character (stats, role, mentions) | Stats database |
| `/valheim request-access <character>` | Anyone | New player: "I'll join as …". The admins' refused-join notice then says who it is | Stats database |
| `/valheim plan <title> <when>` | Anyone | Game night with Going / Maybe / Can't buttons and a reminder ping | Stats database |
| `/valheim map` | Anyone | World seed and a map link (spoilers; only the asker sees it) | `save_dir` |
| `/valheim permit <id>` | Admin | Unban, and add to the permitted list if you use one | `save_dir` |
| `/valheim ban <id>` | Admin | Ban, and remove from the permitted list | `save_dir` |
| `/valheim unban <id>` / `unpermit <id>` | Admin | Remove from one list | `save_dir` |
| `/valheim lists` | Admin | Show the permitted, banned and admin lists | `save_dir` |
| `/valheim backups` | Admin | Newest backup copies, with size and age | [`backups`](#world-backup-copies) |
| `/valheim update-check` | Admin | Check for a Valheim update now | [host helper](host/README.md) |
| `/valheim restart [minutes] [reason]` | Admin | Restart (installs any waiting update); warns players at N/5/1 min, early if everyone leaves | [host helper](host/README.md) |
| `/valheim restart-cancel` | Admin | Stop a restart countdown | — |
| `/valheim settings` | Admin | Current preset, modifiers and setkeys, and every allowed value | [world settings](host/README.md#world-settings-from-discord-preset-modifiers-setkeys) |
| `/valheim modifier <name> <value>` | Admin | Change a modifier, e.g. `raids more`; `normal` resets it | world settings |
| `/valheim preset <name>` | Admin | Change the preset; `default` removes it | world settings |
| `/valheim setkey <key> on\|off` | Admin | Turn nomap, playerevents, passivemobs or nobuildcost on or off | world settings |

Player IDs look like `V_76561198…` (Steam), `X_…`, `S_…` or `N_…`. World-setting changes
apply at the next restart; the bot offers a "Restart in 5 min" button.

**Buttons:** refused-join notices have **Permit / Ban / Ignore**, and world-setting
confirmations have **Restart in 5 min**. They keep working after the bot restarts.

### Setting up the bot

This takes about five minutes in Discord's developer portal.

**1. Create the bot.**
1. Go to <https://discord.com/developers/applications> → **New Application** → give it a name.
   **Muninn** fits the theme (see [roles & names](#roles--names)).
2. Open **Bot** → **Reset Token** → **Copy**.
3. Put the token in `.env` as `DISCORD_BOT_TOKEN=<token>`: no quotes, no spaces.
   - It must be the token from the **Bot** page: about 70 characters with two dots. The
     **Client Secret** on the OAuth2 page (32 characters, no dots) won't work.
   - Every **Reset Token** click invalidates the previous token.
   - Treat the token like a password; anyone who has it controls the bot.
4. Leave the **Privileged Gateway Intents** off; the bot doesn't need them.
5. Optional: switch off **Public Bot** so nobody else can invite it. If Discord complains,
   first set **Installation** → **Install Link** to **None**.

**2. Invite it.**
1. Open **OAuth2** → **URL Generator**.
2. Under scopes, tick **`bot`** and **`applications.commands`**.
3. Under bot permissions, tick **View Channels**, **Send Messages**, **Embed Links** and
   **Read Message History**. Also tick:
   - **Manage Channels**, for the [status voice channel](#status-voice-channel) and the
     [stat channels](#stat-channels);
   - **Manage Roles**, for the ["In Valheim", Odin and title roles](#roles--names);
   - **Manage Events**, only if game nights should create Discord Events.
4. Open the generated URL, pick your Discord server, and click **Authorize**.

**3. Make a private admin channel.** For example `#valheim-admin`, with Private Channel on.
Then channel settings → **Permissions** → add the bot with View Channel, Send Messages
and Embed Links.

**4. Copy the IDs.** Turn on User Settings → Advanced → **Developer Mode**, then right-click
and **Copy ID** on each of these:

| Right-click | Goes in |
|---|---|
| Your server icon | `guild_id` |
| The admin channel | `channel_id` |
| Yourself | `admin_user_ids` |
| An admin role, if you want a whole role to have access | `admin_role_ids` |

**5. Configure.** Fill in the `admin_bot` block of `config.json` as in
`config.selfhosted.example.json`:
- Keep the IDs as quoted strings.
- `save_dir` is the folder with the list files *as the monitor sees it*. In Docker that's
  `/valheim_save_data`, which `docker-compose.yml` maps to `/home/valheim/valheim_save_data`.
- Check the file is valid JSON: `python3 -m json.tool config.json > /dev/null && echo OK`.

**6. Start.** Run `docker compose up -d --build --force-recreate`, or
`pip install -r requirements.txt` and restart the monitor if you don't use Docker.
- The log shows `admin_bot: connected as …` and the bot turns online in Discord.
- With `guild_id` set, `/valheim` commands appear immediately; without it, they can take up
  to an hour.

A player who keeps retrying triggers only one notice per `repeat_cooldown_seconds`
(10 min).

### Status voice channel

The bot keeps a **voice** channel's **name** showing the server's state, so everyone sees it
in the channel list without opening anything.

**Three features, easy to mix up:**

| Setting | Channel type | What you get |
|---|---|---|
| `status_channel` (this section) | **Voice** channel you make | The channel's *name* changes: `🟢 Valheim: 3 online` |
| [`stat_channels`](#stat-channels) | **Voice** channels the bot makes | Three categories of locked channels: who's online by name, join code, uptime, this week's numbers, title holders… |
| [`status_board`](#status-board) | **Text** channel | One *message* the bot keeps editing, with who's on, version, uptime, last save, backup and raid |

You can use any or all of them. `status_channel` and `status_board` each need their own
channel of the right type; the bot won't rename a text channel (it logs a warning and skips
it). The stat channels need no setup in Discord, and they take over your `status_channel`
(moved into their category, with names), so with them on you need neither of the other two.

**The channel shows one of three names at a time**, whichever matches the server right now:

| Server state | Setting | Default name |
|---|---|---|
| People playing | `online` | `🟢 Valheim: 3 online` (the number follows the player count) |
| Up, nobody on | `empty` | `🟢 Valheim: empty` |
| Down or restarting | `offline` | `🔴 Valheim: offline` |

**Setup:**
1. Create a **voice** channel. Nobody needs to join it: in its permissions, deny
   **Connect** for @everyone so it's just a label.
2. In the same permissions screen, add the bot with **View Channel** and **Manage Channels**.
3. Right-click the channel → **Copy Channel ID**, and add this inside the `admin_bot` block:
   ```json
   "status_channel": { "channel_id": "123456789012345678" }
   ```
   That's all that's required. To change the wording, add any of the three names; you
   only need the ones you want to change:
   ```json
   "status_channel": {
     "channel_id": "123456789012345678",
     "online": "⚔️ {server}: {count} online",
     "empty": "💤 {server}: empty",
     "offline": "🔴 {server}: down"
   }
   ```
   `{count}` is the number online; `{server}` is `server_name`.
4. `docker compose up -d --force-recreate`, then check the log:
   `admin_bot: status channel is '…'; it's renamed when the server's state changes`.

**What to expect:**
- **Nothing happens straight away.** The name only changes when the server's state
  changes: someone joins or leaves, or the server stops or starts. If the name already
  matches, nothing needs doing.
- **At most one rename every 5 minutes.** Discord lets a bot rename a channel only twice
  per 10 minutes. When a change has to wait, the log says when it will happen:
  `status channel: renaming to '🟢 Valheim: empty' at 16:19:45`. A burst of joins and
  leaves in between costs one rename, with the latest count. So a player who joins and
  leaves within a minute can leave the channel saying "1 online" for up to 5 minutes.
- **Restarting the monitor starts the 5-minute wait again,** so avoid recreating the
  container repeatedly while testing.
- **Right after the monitor starts,** the name is left alone until the count is known: the
  next join or leave, or the server's `Connections` log line every 10 minutes.
- **Offline** shows as soon as the server logs a shutdown, or once the log has been silent
  for `stale_after_seconds` (15 min), which catches a crash.
- **Log times are UTC** unless you set `TZ` in `.env`, e.g. `TZ=America/Los_Angeles`.

### Stat channels

Locked voice channels at the top of your channel list whose names show the server's
numbers and title holders. The bot creates, fills and updates them; members can see them
but not join. Each name says what it is, because Discord doesn't allow descriptions on
voice channels or categories.

```
🛡️ HEIMDALL'S WATCH · LIVE
  🟢 3 online: Ingrid, Bjorn, Sigrid
  🟢 Server online · l-1.0.16
  🔑 Join code: 482913
  ⏱ Up 3 d
  💾 World saved today 12:04 (1.2 s)
  🗄 Backup: today 04:10
  💽 Disk free: 412 GB
📜 THE SAGA · THIS WEEK
  📈 Peak today: 4
  ⏳ This week: 38 h played
  💀 Deaths this week: 14
  ⚔️ Last raid: The Elder's army (Tue)
  📅 Bonemass run · Sat 20:00
  🧭 23 Vikings have visited
  🏅 312 achievements unlocked
👑 HALL OF CHAMPIONS · TITLES
  👁️ Odin (server owner): Alain
  🛡️ Heimdall (most hours): Ingrid
  💀 Hel (most deaths): Bjorn
  🐎 Sleipnir (most visits): Sigrid
  ⚡ Thor (longest session): Ingrid
  📜 Bragi (most achievements): Bjorn
```

| Key | Shows |
|---|---|
| `players` | Who's online, by name ("+2 more" when the names don't fit in 100 characters) |
| `server` | Online / offline and the version; ⬆️ when the updater has found an update |
| `join_code` | The crossplay join code (known after the first join since the last restart) |
| `uptime` | Time since the server started |
| `saved` | The last world save and how long it took |
| `backup` | When Valheim last made a world backup |
| `disk` | Free space on the save disk |
| `peak_today` | Most players online at once today |
| `hours_week` | Hours played this week (since Monday), all players together |
| `deaths_week` | Deaths this week |
| `last_raid` | The most recent raid and its day |
| `next_plan` | The next game night from `/valheim plan` |
| `vikings` | Characters that have ever played |
| `achievements` | Steam achievements unlocked, all players together |
| `title_owner` | Odin: the Discord server's owner |
| `title_time`, `title_deaths`, `title_sessions`, `title_longest`, `title_achievements` | The [title](#title-roles) holders: Heimdall, Hel, Sleipnir, Thor, Bragi (`titles` means all of them) |

**Setup:** add this to the `admin_bot` block, then `docker compose up -d --force-recreate`:
```json
"stat_channels": { "enabled": true }
```
The bot needs **Manage Channels** and `guild_id`. The categories and channels appear
within a minute.

- **Your status channel moves in.** If you have a `status_channel`, it becomes the
  `players` channel: moved into Heimdall's Watch, locked, and renamed with names. Nothing is
  duplicated, and the old status channel updater stops.
- **The status board isn't needed** with these: everything it showed is here. Remove the
  `status_board` line from `config.json` and delete its message or channel.
- **Choosing channels:** `"show": ["players", "join_code", "deaths_week", "titles"]` lists
  the ones you want, in order. Without `show` you get all of them (the title channels only
  when [titles](#title-roles) are on).
- **One category instead of three:** `"layout": "single"` puts them all in one category,
  named by `"category"` (default "📊 Valheim").
- **Category names:** `"categories": {"watch": "…", "saga": "…", "hall": "…"}` sets them at
  creation. Renaming or moving them later in Discord is fine; the bot remembers everything
  by ID.
- **Removing one:** take it out of `show` first, then delete the channel. If you delete a
  channel that's still in `show`, the bot recreates it.
- **Update speed:** Discord allows each channel 2 renames per 10 minutes, so a channel lags
  a change by up to 5 minutes. Values are kept coarse (hours, days) so they don't hit the
  limit.
- The live channels (`players`, `server`, `join_code`, `uptime`, `saved`, `backup`, `disk`,
  `last_raid`) and Odin don't need the stats database; the rest do.

### Players & community

These need the stats database (`database.path`), which the bot and the monitor share.

- **Stats in Discord.** `/valheim stats Ingrid` shows play time, rank, visits, longest
  session, deaths (and deaths per hour), first and last seen, and Steam achievements
  ([below](#steam-achievements-in-discord)). `/valheim top` has five leaderboards.
  Character names autocomplete.
- **Linking.** `/valheim link Ingrid` ties a character to your Discord account, which lets
  you:
  - run `/valheim stats` with no name;
  - get an @mention in your welcome and milestone posts;
  - get the "In Valheim" role (below).

  A character can only be linked to one account. Anyone can claim an unlinked character,
  since the log can't prove who owns it; admins can `/valheim unlink` anyone's.
- **"In Valheim" role.** Linked players get it while they're in the game, so the member
  list shows who's playing.
  - Turn it on with `"online_role": true`. The bot creates an "In Valheim" role, shown
    separately in the member list, the first time someone joins. It reuses a role that
    already has that name. Use `"online_role": "Vikings online"` for a different name.
  - Or set `online_role_id` to a role you made yourself; that takes precedence.
  - The bot needs **Manage Roles**. A role it creates sits below its own role, so that
    works by itself. A role you made must be **below** the bot's role in Server Settings →
    Roles. You can rename, recolour or move the role later; the bot remembers it by ID.
- **Odin, the owner's role.** With `"owner_role": true`, the bot gives the Discord server's
  owner a role named **Odin** (the Allfather, ruler of Asgard).
  - The bot creates it, shows it separately in the member list, and moves it up to just below
    its own role, so the owner is listed near the top.
  - It follows the server's actual owner, checked at start-up and every 6 hours. If
    ownership is transferred, the role moves with it.
  - It's a title only: no permissions, and it doesn't make anyone a bot admin (that's still
    `admin_user_ids`). Use `"owner_role": "Allfather"` for a different name.
- **Title roles.** See [below](#title-roles).
- **Notifications (DMs).**
  - `/valheim notify first` DMs you when someone joins an empty server.
  - `/valheim notify follow Ingrid` DMs you whenever Ingrid joins.
  - `/valheim notify off` stops everything; `list` shows what you have.

  At most one DM per player per 30 minutes, so a reconnect doesn't spam. The user needs DMs
  from server members allowed.
- **Smoother first joins.** A new player runs `/valheim request-access <character>` before
  joining:
  - the admin channel gets a heads-up;
  - when that character is refused, the notice says **Requested by @them**;
  - clicking **Permit** links the character to them and DMs "you're in, try again".
- **Game nights.** `/valheim plan "Bonemass run" "sat 20:00"` posts a signup in the channel
  with **✅ Going / ❔ Maybe / ❌ Can't** buttons.
  - Times are read in the container's time zone (`TZ`), and shown to everyone in their own
    time zone. It accepts `20:00`, `8pm`, `tomorrow 8pm`, `sat 20:00` and `in 2h`.
  - Everyone going or maybe is pinged `lfg.reminder_minutes` (15) before it starts.
  - With `"lfg": {"discord_event": true}` it also creates a Discord Event; the bot needs
    **Manage Events** for that.
- **Map.** `/valheim map` reads the world seed from the save folder and links to a map of the
  world, visible only to the person who asked, because it shows places nobody has found
  yet. Turn it off with `"map": {"enabled": false}`.
  - It reads Valheim 1.0 world folders (`worlds_local/<world>/_main.<N>.fwl2`, newest save)
    and older `<world>.fwl` files, and skips `_backup_` folders.
  - If it can't read your world file, set the seed by hand: `"map": {"seed": "aB3dE6gH9j"}`
    (in game: F5 console → `seed`, or the seed shown when you pick the world).

```json
"admin_bot": {
  "online_role": true,
  "owner_role": true,
  "lfg": { "reminder_minutes": 15, "discord_event": false },
  "map": { "enabled": true },
  "titles": { "enabled": true }
}
```

### Title roles

The leader of each `/valheim top` board gets a role named after a figure from Norse
mythology:

| Role | Leaderboard | Why |
|---|---|---|
| **Heimdall** | Most time played | The watchman of Bifröst never sleeps and never leaves his post |
| **Hel** | Most deaths | Ruler of the realm of the dead: everyone who dies ends up with her |
| **Sleipnir** | Most visits | Odin's eight-legged horse carries riders between the worlds, and always comes back |
| **Thor** | Longest single session | Drank from a horn linked to the sea and lowered the ocean |
| **Bragi** | Most Steam achievements | God of poetry, who sings the great deeds of heroes in Valhalla |

**Naming tip:** to keep the theme, call the announcement webhook **Huginn** (`"discord":
{"username": "Huginn"}`) and the bot **Muninn** (developer portal → Bot → Username). They're
Odin's two ravens, Thought and Memory, who fly over the world and report back to him, and
the Odin role goes to the server owner. See [roles & names](#roles--names) for the full set.

**How it works:**
- **Weekly.** Every week (default Sunday 18:00, container time zone) the bot looks at the
  leaderboards again. If a title changes hands, it takes the role from the old holder,
  gives it to the new one, and posts a "Titles of the realm" message saying who holds what.
  Nothing is posted in a week where nothing changed. The first run happens right after
  you turn it on.
- **Linked players only.** A role goes to the Discord account linked to the leading
  character (`/valheim link`). If the leader isn't linked, the title is theirs but the role
  waits: the post says so, and the role is given as soon as they link.
- **Ties** keep the current holder, so a title doesn't flip back and forth.
- **All time or weekly.** By default the titles follow the same all-time numbers as
  `/valheim top`. With `"period": "week"`, only the last 7 days count, so the titles move
  around more.
- **`/valheim titles`** shows the current holders. Admins can run `/valheim titles
  refresh:True` to reassign them right away.

**Setup:**
1. Add `"titles": { "enabled": true }` to the `admin_bot` block. It needs `guild_id` and the
   stats database.
2. The bot needs **Manage Roles**. It creates the title roles itself the first time it gives
   them out. New roles go at the bottom of the role list, below the bot's own role, so it can
   manage them.
3. `docker compose up -d --force-recreate`.

You can rename, recolour or move the roles in Server Settings → Roles; the bot remembers
them by ID. Turn on **Display role members separately** on a title role to show its holder
at the top of the member list. Options:

```json
"titles": {
  "enabled": true,
  "period": "all",
  "day": "sunday",
  "hour": 18,
  "channel_id": "",
  "roles": { "time": "", "deaths": "", "sessions": "", "longest": "", "achievements": "" }
}
```
- **`period`:** `all` (default) or `week`.
- **`day` / `hour`:** when to reassign.
- **`channel_id`:** where to post the changes. Empty posts through the webhook, to the same
  channel as logins and deaths.
- **`roles`:** use roles you already have instead of the bot creating them (role IDs).

### Roles & names

Everything the bot adds to your Discord server follows one theme from Norse mythology:

| Name | What it is | Why the name | Turned on with |
|---|---|---|---|
| **Muninn** | The bot | Odin's raven of Memory: watches the server and remembers everything | The bot's name in the developer portal |
| **Huginn** | The webhook that posts logins, deaths, raids, recaps and titles | Odin's raven of Thought: flies out and tells everyone what happened | `"discord": {"username": "Huginn"}` |
| **Odin** | Role for the Discord server's owner, given automatically | The Allfather, ruler of Asgard | `"owner_role": true` |
| **In Valheim** | Role for linked players while they're in the game | (says what it is) | `"online_role": true` |
| **Heimdall** | Title role: most time played | The watchman who never leaves his post | `"titles": {"enabled": true}` |
| **Hel** | Title role: most deaths | Ruler of the realm of the dead | 〃 |
| **Sleipnir** | Title role: most visits | Odin's horse, always carrying riders between the worlds and back | 〃 |
| **Thor** | Title role: longest single session | Drank from a horn linked to the sea and lowered the ocean | 〃 |
| **Bragi** | Title role: most Steam achievements | Sings the great deeds of heroes in Valhalla | 〃 (plus the Steam key) |

Title roles go to whoever leads that `/valheim top` board at the weekly check
([details](#title-roles)).

**All of it at once**, in the `admin_bot` block of `config.json` (plus `"username":
"Huginn"` in the `discord` block):
```json
"online_role": true,
"owner_role": true,
"titles": { "enabled": true }
```
Then `docker compose up -d --force-recreate`. The bot creates the roles itself the first
time it hands each one out. None of them grant any permissions.

**What it needs:**
- **Manage Roles** permission, and `guild_id` set.
- The stats database, for everything except Odin.
- Players have to run `/valheim link <character>` once to get "In Valheim" or a title.

**Role order** (Server Settings → Roles, top to bottom):
1. **Muninn**, the bot's own role (it has the bot icon). It must stay **above** every role
   it hands out.
2. **Odin**. The bot moves it here when it creates it.
3. **Heimdall, Hel, Sleipnir, Thor, Bragi, In Valheim**, in any order.

Roles the bot creates start at the bottom of the list, so they're below its own role and it
can hand them out. If you drag one above Muninn, the log says `can't give the … role`.

**Renaming and recolouring:**
- The bot remembers its roles by ID, so you can rename, recolour or reorder them in
  Discord. The exception: the title posts and `/valheim titles` use the names above.
- **Display role members separately** is on for the Odin and In Valheim roles when the bot
  creates them. Turn it on for a title role to list its holder near the top of the member
  list too.
- **Renamed the bot?** Its own role keeps the name it had when it was invited. Rename that
  role by hand: Server Settings → Roles → the role with the bot icon.

### Testing it

1. **Can the bot read the lists?** In Discord, run `/valheim lists`. You should get a private
   reply showing `permittedlist.txt`, `bannedlist.txt` and `adminlist.txt`.
2. **Fake a refused join.** Append a made-up log line; the ID is fake, so no real player is
   affected:
   ```bash
   echo "$(date '+%m/%d/%Y %H:%M:%S'): Player Test Viking : V_76561190000000001 is blacklisted or not in whitelist." \
     | sudo tee -a /home/valheim/logs/valheim_console.log
   ```
   Within one poll interval (15 s), a "🚫 Join attempt refused" notice with Permit / Ban /
   Ignore buttons appears in the admin channel.
3. **Try the buttons.** Click **Permit** or **Ban**. The notice updates with the result
   and who clicked, then check the file:
   ```bash
   sudo cat /home/valheim/valheim_save_data/permittedlist.txt
   ```
   Undo it with `/valheim unpermit player_id:V_76561190000000001` (or `/valheim unban …`).
   Use a different fake ID for each test, because of the 10-minute cooldown.
4. **For real.** Have someone who isn't on the list try to join, click **Permit**, and have
   them try again.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bot offline; no `admin_bot` line in the log, and the `Monitoring …` line doesn't end in `; admin bot on` | The monitor didn't see `admin_bot.enabled: true`: a config edit made after the container started, or the old code | `git pull`, then `docker compose up -d --build --force-recreate` |
| `Expecting property name enclosed in double quotes` (or other JSON errors) | Usually a comma after the last item in a block, e.g. `},` right before the final `}` | Remove that comma; check with `python3 -m json.tool config.json` |
| `admin_bot stopped: Improper token has been passed.` | The token Discord got is wrong: an old token after a reset, the Client Secret instead of the bot token, or placeholder text left in `.env` | See the length check below |
| `discord.py isn't installed` | The image wasn't rebuilt | `docker compose up -d --build` |
| `admin_bot disabled: …` | A required setting is missing; the message names it | Add it to the `admin_bot` block |
| `/valheim` commands don't show up | `guild_id` isn't set (global commands take up to an hour), or the bot was invited without `applications.commands` | Set `guild_id`, or re-invite the bot with both scopes |
| "Only the server admins can do that." | Your Discord user ID isn't in `admin_user_ids`, and you have none of the `admin_role_ids` roles | Add your ID and recreate the container |
| Status channel never changes; log says `no permission to rename the status channel` | The bot lacks **Manage Channels** on that channel | Add it in the channel's permissions (the bot can't rename a channel it can't manage) |
| Status channel lags behind | Discord's limit of 2 renames per 10 minutes | Expected: it catches up within 5 minutes |
| Status board never appears; log says `no permission to post in the status board channel` | Missing channel permissions | Give the bot View Channel, Send Messages, Embed Links and Read Message History there |
| `backups.dest_dir … doesn't exist` | The backup folder isn't mounted | Add the volume in `docker-compose.override.yml` and create the folder on the host |
| `The restart request wasn't picked up` | The host helper isn't installed or running | `systemctl status valheim-bot-request.path`; see [host/README.md](host/README.md) |
| Raids, summaries etc. don't post | Their names aren't in `events` | Add them (see [Extras](#extras-raids-summaries-milestones-recap-board-backups)) and recreate the container |
| "Couldn't edit the list files" | `save_dir` doesn't point at the mounted save dir | Check the volume in `docker-compose.yml` and `save_dir` in `config.json` |
| `PyNaCl is not installed, voice will NOT be supported` | Harmless | Nothing: the bot doesn't use voice |
| New commands missing after an update | Discord caches the command list | Press Ctrl+R in Discord. If they're still missing, check the log for `admin_bot stopped` |
| `/valheim join` says the join code isn't known | Nobody has joined since the server's last restart, so the new code isn't in the log yet | It appears at the next join. Meanwhile, players can use the address, or ask someone in-game (pause menu) |
| World setting: "the settings file didn't change within 20 s" | The host helper isn't updated, or `/valheim_home` isn't mounted | Re-run the two `install` commands in [host/README.md](host/README.md#one-time-setup); check the mount with `docker compose config` |
| `can't give the Hel title role` (or another title) | The bot lacks Manage Roles, or a title role was moved above the bot's role | Give it Manage Roles and keep the bot's role above the title roles |
| `/valheim titles` says title roles are off | `titles.enabled` isn't true, or `guild_id` / the stats database is missing (the log says which) | Fix the config and recreate the container |
| Stat channels don't appear; log says `can't create the stat channels` | The bot lacks Manage Channels | Give its role Manage Channels (Server Settings → Roles) |
| A stat channel shows an old value | Discord's limit of 2 renames per channel per 10 minutes | Expected: it catches up within 5 minutes |
| `can't give the owner role` | The bot lacks Manage Roles, or the Odin role was moved above the bot's role | Give it Manage Roles and keep the bot's role above Odin |
| `can't change the In-Valheim role` | The bot lacks Manage Roles, or its role is below the "In Valheim" role | Give it Manage Roles and drag the bot's role above that role in Server Settings → Roles |
| Notification DMs don't arrive | The user doesn't accept DMs from server members | In Discord: the server name → Privacy Settings → allow direct messages |
| `/valheim map` can't read the seed | The world file isn't in `<save_dir>/worlds_local`, or its format changed | Check `save_dir` (the monitor looks for `<world>/_main.*.fwl2` and `*.fwl`), or set `admin_bot.map.seed` |
| No backups copied | `source_dir` isn't Valheim's `worlds_local`, or `dest_dir` isn't mounted | Check both, then see what the log says after `Backup created` (`Backups: copied N backup(s)`) |
| No achievement posts | `"achievement"` isn't in `events`; the player hasn't joined since the key was added (that's when their Steam ID is learned); their first check only records what they have; or their profile or game details are private | Check `events` and `docker compose logs \| grep -i steam`; `/valheim stats <player>` shows whether it's private |
| `Steam rejected the API key` | The key in `.env` is wrong, revoked or rotated | Get a new one at <https://steamcommunity.com/dev/apikey>, set `STEAM_API_KEY` in `.env`, recreate |
| Times in the log are off by hours | The container runs in UTC | Set `TZ=America/Los_Angeles` (etc.) in `.env` and recreate |
| `No Discord webhook URL configured` | Neither `.env` nor `config.json` has a webhook URL, or `.env` wasn't loaded | Set `DISCORD_WEBHOOK_URL` in `.env` (next to `docker-compose.yml`) and recreate the container |
| Posts stopped after replacing the webhook; the log shows `Discord HTTP 404` | The old webhook URL is still in use | Put the new URL in `.env` as `DISCORD_WEBHOOK_URL` and recreate the container (`.env` wins over `config.json`) |

To check which token the container actually has without printing it:
```bash
docker compose exec valheim-discord-monitor sh -c 'printf "%s" "$DISCORD_BOT_TOKEN" | awk "{print length(\$0), \"chars,\", gsub(/\\./,\".\"), \"dots\"}"'
```
A bot token is about 70 characters with 2 dots. `0 chars` means `.env` wasn't loaded.
`.env` must sit next to `docker-compose.yml`, and the container must be recreated after
changing it. If `DISCORD_BOT_TOKEN` is empty, the monitor falls back to `admin_bot.token` in
`config.json`.

## Extras: raids, summaries, milestones, recap, board, backups

All of these come from lines vanilla Valheim already writes to its log; no mods. Most are
switched on by adding their name to `events` in `config.json`:

```json
"events": ["login", "logout", "death", "server_restart", "server_online", "server_offline",
           "raid", "version_mismatch", "session_summary", "welcome", "milestone", "weekly_recap"]
```

| Event | Posts | From the log line |
|---|---|---|
| `raid` | ⚔️ "**Raid in Alheim!** Moder's army (drakes) is attacking." | `Random event set:army_moder` |
| `version_mismatch` | ⚠️ "Someone tried to join with a **newer** version of Valheim: the server needs an update." (or *older*: they need to update). At most once an hour. | `Network version check, their:41, mine:40` |
| `session_summary` | Replaces the plain leave message: "**Ingrid** left Alheim after 2h 14m and died 3 times." | the player's join and leave |
| `welcome` | Replaces the join message on someone's **first ever** visit: "🎉 **Ingrid** arrived for the first time. Welcome, viking!" | stats database |
| `milestone` | 🏆 "**Ingrid** has now spent **50 hours** in Alheim!" at 10/25/50/100/250/500/1000 hours, and at the same numbers of deaths | stats database |
| `weekly_recap` | 📜 A weekly embed: top players by time, most deaths, raids, new vikings, total hours, peak online, and exploration ("12 new areas discovered: 3 sunken crypts, 1 fuling village") | stats database, `Placed location` lines |
| `update` | ✅ "Valheim updated: l-1.0.16 → l-1.0.17" when the server comes back on a new version, plus the [auto-updater](#auto-updates-and-restarts-from-discord)'s "update available" and "installing" posts | `Valheim version:` at boot |

`welcome`, `milestone` and `weekly_recap` need the stats database (`database.path`).

**Session summaries and milestones only count sessions the monitor saw start.** Someone who
was already online when the monitor started gets a plain leave message.

**Weekly recap timing:**
```json
"weekly_recap": { "day": "sunday", "hour": 18 }
```
- It posts once per week, at or after that hour in the container's time zone (`TZ` in `.env`).
- A restart doesn't post it twice. A missed day is skipped rather than posted late.
- Quiet weeks with nobody playing post nothing.

### Status board

The admin bot keeps **one message** in a channel up to date:
- a title: 🟢 *N online* / 🟢 *empty* / 🔴 *offline*;
- who's on, with "joined 25 minutes ago";
- the current join code, when the server came up, its version, the last world save (and how
  long it took), free disk space, and the last backup and raid.

Times use Discord's own relative timestamps, so they stay current by themselves. The bot
only edits the message when something changes.

**At start-up** the board fills in the version, and when the server last booted,
saved, backed up and was raided, all read from the existing log. Who's online shows once
the count is known: at the next join or leave, or the server's next count line within 10
minutes. Until then the title is ⚪.

1. Make a text channel (e.g. `#server-status`), ideally read-only for everyone.
2. Give the bot **View Channel**, **Send Messages**, **Embed Links** and **Read Message
   History** there.
3. Add to the `admin_bot` block:
   ```json
   "status_board": { "channel_id": "123456789012345678" }
   ```
4. Recreate the container. The bot posts the message once (log: `status board posted in
   #…`) and remembers it in `status_board.json`. Delete the message and it posts a fresh
   one. **After moving the board to another channel,** delete the old message; the new
   channel gets a fresh one.

It works alongside the [status voice channel](#status-voice-channel): the channel name is
the glanceable version, and the board has the details.

**Commands:**
- **`/valheim join`** (anyone): how to connect, shown only to the person who asked:
  - the current **join code**, which works on every platform and changes whenever the server
    restarts;
  - the server's **address** for PC;
  - the **password**, if one is set, hidden behind a spoiler;
  - the in-game steps (Join Game → Add server);
  - if you use a permitted list, a note that a refused player should ask an admin, who
    then gets the Permit button.
- **`/valheim online`** (anyone): who's on right now, and since when.
- **`/valheim backups`** (admins): the newest copied world backups, with sizes and ages.

### World backup copies

Valheim already backs up your world by itself (`Backup created in Alheim_backup_auto-…` in
the log). On Valheim 1.0 each backup is a **folder** in `worlds_local/`, e.g.
`Alheim_backup_auto-20260928-170645/`, because worlds are saved as chunk files. Older
servers made `.db` + `.fwl` file pairs instead. Both kinds are copied. The monitor can copy those backups to **another
disk**, so a failure of the server's disk doesn't take the backups with it:

1. Mount a folder on the other disk in `docker-compose.override.yml`, e.g.
   `- /mnt/backups/valheim:/backups` (see the [quick start](#self-hosted-linux-server-quick-start)).
   The folder must exist; the monitor won't create it.
2. Add:
   ```json
   "backups": {
     "source_dir": "/valheim_save_data/worlds_local",
     "dest_dir": "/backups",
     "keep": 30,
     "alert_after_hours": 48
   }
   ```

What happens:
- **When:** after every `Backup created` line, and once at start-up to catch up. Valheim
  writes a backup once and never touches it again, so a copy is never half-written.
- **What:** the newest `keep` backups. Older copies are deleted. The live world (the
  `Alheim` folder, or `Alheim.db`) is never copied.
- **Alert:** if Valheim hasn't made a backup in `alert_after_hours` while the server is up,
  the bot posts a warning to the admin channel, once.

How often Valheim makes backups, and how many it keeps, is set with the server's
`-backups`, `-backupshort` and `-backuplong` launch options.

### Server health

Every world save logs how long it took and how much disk space is left. The monitor warns
the admin channel, at most once a day per warning:
- **Low disk:** when free space on the save disk drops below `health.low_disk_gb` (10 GB).
- **Critical disk:** close to the point where Valheim **stops saving the world**.
- **Slow saves:** a save that took longer than `health.slow_save_seconds` (5 s). Players
  feel that as a freeze.

The status board shows **Disk free** and how long the last save took.

```json
"health": { "low_disk_gb": 10, "slow_save_seconds": 5 }
```

**Daily restart (optional).** With the [host helper](host/README.md) installed, the
monitor can restart the server once a day while nobody's on, which also installs any
waiting update:
```json
"daily_restart": { "time": "05:00", "window_minutes": 120 }
```
- It restarts at the first moment in the window when the server is empty.
- If people play right through the window, that day is skipped.
- Times are in the container's time zone (`TZ`).
- The admin channel is told each time.

### Auto-updates and restarts from Discord

For self-hosted servers with a cron-driven update script (`check_update.sh`, which
restarts when nobody is on; the unit's `ExecStartPre` runs `steamcmd app_update`). A
small helper on the host lets the monitor:

- **post the updater's decisions:** "🆕 update available: installs once everyone has
  left", "🔄 installing now", and checker errors to the admin channel;
- **give the updater a reliable player count:** it writes `status.json`, because the
  script's own guess from the console log reads 0 right after a log rotation;
- **take admin commands:**
  - `/valheim update-check` checks for an update now;
  - `/valheim restart [minutes] [reason]` restarts after a warning countdown in the public
    channel, early if everyone leaves, and installs any waiting update;
  - `/valheim restart-cancel` stops a countdown.

The container gets no host privileges. It drops a request file into a shared folder, and a
systemd path unit on the host runs a small handler as the `valheim` user. **Setup,
including the change to `check_update.sh`: [host/README.md](host/README.md).**

**World settings from Discord.** The same link lets admins change the world's preset,
modifiers and setkeys:
- `/valheim settings` shows what's set and every allowed value.
- `/valheim modifier raids more`, `/valheim preset hard` and `/valheim setkey passivemobs on`
  make a change.

Changes apply at the next restart; the bot offers a "Restart in 5 min" button.

The service file isn't edited by the bot. You change it once to read a `valheim`-owned
`world-settings.env`, and the host side only ever writes known values into that file. See
[host/README.md → World settings](host/README.md#world-settings-from-discord-preset-modifiers-setkeys).

## Player stats & public web page

The monitor can record every login, logout and death into a small **SQLite**
database (`sqlite3`, built into Python — no server, one file) and regenerate a
self-contained public web page of leaderboards from it: most play time, most
deaths, longest session, most visits, plus server totals (total hours, unique
players, peak players online at once). Enable it with `database` and
`stats_site` blocks in `config.json`:

```json
"database": { "path": "valheim_stats.db", "enabled": true },
"stats_site": { "output": "site/index.html", "render_interval_seconds": 60 }
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
python3 stats_site.py --db valheim_stats.db --out site/index.html --config config.json
```

The page is static HTML — no scripts, no inputs, no auth needed — so it is safe to
host publicly. **Full deployment (DNS, nginx/Caddy, backfill): see
[DEPLOY_STATS.md](DEPLOY_STATS.md).**

The page is laid out as three tabs (client-side, no server round-trip): **Server**
(totals + recent activity), **Vikings** (the play/death/longest/visits
leaderboards), and **Achievements** (below). The active tab is remembered in the
URL hash so a refresh keeps you where you were.

## Steam achievements

### Steam achievements in Discord

With a Steam Web API key, the bot brings your players' Valheim achievements into Discord.
You don't need the web page for this.

- **Unlock posts.** When a player unlocks an achievement, Huginn posts "🏅 Ingrid unlocked
  Elder slayer" with the achievement's icon, what it's for, and their total (e.g. 23/45).
  Several at once become one post. Add `"achievement"` to `events` to turn this on.
- **`/valheim stats`** shows the player's achievement count and latest unlock, or says
  that their Steam profile is private.
- **`/valheim top achievements`**: the "Most achievements (Steam)" leaderboard.
- **Bragi** title role for the leader, with the [other titles](#title-roles).

**Setup:**
1. Get a free key at <https://steamcommunity.com/dev/apikey>. Any domain name works for
   the form.
2. Put it in `.env` as `STEAM_API_KEY=…`, not in `config.json`. That's enough to turn the
   Steam refresh on.
3. Add `"achievement"` to `events` in `config.json`, then run
   `docker compose up -d --force-recreate`.

**Good to know:**
- Achievements are checked every 30 minutes (`steam.refresh_seconds`), so a post can
  arrive up to half an hour after the unlock.
- The first check for a player only records what they already have, so nobody gets a
  flood of old achievements. Only achievements unlocked after that are posted.
- A Steam account is shown under the character it played most recently.
- **Steam only.** Xbox and Game Pass players have no Steam ID. Microsoft has no free public
  achievements API, so Xbox support would need a third-party service such as OpenXBL.
  PlayStation has no public trophy API, and Nintendo consoles have no achievements at all.
- Players need a **public Steam profile with public game details**. Otherwise
  `/valheim stats` says it's private.

### Achievements on the stats web page

For players who connect through **Steam** (not Xbox/GamePass), the monitor can
show their public Valheim achievement progress on the page — an Achievements tab
with a card per player (avatar, unlocked/total, a progress bar, their latest
unlock) plus a "Recent Unlocks" feed across everyone.

How the link is made: nothing to configure per player. When a Steam player
connects, the server log carries a handshake line
(`PlayFab socket … received local Platform ID V_7656…`, or `Steam_7656…` before 1.0) that the monitor
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

**Docker Compose**: see the [self-hosted quick start](#self-hosted-linux-server-quick-start).
`docker-compose.yml` bind-mounts the repo, so `config.json`, `monitor_state.json` and the
stats database live next to the code and survive rebuilds. If `docker compose` rejects
`env_file` / `required`, your Compose is older than 2.24; delete the `env_file:` block and
pass secrets another way.

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
| `events` | mode default | Log mode: `login`, `logout`, `death`, `respawn`, `server_up`, `join_refused`, and the [extras](#extras-raids-summaries-milestones-recap-board-backups) `raid`, `version_mismatch`, `session_summary`, `welcome`, `milestone`, `weekly_recap`, `update`, `achievement` (Steam unlock posts). Count mode: `player_joined`, `player_left`, `server_online`, `server_offline`. |
| `poll_interval_seconds` | 15 | How often to poll. |
| `discord.username` | Valheim | Name the webhook posts under (the examples use **Huginn**, see [roles & names](#roles--names)). |
| `source.offline_after` | 3 | Count mode: failed queries in a row before "offline". |
| `source.api_key` | — | `steamapi` only; or `STEAM_API_KEY` env var. |
| `steam.enabled` | on when a key is set | Fetch public Steam achievements (for Discord and the page). `false` turns it off even with a key. |
| `steam.api_key` | — | Steam Web API key; better as the `STEAM_API_KEY` env var in `.env`. |
| `steam.refresh_seconds` | 1800 | How often to check Steam, so unlock posts can lag an unlock by up to this long. |
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
| `admin_bot.token` | — | Discord bot token. Prefer the `DISCORD_BOT_TOKEN` env var (`.env`), which takes precedence. |
| `admin_bot.guild_id` / `channel_id` | — | Your Discord server, and the admin channel for notices. |
| `admin_bot.admin_user_ids` / `admin_role_ids` | — | Who may press the buttons and use `/valheim`. |
| `admin_bot.save_dir` | — | Folder holding `permittedlist.txt` / `bannedlist.txt` (must be writable). |
| `admin_bot.repeat_cooldown_seconds` | 600 | One notice per player per this many seconds. |
| `admin_bot.status_channel.channel_id` | — | Voice channel whose name shows the server status. |
| `admin_bot.status_channel.online` / `empty` / `offline` | see above | Name templates; `{count}`, `{server}`. |
| `admin_bot.status_channel.min_interval_seconds` | 300 | Minimum time between renames (can't go below 300 because of Discord's limit). |
| `admin_bot.status_channel.stale_after_seconds` | 900 | Show offline if the log has been silent this long. |
| `admin_bot.status_board.channel_id` | — | Text channel for the live status board message. |
| `admin_bot.status_board.state_file` | `status_board.json` | Where the board's message id is remembered. |
| `admin_bot.join.address` | — | Shown by `/valheim join` for PC players, e.g. `203.0.113.7:2456` or `valheim.example.com:2456`. Without it, the IP the server logs is used, when it logs one. |
| `admin_bot.join.password` | — | Shown by `/valheim join` behind a spoiler. Leave out for a passwordless server. |
| `admin_bot.join.note` | — | Extra text for `/valheim join`, e.g. "Ask in #general for an invite". |
| `backups.source_dir` / `dest_dir` | — | Valheim's `worlds_local` folder, and where to copy its backups (must exist). |
| `backups.keep` | 30 | Backup copies to keep. |
| `backups.alert_after_hours` | 48 | Warn the admin channel if Valheim makes no backup for this long. |
| `weekly_recap.day` / `hour` | sunday / 18 | When to post the weekly recap (container time zone). |
| `updater.log` | — | The host updater's log (`update_check.log`), for update posts. |
| `updater.bot_dir` | — | Folder shared with the host: `status.json` out, `request` for restart/check/settings. |
| `admin_bot.online_role` | — | `true` (or a role name): the bot creates the "In Valheim" role, given to linked players while they're in-game. |
| `admin_bot.online_role_id` | — | Use this existing role instead (takes precedence over `online_role`). |
| `admin_bot.stat_channels.enabled` | false | Locked voice channels showing the server's numbers ([stat channels](#stat-channels)). |
| `admin_bot.stat_channels.show` | all | Which ones, in order ([keys](#stat-channels)); `titles` = all the title channels. |
| `admin_bot.stat_channels.layout` | split | `split`: three categories (live, this week, titles). `single`: one. |
| `admin_bot.stat_channels.categories` | — | Category names for `split`: `{"watch": …, "saga": …, "hall": …}`. |
| `admin_bot.stat_channels.category` | 📊 Valheim | Category name for `single`. |
| `admin_bot.owner_role` | — | `true` (or a role name): an "Odin" role for the Discord server's owner. |
| `admin_bot.lfg.reminder_minutes` | 15 | Ping game-night signups this long before the start. |
| `admin_bot.lfg.discord_event` | false | Also create a Discord Event for each plan (needs Manage Events). |
| `admin_bot.map.enabled` | true | Allow `/valheim map` (seed + map link). |
| `admin_bot.map.seed` | "" | Use this seed instead of reading it from the world file. |
| `admin_bot.titles.enabled` | false | Weekly title roles for the `/valheim top` leaders. |
| `admin_bot.titles.period` | all | `all` (all-time numbers) or `week` (last 7 days). |
| `admin_bot.titles.day` / `hour` | sunday / 18 | When to reassign the titles (container time zone). |
| `admin_bot.titles.channel_id` | — | Where to post title changes; empty = the webhook channel. |
| `admin_bot.titles.roles` | — | Existing role IDs per category (`time`, `deaths`, `sessions`, `longest`, `achievements`); otherwise the bot creates them. |
| `health.low_disk_gb` | 10 | Warn when the save disk has less free space than this. |
| `health.slow_save_seconds` | 5 | Warn when a world save takes longer than this. |
| `daily_restart.time` / `window_minutes` | — / 120 | Restart once a day in this window while nobody's on (needs the host helper). |
| `admin_bot.world_settings.file` | `/valheim_home/world-settings.env` | The host's world settings file, as mounted in the container (read-only). |
| `discord.embeds` | true | Coloured embed vs plain text. |
| `discord.show_player_count` | true | Footer with the current online count. |
| `discord.messages` | see example | Per-event templates; `{player}`, `{server}`, `{who}`, `{count}`, `{max}` placeholders. |
| `state_file` | `monitor_state.json` | Where the read offset is remembered. |

## Notes
- Names come from the character, not the Steam account.
- On a PlayFab/crossplay server a disconnect (clean or timeout) shows up as the
  `Destroying abandoned … owner <id>` line; the monitor emits one logout per player.
- If the server restarts, it starts a fresh log and the monitor starts over with it
  automatically.
- `docker stop` / `docker compose down` stop the monitor straight away (it handles SIGTERM).
- Player names in Discord messages can't ping anyone: mentions are disabled on every post,
  and markdown in names is escaped.
- Player IDs: since Valheim 1.0 the lists use `V_<SteamID64>` for Steam and `X_` / `S_` /
  `N_` for Xbox, PlayStation and Nintendo. Older lists may still contain
  `Steam_…` / `Xbox_…` / `PlayStation_…` / `Nintendo_…` entries. The admin bot leaves
  those untouched.

## Server admin tips

Things that come up running a vanilla dedicated server, learned setting this one up.

**The list files** live in the save dir (`-savedir`, e.g. `/home/valheim/valheim_save_data`):
- `adminlist.txt`: players who can use admin console commands.
- `bannedlist.txt`: players who can't join.
- `permittedlist.txt`: if it has any entries, *only* these players can join.

Put one ID per line in the `V_<SteamID64>` form (Valheim 1.0+). To find your SteamID64:
Steam → your name → **Account details**, or the number at the end of your profile URL.
After editing `adminlist.txt`, restart the server. The admin bot's `/valheim lists` shows
all three files.

**Looking at them** needs `sudo`, because the folder belongs to the `valheim` user. Wrap
wildcards so root expands them:
`sudo sh -c 'ls -la /home/valheim/valheim_save_data/*list.txt'`.

**The in-game console** (for `kick`, `ban`, `unban`):
1. Add `-console` to Valheim's launch options in Steam (Properties → General → Launch
   Options).
2. Press **F5**. On keyboards whose F-keys default to media keys, that's **Fn + F5**, or
   toggle Fn-lock (often **Fn + Esc**).
3. Commands need your ID in `adminlist.txt`. Refer to players by their **platform ID**
   (`kick V_7656…`), not their character name. Your players' IDs are in
   `permittedlist.txt`. The admin bot's notices show the IDs of refused players.

**Idle players:** vanilla Valheim logs nothing per player between joining and leaving, so
idleness can't be detected from the log, and there's no remote kick. Auto-kicking AFK
players needs a server-side mod.

## Development

```bash
pip install -r requirements.txt                   # discord.py, only needed for the admin bot
python -m unittest discover -s tests -v           # unit tests
python valheim_discord_monitor.py --config config.example.json --replay sample_console.log   # parser dry run
```

- **Tests** (`tests/`) cover the log parser, the list-file editing, the status channel's
  naming and rate limiting, the extras (live state, board, milestones, recap, backups),
  and the audit fixes. They run offline, with no Discord or
  Valheim needed.
- **CI:** `.github/workflows/tests.yml` runs the tests on Python 3.9 and 3.12, does the
  replay, and builds the Docker image, on every push to `main` and every pull request.
- **Sample log:** `sample_console.log` is a short example log with crossplay joins, a
  death, a timeout, a disconnect and a refused join. Add lines there when teaching the
  parser something new.

## About this fork

This is a fork of
[justin7jones/valheim-discord-monitor](https://github.com/justin7jones/valheim-discord-monitor).
Added here:
- **Admin bot:** refused-join alerts with Permit / Ban / Ignore buttons, `/valheim`
  commands, and support for Valheim 1.0's `V_…` player IDs.
- **Status voice channel**, bot-created **stat channels**, and a live **status board**,
  filled in from the existing log at start-up.
- **`/valheim join`:** the current join code (tracked across restarts), address and how
  to connect.
- **Extras:** raid alerts, version-mismatch alerts, session summaries, first-visit
  welcomes, milestones, a weekly recap, `/valheim online`, and copies of Valheim's world
  backups to another disk (including Valheim 1.0's backup folders).
- **Auto-update integration:** update posts, a reliable player count for the host's
  update script, and `/valheim restart` with a countdown (`host/`).
- **World settings from Discord:** preset, modifiers and setkeys, validated on the host.
- **Community:** `/valheim stats`, `top`, `notify` (DMs), `link` with an "In Valheim" role,
  `request-access`, `plan` (game nights with RSVPs and reminders), `map` (reads the
  seed from Valheim 1.0 world folders), weekly title roles (`titles`), and the
  bot-created "In Valheim" and Odin (owner) roles.
- **Steam achievements in Discord:** unlock posts, achievements in `/valheim stats`, a
  "Most achievements" leaderboard, and the Bragi title.
- **Server health:** low-disk and slow-save warnings, an optional daily restart, and
  exploration in the weekly recap.
- **Docker setup:** `Dockerfile`, `docker-compose.yml`, `.env.example` and a self-hosted
  example config.
- **Docs:** the self-hosted quick start, bot setup and troubleshooting, and these tips.
- **Fixes from a code audit:**
  - player names can't ping `@everyone`;
  - a restarted server's new log is always read from the top;
  - `docker stop` is clean;
  - one-off commands no longer cut short live stats sessions;
  - Steam and LOW.MS maintenance fixes.
- **Tests and CI.**

## License

MIT — see [LICENSE](LICENSE).
