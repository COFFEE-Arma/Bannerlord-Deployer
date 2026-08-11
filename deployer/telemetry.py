"""Anonymous install telemetry for Bannerlord Deployer.

Sends a periodic heartbeat with a random install UUID (no Discord IDs, IPs, or
guild names). Disable with telemetry.enabled=false or TELEMETRY_ENABLED=0.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from state import State

log = logging.getLogger("updater.telemetry")

DEFAULT_URL = "https://deployer-telemetry.arma.coffee/v1/heartbeat"
APP_NAME = "bannerlord-deployer"


def telemetry_enabled(config: dict) -> bool:
    env = os.environ.get("TELEMETRY_ENABLED", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    section = config.get("telemetry") or {}
    return bool(section.get("enabled", True))


def ensure_install_id(state: "State") -> str:
    existing = state.install_id
    if existing:
        return existing
    install_id = str(uuid.uuid4())
    state.set_install_id(install_id)
    return install_id


def send_heartbeat(config: dict, state: "State") -> bool:
    if not telemetry_enabled(config):
        return False

    section = config.get("telemetry") or {}
    url = (os.environ.get("TELEMETRY_URL") or section.get("url") or DEFAULT_URL).strip()
    version = (
        os.environ.get("DEPLOYER_VERSION")
        or section.get("version")
        or "unknown"
    )
    install_id = ensure_install_id(state)
    payload = {
        "app": APP_NAME,
        "install_id": install_id,
        "version": version,
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code >= 400:
            log.warning("Telemetry heartbeat failed: HTTP %s", response.status_code)
            return False
        log.info("Telemetry heartbeat sent")
        return True
    except requests.RequestException as exc:
        log.warning("Telemetry heartbeat failed: %s", exc)
        return False
