# Bannerlord Coop Updater

Docker Compose stack for hosting the [Bannerlord Coop](https://www.moddb.com/mods/bannerlord-coop)
Console Server on Debian, with a Discord bot that:

- polls ModDB for new **Console Server** versions (the mod team replaces the
  same file entry in place, so the bot fingerprints the served file's name and
  size rather than relying on new RSS items),
- announces new uploads in your Discord channel with a **Deploy** button,
- lets admins (by Discord role) deploy any known release or roll back to a backup,
- stops/starts the game server container automatically during deploys and
  preserves `server-config.json` and saves across updates.

## Services

| Service | Container | Purpose |
| --- | --- | --- |
| `gameserver` | `bannerlord-coop-server` | Runs `BannerlordCoopServer.exe` under Wine (WineHQ stable) |
| `deployer` | `bannerlord-coop-deployer` | Discord bot + deploy pipeline; controls `gameserver` via the Docker socket |

Both share the `./data` directory:

```
data/
  server/       <- the Console Server install (BannerlordCoopServer.exe, server-config.json, ...)
  server-data/  <- persistent server data (saves under CoopData/DedicatedServer/"Game Saves");
                   symlinked into the wineprefix Documents folder by the entrypoint
  staging/      <- downloads and extraction scratch space
  backups/      <- tar.gz backups taken before each deploy (retention configurable)
  wineprefix/   <- persistent Wine prefix
  state.json    <- seen releases, deployed version, backup metadata
```

## Setup

### 1. Create the Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and
   create an application, then add a **Bot** to it.
2. Copy the bot token (this goes in `.env`).
3. No privileged intents are required.
4. Invite the bot to your server with an OAuth2 URL using scopes
   `bot` + `applications.commands` and permissions **Send Messages** and
   **Embed Links**:
   `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot%20applications.commands&permissions=18432`

### 2. Configure

On the Debian host (requires Docker + the compose plugin: `apt install docker.io docker-compose-plugin`
or follow [docs.docker.com/engine/install/debian](https://docs.docker.com/engine/install/debian/)):

```bash
git clone <this repo> /opt/bannerlord-coop-updater
cd /opt/bannerlord-coop-updater

cp .env.example .env                                    # fill in DISCORD_BOT_TOKEN
cp deployer/config.example.json deployer/config.json    # fill in IDs below
```

In `deployer/config.json` set:

- `guild_id` — your Discord server ID (right-click server icon → Copy Server ID,
  with Developer Mode enabled). Makes slash commands appear instantly.
- `announce_channel_id` — the channel where update announcements are posted.
- `admin_role_ids` — list of role IDs allowed to deploy/roll back (guild
  administrators are always allowed).

### 3. Migrate the existing install (from screen/Wine)

```bash
mkdir -p data/server

# Stop the currently running server first (inside your screen session, or:)
screen -S <session-name> -X quit

# Copy the existing install, including server-config.json
cp -a /path/to/current/server/. data/server/

# Put existing campaign saves (<name>.sav + <name>.json pairs) where the
# server reads them:
mkdir -p "data/server-data/CoopData/DedicatedServer/Game Saves"
cp -a /path/to/old/saves/. "data/server-data/CoopData/DedicatedServer/Game Saves/"
```

The Console Server stores its persistent data (campaign saves, config backups)
under `Documents\Mount and Blade II Bannerlord\` inside the Wine prefix, in
`CoopData\DedicatedServer\Game Saves\`. The container entrypoint symlinks that
Documents folder to `data/server-data/`, so all of it lives on the volume,
outside the install dir, where deploys and rollbacks never touch it.

### 4. Start

```bash
docker compose up -d --build
```

- The game server uses host networking (Docker NAT breaks its UDP traffic and
  advertised IP), so it listens directly on ports 4200-4201 (game, TCP+UDP)
  and 7210 (Steam/query, TCP+UDP); make sure these are open in your firewall.
- The bot logs in, syncs slash commands, and starts polling ModDB every 30 minutes
  (configurable via `poll_interval_minutes`).
- On the very first poll, existing ModDB uploads are recorded **without** being
  announced (no spam); they are still selectable via `/deploy`.
- Check logs with `docker compose logs -f deployer` and `docker compose logs -f gameserver`.

If the game server exits immediately because Wine cannot run headless, set
`USE_XVFB=1` in `.env` and run `docker compose up -d gameserver` again — this
wraps the server in a virtual framebuffer.

## Discord usage

- **Announcement**: when a new Console Server file appears on ModDB, or the
  existing file is replaced with a new version, the bot posts an embed with
  **Deploy** and **Dismiss** buttons (admin-only).
- `/deploy` — pick any known release from a dropdown and deploy it (with confirmation).
- `/rollback` — restore the most recent pre-deploy backup (with confirmation).
- `/status` — container state, deployed release, latest known release, backup count.
- `/checkupdates` — poll ModDB immediately instead of waiting for the next cycle.

A deploy does the following, reporting progress in the channel:

1. Resolves the ModDB mirror and downloads the archive to `data/staging`
   (checks ~3x the archive size in free disk space first).
2. Extracts the archive (7z/zip/tar supported) and locates the folder containing
   `BannerlordCoopServer.exe`.
3. Stops the `gameserver` container (90s grace).
4. Backs up `data/server` to `data/backups/server-<timestamp>.tar.gz` and prunes
   old backups beyond `backup_retention`.
5. Copies the new files over the install, then restores the preserved files
   (`preserve` globs in config — by default the install-dir `server-config.json`;
   the live config and saves are in `data/server-data`, untouched by deploys).
6. Starts the container, confirms it is running, and notes that the server may
   take up to 2 minutes before accepting connections.

Only one deploy/rollback can run at a time.

## Security note

The deployer mounts `/var/run/docker.sock`, which is effectively root access to
the host. Deploy actions are gated behind the Discord admin role allowlist, but
treat the bot token accordingly (anyone who controls the bot application can
execute deploys). If you want tighter isolation later, put a docker socket proxy
(e.g. `tecnativa/docker-socket-proxy` with only container start/stop allowed)
between the deployer and the socket.

## Development / verification

`deployer/dry_run_test.py` exercises the full deploy + rollback pipeline locally
with a fake archive and a mocked Docker client (no Discord or Docker needed):

```bash
python deployer/dry_run_test.py
```
