#!/usr/bin/env python3
"""Sync Chrome Reading List from Google Drive Takeout ZIPs into Hatena Bookmark.

Behavior:
- Finds new `/Takeout/takeout-*.zip` files on Google Drive.
- Reads `/takeout/Chrome/リーディング リスト.html` inside each ZIP.
- Extracts links in `<A HREF="url">title</A>` format.
- Registers each URL to Hatena Bookmark as a private bookmark with `[あとで読む]`.
- Skips URLs already bookmarked in Hatena Bookmark.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

LOGGER = logging.getLogger(__name__)

ZIP_NAME_PATTERN = re.compile(r"^takeout-.*\.zip$", re.IGNORECASE)
READING_LIST_HTML_PATH = "takeout/Chrome/リーディング リスト.html"
ANCHOR_PATTERN = re.compile(r'<A\s+HREF="([^"]+)"[^>]*>.*?</A>', re.IGNORECASE)


@dataclass(frozen=True)
class DriveZipFile:
    file_id: str
    name: str
    modified_time: str


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"processed_zip_ids": [], "processed_zip_names": []}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def has_processed(self, drive_zip: DriveZipFile) -> bool:
        return (
            drive_zip.file_id in self._data.get("processed_zip_ids", [])
            or drive_zip.name in self._data.get("processed_zip_names", [])
        )

    def mark_processed(self, drive_zip: DriveZipFile) -> None:
        ids = set(self._data.get("processed_zip_ids", []))
        names = set(self._data.get("processed_zip_names", []))
        ids.add(drive_zip.file_id)
        names.add(drive_zip.name)
        self._data["processed_zip_ids"] = sorted(ids)
        self._data["processed_zip_names"] = sorted(names)

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)


class GoogleDriveTakeoutClient:
    def __init__(self, service_account_file: str):
        creds = service_account.Credentials.from_service_account_file(
            service_account_file,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        self.service = build("drive", "v3", credentials=creds)

    def list_takeout_zip_files(self) -> list[DriveZipFile]:
        q = "mimeType='application/zip' and trashed=false"
        fields = "files(id,name,modifiedTime,parents),nextPageToken"
        files: list[dict] = []
        page_token = None
        while True:
            resp = (
                self.service.files()
                .list(
                    q=q,
                    fields=fields,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    pageSize=1000,
                )
                .execute()
            )
            files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        parent_ids = {pid for f in files for pid in f.get("parents", [])}
        parent_map = self._fetch_folder_names(parent_ids)

        result: list[DriveZipFile] = []
        for file in files:
            if not ZIP_NAME_PATTERN.match(file["name"]):
                continue
            parent_names = {parent_map.get(pid, "") for pid in file.get("parents", [])}
            if "Takeout" not in parent_names:
                continue
            result.append(
                DriveZipFile(
                    file_id=file["id"],
                    name=file["name"],
                    modified_time=file["modifiedTime"],
                )
            )

        result.sort(key=lambda x: x.modified_time)
        return result

    def _fetch_folder_names(self, folder_ids: set[str]) -> dict[str, str]:
        if not folder_ids:
            return {}
        ids = list(folder_ids)
        mapping: dict[str, str] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            query = " or ".join([f"id='{folder_id}'" for folder_id in chunk])
            resp = (
                self.service.files()
                .list(
                    q=f"({query}) and trashed=false",
                    fields="files(id,name,mimeType)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    pageSize=100,
                )
                .execute()
            )
            for file in resp.get("files", []):
                mapping[file["id"]] = file["name"]
        return mapping

    def download_zip(self, file_id: str) -> bytes:
        request = self.service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return fh.getvalue()


class HatenaBookmarkClient:
    """Simple client for Hatena Bookmark REST API.

    Uses OAuth access token in Authorization Bearer header.
    Endpoint defaults to Hatena Bookmark API v1 style route.
    """

    def __init__(self, access_token: str, endpoint_base: str = "https://bookmark.hatenaapis.com/rest/1"):
        self.endpoint_base = endpoint_base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": "takeout-reading-list-sync/1.0",
            }
        )

    def has_bookmark(self, url: str) -> bool:
        endpoint = f"{self.endpoint_base}/my/bookmark"
        resp = self.session.get(endpoint, params={"url": url}, timeout=30)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        data = resp.json()
        return bool(data and data.get("url"))

    def add_private_bookmark(self, url: str, comment: str = "[あとで読む]") -> None:
        endpoint = f"{self.endpoint_base}/my/bookmark"
        payload = {
            "url": url,
            "comment": comment,
            "private": "1",
        }
        resp = self.session.post(endpoint, data=payload, timeout=30)
        if resp.status_code == 409:
            LOGGER.info("Already bookmarked (409): %s", url)
            return
        resp.raise_for_status()


def extract_reading_list_urls(zip_blob: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zip_blob)) as zf:
        target_name = _find_reading_list_entry(zf.namelist())
        if not target_name:
            raise FileNotFoundError(f"{READING_LIST_HTML_PATH} not found in ZIP")
        raw_html = zf.read(target_name).decode("utf-8", errors="ignore")

    urls: list[str] = []
    for m in ANCHOR_PATTERN.finditer(raw_html):
        url = html.unescape(m.group(1)).strip()
        if url:
            urls.append(url)

    # Keep input order while removing duplicates.
    seen = set()
    deduped = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


def _find_reading_list_entry(zip_names: Iterable[str]) -> str | None:
    normalized_target = READING_LIST_HTML_PATH.lower().replace("\\", "/")
    for name in zip_names:
        normalized = name.lower().replace("\\", "/").lstrip("/")
        if normalized.endswith(normalized_target):
            return name
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service-account-file",
        default=os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"),
        help="Path to Google service account credentials JSON.",
    )
    parser.add_argument(
        "--hatena-access-token",
        default=os.environ.get("HATENA_ACCESS_TOKEN"),
        help="Hatena API OAuth access token.",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("STATE_FILE", ".state/synced_takeout_files.json"),
        help="Local state file path storing processed ZIP IDs.",
    )
    parser.add_argument(
        "--hatena-endpoint-base",
        default=os.environ.get("HATENA_ENDPOINT_BASE", "https://bookmark.hatenaapis.com/rest/1"),
        help="Base URL for Hatena Bookmark API.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Parse only; do not post to Hatena Bookmark.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.hatena_access_token and not args.dry_run:
        LOGGER.error("--hatena-access-token (or HATENA_ACCESS_TOKEN) is required unless --dry-run is used")
        return 2

    state = StateStore(Path(args.state_file))
    drive = GoogleDriveTakeoutClient(args.service_account_file)
    hatena = None if args.dry_run else HatenaBookmarkClient(args.hatena_access_token, args.hatena_endpoint_base)

    all_zip_files = drive.list_takeout_zip_files()
    new_zip_files = [z for z in all_zip_files if not state.has_processed(z)]

    if not new_zip_files:
        LOGGER.info("No new takeout ZIP files found.")
        return 0

    LOGGER.info("Found %d new ZIP file(s).", len(new_zip_files))

    for z in new_zip_files:
        LOGGER.info("Processing ZIP: %s (%s)", z.name, z.file_id)
        try:
            zip_blob = drive.download_zip(z.file_id)
            urls = extract_reading_list_urls(zip_blob)
        except Exception as exc:
            LOGGER.exception("Failed to parse %s: %s", z.name, exc)
            continue

        LOGGER.info("Extracted %d reading-list URL(s) from %s", len(urls), z.name)

        for url in urls:
            if args.dry_run:
                LOGGER.info("[DRY-RUN] Would sync: %s", url)
                continue
            assert hatena is not None
            try:
                if hatena.has_bookmark(url):
                    LOGGER.info("Skip existing bookmark: %s", url)
                    continue
                hatena.add_private_bookmark(url, comment="[あとで読む]")
                LOGGER.info("Added private bookmark: %s", url)
            except requests.HTTPError as exc:
                LOGGER.error("Failed to sync URL %s: %s", url, exc)

        state.mark_processed(z)
        state.save()

    LOGGER.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
