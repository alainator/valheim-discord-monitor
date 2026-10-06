# Contributing

Run the tests before every push:

```bash
pip install -r requirements.txt
python3 -m unittest discover -s tests
```

## The "new feature" checklist

The bot describes itself in many places: channel topics, pinned guides, stat channels, the welcome
DM, the README. When something is added, renamed or removed, **all of them change in the same pull
request**. `tests/test_consistency.py` catches most misses (CI fails), but not all, so go through this
list every time.

### A new or renamed slash command
- [ ] **Home channel:** if its reply is public (`/muninn`, `/warcouncil`), add it to `COMMAND_PLACES`
      in `admin_bot.py`.
- [ ] **Channel topic:** mention it in that channel's topic in `server_layout.TEMPLATE`. Add the
      **old** topic text to `OLD_TOPICS`, so existing servers get the new wording at the bot's next
      start.
- [ ] **README:** add a row to the command table under "Discord commands", and add it to the group
      table above that.
- [ ] **Command guides:** these are built from the command tree, so nothing to do. Check that the
      description reads well, since it's shown in the pinned guide and in Discord's `/` menu.
- [ ] **Texts that point people to it:** the welcome DM (`WELCOME_DM`), the starter rules
      (`DEFAULT_RULES`), the `/valheim progress` guide (`fch_progress.FIND_GUIDES`), and the channel
      guide (built from the topics).

### A new role (title, honor, or one the bot hands out)
- [ ] **README, Roles & names:** add it to the table and to the "Role order" list.
- [ ] **Stat channel:** if it has one holder (or a count), add a channel for it in the Hall of
      Champions (see below). Titles get one automatically, as `title_<category>`.
- [ ] **`/muninn titles`:** if it's title-like, show its holders there (`community.render_titles`).
- [ ] **Permissions:** the bot needs Manage Roles, and its role must sit above the new one. Say so
      in the feature's README section.

### A new stat channel
- [ ] `stat_channels.GROUPS` (the key and what it shows) and `stat_channels.name_for`.
- [ ] If the feature is optional, leave the channel out of the default list when the feature is
      off (`_start_tasks` in `admin_bot.py`).
- [ ] Add a row to the README's stat channel table.

### A new Huginn post (webhook)
- [ ] **Reaction:** add an emoji for it to `REACTIONS` in `admin_bot.py`, if the bot should react.
- [ ] **README:** mention it where the feed is described, and in the #huginns-watch topic if it's a
      new kind of post.

### Config, permissions, host scripts
- [ ] **New config keys:** add them to `config.example.json`, `config.selfhosted.example.json` and
      the README's Options table.
- [ ] **New Discord permission:** add it to the README's "Bot permissions" table, and update the
      permissions number in the invite link.
- [ ] **Changed `host/` script:** say in the README's "What's new" that it has to be installed again,
      and give the command.

### README, every time
- [ ] **What's new** (top of the README): one short entry.
- [ ] **Feature list and Contents:** if it's a new feature.
- [ ] **Its own section:** what it does, how to turn it on, and its options.
- [ ] **Troubleshooting:** the likely ways it goes wrong.
- [ ] **About this fork:** one line.

### Tests
- [ ] Tests for the feature itself.
- [ ] `python3 -m unittest discover -s tests` passes, including `test_consistency.py`.

## What the bot keeps in step by itself

At every start, the bot updates what `/odin setup` posted:
- **Pinned command guides:** rebuilt from the commands.
- **Channel topics:** any topic still showing an old default (one listed in `OLD_TOPICS`) gets the
  new one. Topics someone wrote by hand are left alone.
- **Channel guide** in #the-gates: rebuilt from the topics.

It never creates anything at start-up. Missing channels or guides come back with
`/odin setup apply` (or `/odin setup action:guides` for the command guides).
