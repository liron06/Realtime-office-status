# Realtime Discord bot

An asynchronous `discord.py` bot for the Realtime student association. It provides office-status controls, persistent Congressus membership and Minecraft-account registration, and placeholders for future Minecraft server operations.

## Project structure

```text
bot.py                  Bot startup, cog loading, command sync, error handling
config.py               Environment loading and validation
cogs/                   Discord commands, views, and events
services/               Async integration and persistence boundaries
  database.py           SQLite member/account persistence
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

Invite the Discord bot with the `bot` and `applications.commands` scopes. Give it permission to view/send messages and read message history in the control channel, manage the office voice channel, and manage roles. The bot's highest role must be above `MINECRAFT_ROLE_ID`. Enable Discord Developer Mode to copy IDs.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | yes | Discord bot token |
| `DISCORD_GUILD_ID` | yes | Guild where commands are synced and validated roles are assigned |
| `OFFICE_VOICE_CHANNEL_ID` | yes | Voice channel renamed to show office status |
| `OFFICE_CONTROL_CHANNEL_ID` | yes | Text channel where office buttons are posted |
| `OFFICE_MANAGER_ROLE_ID` | yes | Role allowed to use office buttons |
| `BOARD_ROLE_ID` | yes | Board role allowed to manage Office and Minecraft |
| `MINECRAFT_MANAGER_ROLE_ID` | yes | Role allowed to use Minecraft management commands |
| `MINECRAFT_ROLE_ID` | yes | Role assigned after active Congressus membership validation; grants no management access |
| `CONGRESSUS_CLIENT_ID` | yes | Congressus OAuth client identifier |
| `CONGRESSUS_CLIENT_SECRET` | yes | Congressus OAuth client secret |
| `CONGRESSUS_BASE_URL` | yes | Congressus base URL, normally `https://www.sv-realtime.nl` |
| `CONGRESSUS_REDIRECT_URI` | yes | Public OAuth callback URL registered with Congressus |
| `DATABASE_PATH` | no | SQLite path; defaults to `data/realtime.db` |

The callback server listens only on `127.0.0.1:8090`. The public `CONGRESSUS_REDIRECT_URI` must therefore be forwarded by the existing HTTPS reverse proxy to `http://127.0.0.1:8090/congressus/callback`. Configure the proxy not to log callback query strings because they contain short-lived OAuth credentials. `.env` is excluded by `.gitignore`.

## Permissions

Members with `BOARD_ROLE_ID` may manage both Office and Minecraft. `OFFICE_MANAGER_ROLE_ID` remains an Office-only role for backwards compatibility. `MINECRAFT_MANAGER_ROLE_ID` is Minecraft-only. Discord Administrators always pass every management check.

`/minecraft validate` is available in the configured guild and assigns `MINECRAFT_ROLE_ID` only after Congressus reports an active membership and the account link is stored. `/minecraft register` additionally requires that persisted validation and the role. `status` and `players` remain available to all server members. `MINECRAFT_ROLE_ID` is deliberately not consulted by any management check. Button authorization is checked in the callback itself; channel visibility is not treated as security.

## Minecraft account commands

- `/minecraft register <username>` stores the validated member's first Minecraft Java username. Members cannot replace it themselves.
- `/minecraft whitelist show <member>` shows the stored account link to Minecraft managers.
- `/minecraft whitelist reset <member>` clears only the Minecraft username so the member can register again.
- `/minecraft whitelist set <member> <username>` sets or replaces a validated member's username.

Usernames must be 3–16 ASCII letters, numbers, or underscores and are unique without regard to letter casing. These commands only store intended account links; they do not contact or modify a Minecraft server.

## SQLite storage

The database and parent directory are created automatically at startup. The `minecraft_members` table stores the Discord and Congressus identifiers, optional Congressus display fields, the optional Minecraft username, and verification/registration timestamps. Discord IDs, Congressus IDs, and Minecraft usernames are unique; Minecraft username uniqueness is case-insensitive. Existing database files are never reset automatically.

## Run locally

```bash
source .venv/bin/activate
python bot.py
```

The bot initializes SQLite, loads all cogs, starts the local Congressus callback server, registers persistent office-button handlers, reuses the existing Office panel when it finds one in the 100 most recent control-channel messages, and synchronizes slash commands to `DISCORD_GUILD_ID`.

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

If the unit uses `EnvironmentFile=`, add the variables there instead of relying on `.env`. Ensure the systemd service user can write to the configured database directory and include the SQLite file in backups. No unit file is present in this repository; inspect the deployed unit with `systemctl cat <service-name>` before editing it.

## Add another module

1. Add a cog under `cogs/` with an asynchronous `setup(bot)` function.
2. Keep Discord commands and interaction presentation in the cog.
3. Put external API or server behavior in an asynchronous class under `services/`.
4. Put reusable ID-based checks in `utils/permissions.py` and configuration in `config.py`.
5. Add the cog import path to `EXTENSIONS` in `bot.py`.

Do not use blocking HTTP, subprocess, or sleep calls in event handlers. Use async clients and `asyncio`-compatible subprocesses when integrations are added.
