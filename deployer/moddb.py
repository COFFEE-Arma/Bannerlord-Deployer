"""ModDB RSS feed polling and file downloading.

ModDB does not offer a download API, so this module:
  1. polls the mod's downloads RSS feed for new files,
  2. resolves the real mirror URL behind /downloads/start/<id>,
  3. streams the archive to the staging directory.
"""

from __future__ import annotations

import html
import logging
import re
import shutil
from dataclasses import dataclass, asdict
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) bannerlord-coop-updater/1.0 "
    "(+https://www.moddb.com/mods/bannerlord-coop)"
)
MODDB_BASE = "https://www.moddb.com"

# Require free space for the archive plus extraction plus a safety margin.
FREE_SPACE_FACTOR = 3

DOWNLOAD_CHUNK = 1024 * 256


@dataclass
class Release:
    file_id: int
    guid: str
    title: str
    link: str
    published: str  # ISO 8601
    description: str
    # Current file behind the download: ModDB entries are replaced in place when
    # the mod team ships a new version (same GUID/pubDate), so the archive
    # filename + byte size act as the version fingerprint.
    filename: str = ""
    size: int = 0

    @property
    def version_key(self) -> str:
        return f"{self.filename}|{self.size}"

    @property
    def size_mb(self) -> int:
        return self.size // 1024 // 1024

    @property
    def label(self) -> str:
        if self.filename:
            return f"{self.title} — {self.filename} (#{self.file_id})"
        date = self.published[:16].replace("T", " ")
        return f"{self.title} — {date} UTC (#{self.file_id})"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Release":
        return cls(**data)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def fetch_feed(rss_url: str, title_filter: str) -> list[Release]:
    """Return feed items whose title matches ``title_filter`` (regex, case-insensitive),
    newest first."""
    with _session() as session:
        response = session.get(rss_url, timeout=30)
        response.raise_for_status()

    root = ElementTree.fromstring(response.content)
    pattern = re.compile(title_filter, re.IGNORECASE)
    releases: list[Release] = []

    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description = _strip_html(item.findtext("description") or "")

        id_match = re.fullmatch(r"downloads(\d+)", guid)
        if not id_match or not pattern.search(title):
            continue

        try:
            published = parsedate_to_datetime(pub_date).isoformat()
        except (TypeError, ValueError):
            published = pub_date

        releases.append(
            Release(
                file_id=int(id_match.group(1)),
                guid=guid,
                title=title,
                link=link,
                published=published,
                description=description,
            )
        )

    releases.sort(key=lambda r: r.file_id, reverse=True)
    return releases


def resolve_mirror_url(session: requests.Session, file_id: int) -> str:
    """Resolve the mirror URL that actually serves the file for a ModDB download id."""
    start_url = f"{MODDB_BASE}/downloads/start/{file_id}/all"
    response = session.get(start_url, timeout=30)
    response.raise_for_status()

    match = re.search(r'["\']((?:https?://www\.moddb\.com)?/downloads/mirror/[^"\']+)["\']', response.text)
    if not match:
        raise RuntimeError(
            f"Could not find a mirror link on {start_url}; ModDB may have changed their page layout."
        )

    mirror = match.group(1)
    if mirror.startswith("/"):
        mirror = MODDB_BASE + mirror
    return mirror


def _filename_from_response(response: requests.Response, file_id: int) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match:
        return Path(match.group(1).strip()).name

    path_name = Path(requests.utils.urlparse(response.url).path).name
    if path_name and "." in path_name:
        return path_name
    return f"moddb_{file_id}.zip"


def fetch_file_info(session: requests.Session, file_id: int) -> tuple[str, int]:
    """Return (filename, size in bytes) of the file currently served for a
    download id, via a HEAD request on the resolved mirror (no body transfer)."""
    mirror = resolve_mirror_url(session, file_id)
    response = session.head(
        mirror,
        headers={"Referer": f"{MODDB_BASE}/downloads/start/{file_id}"},
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    filename = _filename_from_response(response, file_id)
    size = int(response.headers.get("Content-Length") or 0)
    return filename, size


def check_updates(rss_url: str, title_filter: str) -> list[Release]:
    """Fetch matching feed items and populate each with the fingerprint of the
    file currently being served for it."""
    releases = fetch_feed(rss_url, title_filter)
    with _session() as session:
        for release in releases:
            release.filename, release.size = fetch_file_info(session, release.file_id)
    return releases


def download_release(
    release: Release,
    staging_dir: Path,
    progress_cb=None,
) -> Path:
    """Download a release archive into ``staging_dir`` and return its path.

    ``progress_cb`` is called with short human-readable status strings.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)

    def report(message: str) -> None:
        log.info(message)
        if progress_cb:
            progress_cb(message)

    with _session() as session:
        mirror = resolve_mirror_url(session, release.file_id)
        report(f"Resolved ModDB mirror for file #{release.file_id}")

        with session.get(
            mirror,
            headers={"Referer": f"{MODDB_BASE}/downloads/start/{release.file_id}"},
            stream=True,
            timeout=60,
        ) as response:
            response.raise_for_status()

            total = int(response.headers.get("Content-Length") or 0)
            if total:
                free = shutil.disk_usage(staging_dir).free
                needed = total * FREE_SPACE_FACTOR
                if free < needed:
                    raise RuntimeError(
                        f"Not enough free disk space: need ~{needed // 1024 // 1024} MB "
                        f"(download + extract + backup), have {free // 1024 // 1024} MB."
                    )

            filename = _filename_from_response(response, release.file_id)
            target = staging_dir / filename
            partial = target.with_suffix(target.suffix + ".part")

            report(
                f"Downloading {filename}"
                + (f" ({total // 1024 // 1024} MB)" if total else "")
            )

            written = 0
            next_report = 25
            with open(partial, "wb") as handle:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
                    if total and (written / total) * 100 >= next_report:
                        report(f"Download {next_report}% complete")
                        next_report += 25

            partial.replace(target)
            report(f"Downloaded {filename} ({written // 1024 // 1024} MB)")
            return target
