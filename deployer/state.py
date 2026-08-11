"""Persistent runtime state, stored as JSON on the shared data volume."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from moddb import Release


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = {
            "seen_guids": [],
            "releases": {},  # file_id (str) -> release dict
            "deployed": None,  # release dict currently on the server
            "backups": [],  # newest first: {"file": str, "created": str, "deployed_before": dict|None}
        }
        if path.exists():
            self._data.update(json.loads(path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- feed tracking ------------------------------------------------------

    def is_first_run(self) -> bool:
        return not self._data["seen_guids"]

    def is_seen(self, guid: str) -> bool:
        return guid in self._data["seen_guids"]

    def record_release(self, release: Release) -> None:
        with self._lock:
            if release.guid not in self._data["seen_guids"]:
                self._data["seen_guids"].append(release.guid)
            self._data["releases"][str(release.file_id)] = release.to_dict()
            self._save()

    def get_release(self, file_id: int) -> Release | None:
        data = self._data["releases"].get(str(file_id))
        return Release.from_dict(data) if data else None

    def known_releases(self) -> list[Release]:
        """All known releases, newest first."""
        releases = [Release.from_dict(d) for d in self._data["releases"].values()]
        releases.sort(key=lambda r: r.file_id, reverse=True)
        return releases

    # -- deploy tracking ----------------------------------------------------

    @property
    def deployed(self) -> Release | None:
        data = self._data["deployed"]
        return Release.from_dict(data) if data else None

    def set_deployed(self, release: Release | None) -> None:
        with self._lock:
            self._data["deployed"] = release.to_dict() if release else None
            self._save()

    # -- backups ------------------------------------------------------------

    @property
    def backups(self) -> list[dict]:
        return list(self._data["backups"])

    def add_backup(self, entry: dict) -> None:
        with self._lock:
            self._data["backups"].insert(0, entry)
            self._save()

    def set_backups(self, backups: list[dict]) -> None:
        with self._lock:
            self._data["backups"] = backups
            self._save()
