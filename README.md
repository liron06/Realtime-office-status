# Realtime Discord bot

An asynchronous `discord.py` bot for the Realtime student association. The office-status buttons are implemented, and Minecraft and Congressus have clean extension points without performing external operations yet.

## Project structure

```text
bot.py                  Bot startup, cog loading, command sync, error handling
config.py               Environment loading and validation
cogs/                   Discord commands, views, and events
services/               Future async external integration boundaries
utils/permissions.py    Reusable administrator/role-ID checks
```

## Setup

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Invite the Discord bot with the `bot` and `applications.commands` scopes. Give it permission to view/send messages and read message history in the control channel, and to manage the office voice channel. Enable Discord Developer Mode to copy IDs.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | yes | Discord bot token |
| `DISCORD_GUILD_ID` | no | Sync commands immediately to one guild; omit for global commands |
| `OFFICE_VOICE_CHANNEL_ID` | yes | Voice channel renamed to show office status |
| `OFFICE_CONTROL_CHANNEL_ID` | yes | Text channel where office buttons are posted |
| `OFFICE_MANAGER_ROLE_ID` | yes | Role allowed to use office buttons |
| `MINECRAFT_MANAGER_ROLE_ID` | yes | Role allowed to use Minecraft management commands |
| `MINECRAFT_ROLE_ID` | no | Future validated-player role; grants no management access |

Congressus and Minecraft/RCON secrets are intentionally not required or consumed yet. Suggested future names are documented in `.env.example`; define their exact configuration only when those integrations are designed. `.env` is excluded by `.gitignore`.

## Permissions

Members with `OFFICE_MANAGER_ROLE_ID` may use the office buttons. Members with `MINECRAFT_MANAGER_ROLE_ID` may use `/minecraft start`, `stop`, `restart`, `whitelist`, and `commands`. Discord Administrators always pass both checks.

`/minecraft validate`, `status`, and `players` are available to all server members. `MINECRAFT_ROLE_ID` is deliberately not consulted by either management check. Button authorization is checked in the callback itself; channel visibility is not treated as security.

## Run locally

```bash
source .venv/bin/activate
python bot.py
```

The bot loads all cogs, registers persistent office-button handlers, reuses the existing Office panel when it finds one in the 100 most recent control-channel messages, and synchronizes slash commands. `DISCORD_GUILD_ID` makes command changes appear quickly in that guild; global propagation can take longer.

## Run with the existing systemd service

The entry point remains `bot.py`. Ensure the service working directory is this repository and its environment includes the new role IDs. A typical unit contains:

```ini
WorkingDirectory=/path/to/realtime-office-bot
ExecStart=/path/to/realtime-office-bot/.venv/bin/python /path/to/realtime-office-bot/bot.py
```

After updating the code and environment, install dependencies and restart the existing unit (replace `<service-name>`):

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl restart <service-name>
sudo systemctl status <service-name>
journalctl -u <service-name> -f
```

If the unit uses `EnvironmentFile=`, add the variables there instead of relying on `.env`. No unit file is present in this repository; inspect the deployed unit with `systemctl cat <service-name>` before editing it.

## Add another module

1. Add a cog under `cogs/` with an asynchronous `setup(bot)` function.
2. Keep Discord commands and interaction presentation in the cog.
3. Put external API or server behavior in an asynchronous class under `services/`.
4. Put reusable ID-based checks in `utils/permissions.py` and configuration in `config.py`.
5. Add the cog import path to `EXTENSIONS` in `bot.py`.

Do not use blocking HTTP, subprocess, or sleep calls in event handlers. Use async clients and `asyncio`-compatible subprocesses when integrations are added.
