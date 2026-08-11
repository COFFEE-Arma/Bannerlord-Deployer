"""Dry-run test of the deploy pipeline: fake archive, mocked Docker, no Discord.

Run with:  python deployer/dry_run_test.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import zipfile
from pathlib import Path

# -- mock the docker SDK before importing deploy ------------------------------------

docker_mod = types.ModuleType("docker")
errors_mod = types.ModuleType("docker.errors")


class NotFound(Exception):
    pass


class FakeContainer:
    def __init__(self):
        self.status = "running"
        self.events: list[str] = []
        self.stdin_writes: list[bytes] = []

    def stop(self, timeout=None):
        self.events.append("stop")
        self.status = "exited"

    def start(self):
        self.events.append("start")
        self.status = "running"

    def logs(self, tail=30, since=None):
        if since is not None:
            return b"[fake server] save complete"
        return b"[fake server] listening on port 4201"

    def attach_socket(self, params=None):
        container = self

        class FakeSock:
            class _Sock:
                @staticmethod
                def sendall(data: bytes):
                    container.stdin_writes.append(data)
                    container.events.append("stdin")

            _sock = _Sock()

            def close(self):
                pass

        return FakeSock()


FAKE_CONTAINER = FakeContainer()


class FakeContainers:
    @staticmethod
    def get(name):
        return FAKE_CONTAINER


class FakeClient:
    containers = FakeContainers()


errors_mod.NotFound = NotFound
docker_mod.errors = errors_mod
docker_mod.from_env = lambda: FakeClient()
sys.modules["docker"] = docker_mod
sys.modules["docker.errors"] = errors_mod

sys.path.insert(0, str(Path(__file__).parent))

import deploy  # noqa: E402
import moddb  # noqa: E402
from deploy import Deployer  # noqa: E402
from moddb import Release  # noqa: E402
from state import State  # noqa: E402

deploy.STARTUP_CHECK_WAIT_SECONDS = 0


def check(condition: bool, label: str) -> None:
    print(("  PASS  " if condition else "  FAIL  ") + label)
    if not condition:
        raise SystemExit(f"Dry-run failed at: {label}")


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="coop-dryrun-"))
    print(f"Working in {root}")

    server = root / "server"
    (server / "Saves").mkdir(parents=True)
    (server / "BannerlordCoopServer.exe").write_text("OLD BINARY")
    (server / "server-config.json").write_text('{"port": 4201, "name": "our server"}')
    (server / "Saves" / "campaign1.sav").write_text("save data")
    (server / "old-file.dll").write_text("stale library")

    # Fake "new release" archive: wrapped in a top-level dir, with a default
    # config that must NOT overwrite ours, and without old-file.dll.
    archive_src = root / "archive-src" / "DedicatedServer"
    archive_src.mkdir(parents=True)
    (archive_src / "BannerlordCoopServer.exe").write_text("NEW BINARY")
    (archive_src / "server-config.json").write_text('{"port": 4201, "name": "DEFAULT"}')
    (archive_src / "new-file.dll").write_text("new library")
    fake_archive = root / "DedicatedServer.zip"
    with zipfile.ZipFile(fake_archive, "w") as zf:
        for path in archive_src.rglob("*"):
            zf.write(path, path.relative_to(archive_src.parent))

    config = {
        "server_dir": str(server),
        "staging_dir": str(root / "staging"),
        "backup_dir": str(root / "backups"),
        "gameserver_container": "bannerlord-coop-server",
        "preserve": ["server-config.json", "Saves/**/*"],
        "backup_retention": 3,
        "save_command": "save",
        "save_wait_seconds": 0,
    }
    state = State(root / "state.json")
    deployer = Deployer(config, state)

    release = Release(
        file_id=313475,
        guid="downloads313475",
        title="Console Server",
        link="https://www.moddb.com/mods/bannerlord-coop/downloads/console-server",
        published="2026-07-29T23:58:17+00:00",
        description="Console Server for Bannerlord Coop.",
    )
    state.record_release(release)

    def fake_download(rel, staging_dir, progress_cb=None):
        staging_dir.mkdir(parents=True, exist_ok=True)
        target = staging_dir / fake_archive.name
        shutil.copy2(fake_archive, target)
        if progress_cb:
            progress_cb(f"(fake) downloaded {target.name}")
        return target

    moddb.download_release = fake_download

    print("\n== deploy ==")
    report = deployer.deploy(release, progress_cb=lambda m: print(f"    {m}"))

    check((server / "BannerlordCoopServer.exe").read_text() == "NEW BINARY", "new binary installed")
    check((server / "new-file.dll").exists(), "new files copied in")
    check(
        (server / "server-config.json").read_text() == '{"port": 4201, "name": "our server"}',
        "server-config.json preserved",
    )
    check((server / "Saves" / "campaign1.sav").read_text() == "save data", "saves preserved")
    check(len(list((root / "backups").glob("*.tar.gz"))) == 1, "backup created")
    check(FAKE_CONTAINER.events == ["stop", "start"], "container stopped then started")
    check(state.deployed and state.deployed.file_id == 313475, "state records deployed release")
    check(not any((root / "staging").glob("*.zip")), "staging archive cleaned up")
    check(report["container_status"] == "running", "container running after deploy")

    print("\n== rollback ==")
    (server / "BannerlordCoopServer.exe").write_text("BROKEN")
    deployer.rollback(progress_cb=lambda m: print(f"    {m}"))

    check(
        (server / "BannerlordCoopServer.exe").read_text() == "OLD BINARY",
        "old binary restored from backup",
    )
    check((server / "old-file.dll").exists(), "pre-deploy file restored")
    check((server / "Saves" / "campaign1.sav").exists(), "saves restored")
    check(FAKE_CONTAINER.events == ["stop", "start", "stop", "start"], "container cycled for rollback")
    check(len(state.backups) == 0, "consumed backup removed from state")

    print("\n== console + restart ==")
    out = deployer.send_command("help", response_wait=0)
    check(b"help\n" in FAKE_CONTAINER.stdin_writes[-1], "console command written to stdin")
    check("save complete" in out, "console captures following logs")

    FAKE_CONTAINER.events.clear()
    FAKE_CONTAINER.stdin_writes.clear()
    restart_report = deployer.restart(progress_cb=lambda m: print(f"    {m}"))
    check(FAKE_CONTAINER.stdin_writes and b"save\n" in FAKE_CONTAINER.stdin_writes[0], "restart sends save")
    check(FAKE_CONTAINER.events == ["stdin", "stop", "start"], "restart save then stop/start")
    check(restart_report["container_status"] == "running", "container running after restart")

    shutil.rmtree(root, ignore_errors=True)
    print("\nAll dry-run checks passed.")


if __name__ == "__main__":
    main()
