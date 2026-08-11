# Bannerlord Deployer

Unofficial Discord-controlled Docker stack for hosting the
[Bannerlord Coop](https://www.moddb.com/mods/bannerlord-coop) **Console Server**
on Linux.

This project is **not affiliated with** TaleWorlds Entertainment or the
Bannerlord Coop team. It automates downloading the Console Server package from
ModDB and managing a Wine-based dedicated server container. It does **not**
redistribute Bannerlord or Coop binaries.

**License:** [PolyForm Noncommercial 1.0.0](LICENSE) (source-available, not OSI
open source). Intended for personal and community self-hosting.

**Public clone URL:** https://github.com/COFFEE-Arma/Bannerlord-Deployer.git  
Development happens on a private GitLab remote; GitHub is the public mirror.

## Requirements

- **Linux** host (Debian/Ubuntu-class). The game server uses Docker
  `network_mode: host`, which does **not** work the same on Docker Desktop for
  macOS/Windows.
- Docker Engine + Compose v2
- Disk space for a ~4 GB ModDB archive plus extract and backups (plan on roughly
  **3× archive size** free before a deploy)
- Firewall / port forwards: **4200–4201** and **7210** (TCP and UDP)
- A Discord application (bot token) and a role ID for operators

## Services

| Service | Purpose |
| --- | --- |
| `gameserver` | Runs `BannerlordCoopServer.exe` under Wine (WineHQ stable) |
| `deployer` | Discord bot + deploy pipeline; controls `gameserver` via Docker |
| `socket-proxy` | (recommended overlay) limited Docker API for the deployer |

Both game and deployer share `./data`:

```
data/
  server/       <- Console Server install
  server-data/  <- saves / CoopData (outside install; survives deploys)
  staging/      <- downloads and extraction scratch
  backups/      <- tar.gz backups before each deploy
  wineprefix/   <- persistent Wine prefix
  state.json    <- seen releases, deployed version, backup metadata
```

Set `COMPOSE_PROJECT_NAME` in `.env` (default example: `bl-coop`) so container
names are unique if you run more than one stack on a host. Keep
`gameserver_container` in `config.json` in sync with the gameserver container
name Compose creates (see below).

## Quick start (first-time install)

No existing Wine/screen install required.

### 1. Discord bot

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications) and add a **Bot**.
2. Copy the bot token into `.env` as `DISCORD_BOT_TOKEN`.
3. Invite with scopes `bot` + `applications.commands` and permissions
   **Send Messages** + **Embed Links**:
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot%20applications.commands&permissions=18432`

### 2. Configure

```bash
git clone https://github.com/COFFEE-Arma/Bannerlord-Deployer.git
cd Bannerlord-Deployer

cp .env.example .env
cp deployer/config.example.json deployer/config.json
```

Edit `.env`:

- `DISCORD_BOT_TOKEN`
- `COMPOSE_PROJECT_NAME` (e.g. `bl-coop`)
- `GAMESERVER_CONTAINER` — must match the gameserver container name (with the
  default compose file: `${COMPOSE_PROJECT_NAME}-gameserver`)

Edit `deployer/config.json`:

- `guild_id`, `announce_channel_id` (required; bot refuses to start if unset)
- `admin_role_ids` — **required**; at least one role ID. Discord “Administrator”
  permission does **not** grant deploy access unless you set
  `allow_guild_administrators: true`
- `gameserver_container` — same value as `GAMESERVER_CONTAINER`

### 3. Start (recommended: socket proxy)

```bash
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml up -d --build
```

(Trusted fallback: add `-f docker-compose.raw-socket.yml` instead of the socket-proxy file.)

Then in Discord:

1. `/checkupdates` — seed ModDB releases (first poll does not spam announcements)
2. `/deploy` — download and install the Console Server package into `data/server`
3. Wait for the gameserver to finish booting (up to ~2 minutes before it accepts connections)

Alternatively, manually download the Console Server from ModDB, extract it into
`data/server/`, then start only the gameserver service.

### 4. Saves

On first boot the server creates data under
`data/server-data/CoopData/DedicatedServer/` (including `Game Saves/`). Put
campaign `.sav` / `.json` pairs there if you are migrating an existing world.
The entrypoint symlinks the Wine Documents path to `data/server-data` so saves
are not wiped by deploys.

## Migrating from screen/Wine

```bash
mkdir -p data/server
screen -S <session-name> -X quit   # stop the old process
cp -a /path/to/current/server/. data/server/
mkdir -p "data/server-data/CoopData/DedicatedServer/Game Saves"
cp -a /path/to/old/saves/. "data/server-data/CoopData/DedicatedServer/Game Saves/"
```

Then bring the stack up as in Quick start.

## Discord commands

All mutating commands require a role listed in `admin_role_ids`.

| Command | Description |
| --- | --- |
| Announcement **Deploy** button | Deploy the announced ModDB file (with confirm) |
| `/deploy` | Pick a known release and deploy |
| `/rollback` | Restore the newest pre-deploy backup |
| `/restart` | Run `save_command`, stop container, start again |
| `/console` | Send one line to the server console (allowlisted) |
| `/status` | Container + release status |
| `/checkupdates` | Poll ModDB immediately |

`/console` only accepts commands matching `console_command_allowlist` (defaults:
`help`, `save…`, `coop.debug.…`). To allow any command, set the allowlist to
`["^.*$"]`. An empty allowlist denies all console commands.

## ModDB behavior

- The Coop team often **replaces the same ModDB file** instead of uploading a new
  entry. The bot fingerprints **filename + size** via a HEAD on the download
  mirror, not RSS `pubDate` alone.
- RSS is used to discover matching “Console Server” file IDs.
- ModDB HTML pages are frequently behind Cloudflare; this bot does **not** scrape
  those pages. It uses `/downloads/start/<id>` and the mirror URL instead.
- Archives are large (~4 GB). Deploys check free disk (~3× size) before downloading.

## Security

**Preferred:** run with `docker-compose.socket-proxy.yml` so the deployer only
talks to a locked-down Docker API (start/stop/logs/attach), not the raw host
socket.

```bash
docker compose -f docker-compose.yml -f docker-compose.socket-proxy.yml up -d --build
```

**Fallback (trusted hosts only):**

```bash
docker compose -f docker-compose.yml -f docker-compose.raw-socket.yml up -d --build
```

This mounts `/var/run/docker.sock` into the deployer. A stolen bot token can then
mean full Docker (≈ root) on the host.

Other notes:

- `admin_role_ids` is mandatory at startup.
- Guild Administrator bypass is **off** by default (`allow_guild_administrators`).
- `/console` is allowlisted; treat allowlist expansion as privilege escalation.
- Rate limits apply per Discord user (`console_rate_limit_per_minute`,
  `action_cooldown_seconds`).

## Configuration reference

See [deployer/config.example.json](deployer/config.example.json) for all keys,
including:

- `allow_guild_administrators`
- `console_command_allowlist`
- `console_rate_limit_per_minute` / `action_cooldown_seconds`
- `save_command` / `save_wait_seconds`
- `gameserver_container`

## Maintainer: mirror to GitHub

GitLab (`origin`) is the development remote. After merging to `main` there:

```bash
git remote add github https://github.com/COFFEE-Arma/Bannerlord-Deployer.git   # once
git push origin main
git push github main
```

## Development

```bash
python deployer/dry_run_test.py
```

GitHub Actions runs the same dry-run test on pushes and pull requests.
