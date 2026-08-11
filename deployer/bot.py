"""Discord bot: announces new ModDB releases and runs role-gated deploys."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks

import moddb
from deploy import Deployer
from moddb import Release
from state import State

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("updater")

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config.json"))

EMBED_COLOR_INFO = 0x2B6CB0
EMBED_COLOR_OK = 0x2F855A
EMBED_COLOR_ERROR = 0xC53030
EMBED_COLOR_PROGRESS = 0xB7791F


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def is_admin(config: dict, user: discord.abc.User) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    admin_roles = set(config.get("admin_role_ids", []))
    return any(role.id in admin_roles for role in user.roles)


class ProgressReporter:
    """Collects progress lines from the worker thread and edits a Discord message,
    throttled so we do not hammer the API during long downloads."""

    MIN_EDIT_INTERVAL = 3.0

    def __init__(self, message: discord.Message, title: str, loop: asyncio.AbstractEventLoop):
        self.message = message
        self.title = title
        self.loop = loop
        self.lines: list[str] = []
        self._last_edit = 0.0

    def __call__(self, text: str) -> None:  # called from a worker thread
        self.lines.append(text)
        asyncio.run_coroutine_threadsafe(self.flush(), self.loop)

    def _embed(self, color: int = EMBED_COLOR_PROGRESS) -> discord.Embed:
        body = "\n".join(f"- {line}" for line in self.lines[-15:]) or "Starting..."
        return discord.Embed(title=self.title, description=body[:4000], color=color)

    async def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_edit < self.MIN_EDIT_INTERVAL:
            return
        self._last_edit = now
        try:
            await self.message.edit(embed=self._embed())
        except discord.HTTPException:
            log.warning("Failed to edit progress message", exc_info=True)


class UpdaterBot(commands.Bot):
    def __init__(self, config: dict):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.state = State(Path(config["state_file"]))
        self.deployer = Deployer(config, self.state)
        self.deploy_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.add_dynamic_items(DeployButton, DismissButton)
        register_commands(self)

        guild_id = int(self.config.get("guild_id") or 0)
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        self.poll_feed.change_interval(minutes=int(self.config.get("poll_interval_minutes", 30)))
        self.poll_feed.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id)

    # -- feed polling ---------------------------------------------------------

    @tasks.loop(minutes=30)
    async def poll_feed(self) -> None:
        try:
            releases = await asyncio.to_thread(
                moddb.check_updates, self.config["rss_url"], self.config["title_filter"]
            )
        except Exception:
            log.exception("Failed to poll ModDB feed")
            return

        first_run = self.state.is_first_run()
        for release in releases:
            stored = self.state.get_release(release.file_id)
            # ModDB entries are replaced in place for new versions (same GUID and
            # pubDate), so compare the served file's fingerprint, not the feed item.
            if stored is not None and stored.version_key == release.version_key:
                continue
            replaced = stored is not None
            self.state.record_release(release)
            if first_run:
                # Seed existing uploads silently so we do not spam history on
                # first start; they remain deployable via /deploy.
                log.info("Seeded existing release without announcing: %s", release.label)
                continue
            await self.announce_release(release, replaced=replaced)

    @poll_feed.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    async def announce_release(self, release: Release, replaced: bool = False) -> None:
        channel = self.get_channel(int(self.config["announce_channel_id"]))
        if channel is None:
            log.error("Announce channel %s not found", self.config["announce_channel_id"])
            return

        embed = discord.Embed(
            title=(
                "Bannerlord Coop server update available (file replaced on ModDB)"
                if replaced
                else "New Bannerlord Coop server update available"
            ),
            description=release.description[:2000] or "(no description)",
            color=EMBED_COLOR_INFO,
            url=release.link,
        )
        embed.add_field(name="File", value=release.filename or release.title, inline=True)
        embed.add_field(
            name="Size",
            value=f"{release.size_mb} MB" if release.size else "unknown",
            inline=True,
        )
        embed.add_field(name="ModDB file ID", value=f"#{release.file_id}", inline=True)
        embed.set_footer(text="Deploy stops the server, backs it up, and installs this update.")

        view = discord.ui.View(timeout=None)
        view.add_item(DeployButton(release.file_id))
        view.add_item(DismissButton(release.file_id))
        await channel.send(embed=embed, view=view)
        log.info("Announced release %s", release.label)

    # -- deploy orchestration -----------------------------------------------------

    async def run_deploy(
        self,
        channel: discord.abc.Messageable,
        requested_by: discord.abc.User,
        release: Release,
    ) -> None:
        if self.deploy_lock.locked():
            await channel.send("A deploy or rollback is already in progress; try again once it finishes.")
            return

        async with self.deploy_lock:
            title = f"Deploying {release.title} (#{release.file_id})"
            message = await channel.send(
                embed=discord.Embed(
                    title=title,
                    description=f"Requested by {requested_by.mention}",
                    color=EMBED_COLOR_PROGRESS,
                )
            )
            reporter = ProgressReporter(message, title, asyncio.get_running_loop())
            try:
                report = await asyncio.to_thread(self.deployer.deploy, release, reporter)
            except Exception as exc:  # noqa: BLE001 - report all failures to Discord
                log.exception("Deploy failed")
                await reporter.flush(force=True)
                await channel.send(
                    embed=discord.Embed(
                        title="Deploy failed",
                        description=f"`{type(exc).__name__}`: {str(exc)[:1500]}",
                        color=EMBED_COLOR_ERROR,
                    )
                )
                return

            await reporter.flush(force=True)
            await channel.send(embed=self._result_embed("Deploy complete", report))

    async def run_rollback(
        self, channel: discord.abc.Messageable, requested_by: discord.abc.User
    ) -> None:
        if self.deploy_lock.locked():
            await channel.send("A deploy or rollback is already in progress; try again once it finishes.")
            return

        async with self.deploy_lock:
            title = "Rolling back to previous backup"
            message = await channel.send(
                embed=discord.Embed(
                    title=title,
                    description=f"Requested by {requested_by.mention}",
                    color=EMBED_COLOR_PROGRESS,
                )
            )
            reporter = ProgressReporter(message, title, asyncio.get_running_loop())
            try:
                report = await asyncio.to_thread(self.deployer.rollback, reporter)
            except Exception as exc:  # noqa: BLE001
                log.exception("Rollback failed")
                await reporter.flush(force=True)
                await channel.send(
                    embed=discord.Embed(
                        title="Rollback failed",
                        description=f"`{type(exc).__name__}`: {str(exc)[:1500]}",
                        color=EMBED_COLOR_ERROR,
                    )
                )
                return

            await reporter.flush(force=True)
            await channel.send(embed=self._result_embed("Rollback complete", report))

    @staticmethod
    def _result_embed(title: str, report: dict) -> discord.Embed:
        running = report["container_status"] == "running"
        embed = discord.Embed(title=title, color=EMBED_COLOR_OK if running else EMBED_COLOR_ERROR)
        embed.add_field(name="Release", value=report["release"], inline=False)
        embed.add_field(name="Duration", value=f"{report['duration_s']}s", inline=True)
        embed.add_field(name="Container", value=report["container_status"], inline=True)
        if running:
            embed.add_field(
                name="Server booting",
                value=(
                    "The server is starting up — it can take up to 2 minutes "
                    "before it accepts connections."
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="Warning",
                value=(
                    "The gameserver container is not running. Check "
                    "`docker compose logs gameserver` on the host."
                ),
                inline=False,
            )
        return embed


# -- persistent announcement buttons ------------------------------------------------


class DeployButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"coopdeploy:(?P<file_id>\d+)",
):
    def __init__(self, file_id: int):
        super().__init__(
            discord.ui.Button(
                label="Deploy",
                style=discord.ButtonStyle.danger,
                custom_id=f"coopdeploy:{file_id}",
            )
        )
        self.file_id = file_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["file_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: UpdaterBot = interaction.client
        if not is_admin(bot.config, interaction.user):
            await interaction.response.send_message(
                "You need the server admin role to deploy updates.", ephemeral=True
            )
            return

        release = bot.state.get_release(self.file_id)
        if release is None:
            await interaction.response.send_message(
                "This release is no longer tracked in state.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Deploy **{release.label}**? The server will be stopped, backed up, and updated.",
            view=ConfirmDeployView(release),
            ephemeral=True,
        )


class DismissButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"coopdismiss:(?P<file_id>\d+)",
):
    def __init__(self, file_id: int):
        super().__init__(
            discord.ui.Button(
                label="Dismiss",
                style=discord.ButtonStyle.secondary,
                custom_id=f"coopdismiss:{file_id}",
            )
        )
        self.file_id = file_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["file_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: UpdaterBot = interaction.client
        if not is_admin(bot.config, interaction.user):
            await interaction.response.send_message(
                "You need the server admin role to dismiss announcements.", ephemeral=True
            )
            return
        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if embed:
            embed.color = discord.Color.dark_grey()
            embed.set_footer(text=f"Dismissed by {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=None)


class ConfirmDeployView(discord.ui.View):
    def __init__(self, release: Release):
        super().__init__(timeout=120)
        self.release = release

    @discord.ui.button(label="Confirm deploy", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        bot: UpdaterBot = interaction.client
        await interaction.response.edit_message(
            content=f"Deploy of **{self.release.label}** started.", view=None
        )
        await bot.run_deploy(interaction.channel, interaction.user, self.release)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Deploy cancelled.", view=None)


class ConfirmRollbackView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Confirm rollback", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        bot: UpdaterBot = interaction.client
        await interaction.response.edit_message(content="Rollback started.", view=None)
        await bot.run_rollback(interaction.channel, interaction.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Rollback cancelled.", view=None)


class ReleaseSelectView(discord.ui.View):
    def __init__(self, releases: list[Release]):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=release.label[:100],
                value=str(release.file_id),
                description=(
                    f"{release.size_mb} MB — {release.description}"[:100]
                    if release.size
                    else release.description[:100] or None
                ),
            )
            for release in releases[:25]
        ]
        select = discord.ui.Select(placeholder="Choose a release to deploy", options=options)
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        bot: UpdaterBot = interaction.client
        release = bot.state.get_release(int(self._select.values[0]))
        if release is None:
            await interaction.response.edit_message(content="Release not found.", view=None)
            return
        await interaction.response.edit_message(
            content=f"Deploy **{release.label}**? The server will be stopped, backed up, and updated.",
            view=ConfirmDeployView(release),
        )


# -- slash commands ------------------------------------------------------------------


def register_commands(bot: UpdaterBot) -> None:
    def admin_only(interaction: discord.Interaction) -> bool:
        return is_admin(bot.config, interaction.user)

    @bot.tree.command(name="deploy", description="Deploy a Bannerlord Coop server update from ModDB")
    async def deploy_cmd(interaction: discord.Interaction) -> None:
        if not admin_only(interaction):
            await interaction.response.send_message(
                "You need the server admin role to deploy updates.", ephemeral=True
            )
            return
        releases = bot.state.known_releases()
        if not releases:
            await interaction.response.send_message(
                "No releases known yet; try /checkupdates first.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Select the release to deploy:", view=ReleaseSelectView(releases), ephemeral=True
        )

    @bot.tree.command(name="rollback", description="Restore the most recent server backup")
    async def rollback_cmd(interaction: discord.Interaction) -> None:
        if not admin_only(interaction):
            await interaction.response.send_message(
                "You need the server admin role to roll back.", ephemeral=True
            )
            return
        backups = bot.state.backups
        if not backups:
            await interaction.response.send_message("No backups available.", ephemeral=True)
            return
        entry = backups[0]
        before = entry.get("deployed_before")
        label = Release.from_dict(before).label if before else "(unknown release)"
        await interaction.response.send_message(
            f"Roll back to backup `{entry['file']}` (created {entry['created'][:16]} UTC, "
            f"was running {label})?",
            view=ConfirmRollbackView(),
            ephemeral=True,
        )

    @bot.tree.command(name="status", description="Show game server and update status")
    async def status_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        container_status = await asyncio.to_thread(bot.deployer.container_status)
        deployed = bot.state.deployed
        releases = bot.state.known_releases()
        latest = releases[0] if releases else None

        embed = discord.Embed(title="Bannerlord Coop server status", color=EMBED_COLOR_INFO)
        embed.add_field(name="Container", value=container_status, inline=True)
        embed.add_field(
            name="Deployed release",
            value=deployed.label if deployed else "(unknown — nothing deployed via bot yet)",
            inline=False,
        )
        embed.add_field(
            name="Latest known release",
            value=latest.label if latest else "(none seen yet)",
            inline=False,
        )
        embed.add_field(name="Backups", value=str(len(bot.state.backups)), inline=True)
        if deployed and latest and latest.version_key != deployed.version_key:
            embed.add_field(name="Update available", value=f"Yes — {latest.label}", inline=False)
            embed.color = EMBED_COLOR_PROGRESS
        await interaction.followup.send(embed=embed, ephemeral=True)

    @bot.tree.command(name="checkupdates", description="Poll ModDB for new releases right now")
    async def checkupdates_cmd(interaction: discord.Interaction) -> None:
        if not admin_only(interaction):
            await interaction.response.send_message(
                "You need the server admin role to trigger a check.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        before = {r.file_id: r.version_key for r in bot.state.known_releases()}
        await bot.poll_feed()
        new = [
            r for r in bot.state.known_releases() if before.get(r.file_id) != r.version_key
        ]
        if new:
            await interaction.followup.send(
                f"Found {len(new)} new/updated release(s); announced in the update channel.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("No new releases found.", ephemeral=True)


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN environment variable is not set.")
    config = load_config()
    UpdaterBot(config).run(token, log_handler=None)


if __name__ == "__main__":
    main()
