# Notes for Claude Code

Valheim server monitor (`valheim_discord_monitor.py`) plus an optional Discord bot (`admin_bot.py`).
No game mods: everything comes from the server log, the world save, and files players upload.

- **Before pushing:** run `python3 -m unittest discover -s tests`. It must pass, including
  `tests/test_consistency.py`.
- **For every feature, rename or removal:** go through the checklist in `CONTRIBUTING.md` in the same
  change. That covers channel topics and `OLD_TOPICS`, `COMMAND_PLACES`, stat channels, Roles & names,
  the welcome DM and guides, example configs, and every README section it touches.
- **Never commit** `config.json`, `.env` or `webhook.json`, or anything with a token or webhook URL.
- Keep the core monitor free of third-party packages. `discord.py` is only needed for the bot, and
  Pillow only for the chart.
