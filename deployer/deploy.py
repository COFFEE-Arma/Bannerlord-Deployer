"""Deploy pipeline: download, stop container, backup, extract, restore, restart."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import NotFound

import moddb
from moddb import Release
from state import State

log = logging.getLogger(__name__)

SERVER_EXE = "BannerlordCoopServer.exe"
STOP_TIMEOUT_SECONDS = 90
STARTUP_CHECK_WAIT_SECONDS = 5


class DeployError(RuntimeError):
    pass


class Deployer:
    def __init__(self, config: dict, state: State):
        self.config = config
        self.state = state
        self.server_dir = Path(config["server_dir"])
        self.staging_dir = Path(config["staging_dir"])
        self.backup_dir = Path(config["backup_dir"])
        self._docker = docker.from_env()

    # -- container control ---------------------------------------------------

    def _container(self):
        try:
            return self._docker.containers.get(self.config["gameserver_container"])
        except NotFound:
            return None

    def container_status(self) -> str:
        container = self._container()
        return container.status if container else "not created"

    def send_command(self, command: str, response_wait: float = 2.0) -> str:
        """Write a console command to the gameserver's stdin and return the log
        output that follows. Requires `stdin_open: true` on the container."""
        container = self._container()
        if container is None or container.status != "running":
            raise DeployError("The gameserver container is not running.")

        since = datetime.now(timezone.utc)
        sock = container.attach_socket(params={"stdin": 1, "stream": 1})
        try:
            # docker-py returns a SocketIO wrapper; the underlying socket is the
            # documented way to write to attached stdin.
            sock._sock.sendall(command.encode("utf-8") + b"\n")
        finally:
            sock.close()

        time.sleep(response_wait)
        output = container.logs(since=since).decode("utf-8", errors="replace").strip()
        return output

    def _stop_server(self, report) -> None:
        container = self._container()
        if container is None:
            report("Gameserver container does not exist yet; skipping stop.")
            return
        if container.status == "running":
            report("Stopping gameserver container...")
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
            report("Gameserver stopped.")
        else:
            report(f"Gameserver container is not running (status: {container.status}).")

    def _start_server(self, report) -> None:
        container = self._container()
        if container is None:
            report(
                "Gameserver container does not exist; run `docker compose up -d gameserver` "
                "on the host to create it."
            )
            return
        report("Starting gameserver container...")
        container.start()

    # -- backup / restore ------------------------------------------------------

    def _backup(self, report) -> Path | None:
        if not self.server_dir.exists() or not any(self.server_dir.iterdir()):
            report("Server directory is empty; nothing to back up.")
            return None

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = self.backup_dir / f"server-{stamp}.tar.gz"

        report(f"Backing up current server files to {backup_path.name}...")
        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(self.server_dir, arcname=".")
        report(f"Backup complete ({backup_path.stat().st_size // 1024 // 1024} MB).")

        deployed = self.state.deployed
        self.state.add_backup(
            {
                "file": backup_path.name,
                "created": datetime.now(timezone.utc).isoformat(),
                "deployed_before": deployed.to_dict() if deployed else None,
            }
        )
        self._prune_backups(report)
        return backup_path

    def _prune_backups(self, report) -> None:
        retention = int(self.config.get("backup_retention", 3))
        backups = self.state.backups
        keep, drop = backups[:retention], backups[retention:]
        for entry in drop:
            path = self.backup_dir / entry["file"]
            if path.exists():
                path.unlink()
            report(f"Pruned old backup {entry['file']}.")
        if drop:
            self.state.set_backups(keep)

    # -- extraction -------------------------------------------------------------

    def _extract_archive(self, archive: Path, dest: Path) -> None:
        suffix = archive.suffix.lower()
        if suffix == ".7z":
            result = subprocess.run(
                ["7z", "x", "-y", f"-o{dest}", str(archive)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise DeployError(f"7z extraction failed:\n{result.stderr or result.stdout}")
        else:
            try:
                shutil.unpack_archive(str(archive), str(dest))
            except shutil.ReadError as exc:
                raise DeployError(f"Could not extract {archive.name}: {exc}") from exc

    @staticmethod
    def _find_content_root(extracted: Path) -> Path:
        """Find the directory containing the server exe (archives sometimes wrap
        everything in a top-level folder)."""
        candidates = sorted(
            extracted.rglob(SERVER_EXE), key=lambda p: len(p.relative_to(extracted).parts)
        )
        if candidates:
            return candidates[0].parent

        entries = list(extracted.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            return entries[0]
        return extracted

    def _preserve_files(self) -> tuple[Path, list[Path]]:
        """Copy preserved files (config, saves) to a temp dir; return (tempdir, relpaths)."""
        temp = Path(tempfile.mkdtemp(prefix="preserve-", dir=self.staging_dir))
        preserved: list[Path] = []
        for pattern in self.config.get("preserve", []):
            for match in self.server_dir.glob(pattern):
                if not match.is_file():
                    continue
                rel = match.relative_to(self.server_dir)
                target = temp / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(match, target)
                preserved.append(rel)
        return temp, preserved

    def _restore_files(self, temp: Path, preserved: list[Path], report) -> None:
        for rel in preserved:
            target = self.server_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(temp / rel, target)
        if preserved:
            report(f"Restored {len(preserved)} preserved file(s): "
                   + ", ".join(str(p) for p in preserved[:5])
                   + ("..." if len(preserved) > 5 else ""))

    # -- public operations --------------------------------------------------------

    def deploy(self, release: Release, progress_cb=None) -> dict:
        """Full deploy of a release. Blocking; run in a thread from the bot."""
        started = time.monotonic()

        def report(message: str) -> None:
            log.info("[deploy] %s", message)
            if progress_cb:
                progress_cb(message)

        archive = moddb.download_release(release, self.staging_dir, progress_cb=report)
        extract_dir = Path(tempfile.mkdtemp(prefix="extract-", dir=self.staging_dir))

        try:
            report(f"Extracting {archive.name}...")
            self._extract_archive(archive, extract_dir)
            content_root = self._find_content_root(extract_dir)
            report("Extraction complete.")

            self._stop_server(report)
            self._backup(report)

            preserve_temp, preserved = self._preserve_files()
            try:
                report("Copying new server files into place...")
                self.server_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(content_root, self.server_dir, dirs_exist_ok=True)
                self._restore_files(preserve_temp, preserved, report)
            finally:
                shutil.rmtree(preserve_temp, ignore_errors=True)

            self._start_server(report)
            self.state.set_deployed(release)

            # Brief pause so an immediate crash shows up as a non-running status.
            time.sleep(STARTUP_CHECK_WAIT_SECONDS)
            duration = int(time.monotonic() - started)
            report(f"Deploy finished in {duration}s.")
            return {
                "release": release.label,
                "duration_s": duration,
                "container_status": self.container_status(),
            }
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
            archive.unlink(missing_ok=True)

    def rollback(self, progress_cb=None) -> dict:
        """Restore the newest backup and restart the server."""
        started = time.monotonic()

        def report(message: str) -> None:
            log.info("[rollback] %s", message)
            if progress_cb:
                progress_cb(message)

        backups = self.state.backups
        if not backups:
            raise DeployError("No backups available to roll back to.")

        entry = backups[0]
        backup_path = self.backup_dir / entry["file"]
        if not backup_path.exists():
            raise DeployError(f"Backup file {entry['file']} is missing on disk.")

        self._stop_server(report)

        report(f"Restoring backup {entry['file']}...")
        if self.server_dir.exists():
            shutil.rmtree(self.server_dir)
        self.server_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(backup_path, "r:gz") as tar:
            tar.extractall(self.server_dir, filter="data")
        report("Backup restored.")

        self._start_server(report)

        previous = entry.get("deployed_before")
        self.state.set_deployed(Release.from_dict(previous) if previous else None)
        self.state.set_backups(backups[1:])
        backup_path.unlink(missing_ok=True)

        time.sleep(STARTUP_CHECK_WAIT_SECONDS)
        duration = int(time.monotonic() - started)
        report(f"Rollback finished in {duration}s.")
        return {
            "release": Release.from_dict(previous).label if previous else "(unknown)",
            "duration_s": duration,
            "container_status": self.container_status(),
        }
