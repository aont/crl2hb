#!/usr/bin/env python3
"""Import Google Takeout Reading List bookmarks into Hatena Bookmark.

Workflow:
1. Scan a Google Drive folder for new takeout ZIP files matching takeout-*.zip.
2. Read /takeout/Chrome/リーディング リスト.html from each ZIP.
3. Extract links from <A HREF="...">title</A> entries.
4. Add each URL to Hatena Bookmark as private with comment "[Read later]".
5. Skip URLs already bookmarked in Hatena.

This script requires a pre-generated OAuth token JSON file for Hatena.
"""

from __future__ import annotations

import argparse
import html.parser
import io
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Iterable
import urllib.parse
import zipfile

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
import httpx
from authlib.integrations.httpx_client import OAuth1Auth

HATENA_BOOKMARK_API = "https://bookmark.hatenaapis.com/rest/1/my/bookmark"
READING_LIST_HTML = "Takeout/Chrome/リーディング リスト.html"
DEFAULT_COMMENT = "[あとで読む]"
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
DEFAULT_DRIVE_SCOPES = "https://www.googleapis.com/auth/drive.readonly"


class AnchorExtractor(html.parser.HTMLParser):
    """Extract URLs from anchor tags."""

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_map = {k.lower(): v for k, v in attrs}
        href = attrs_map.get("href")
        if href:
            self.urls.append(href.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--google-drive-folder-id",
        required=True,
        help="Google Drive folder ID containing takeout-*.zip files.",
    )
    parser.add_argument(
        "--google-credentials-file",
        type=Path,
        default=None,
        help=(
            "Path to Google service account JSON. "
            "If omitted, uses Application Default Credentials."
        ),
    )
    parser.add_argument(
        "--google-drive-scopes",
        default=DEFAULT_DRIVE_SCOPES,
        help="Comma-separated Google Drive API scope(s).",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("token.json"),
        help="Path to Hatena OAuth token JSON.",
    )
    parser.add_argument(
        "--consumer-key",
        default=None,
        help="Hatena OAuth consumer key (defaults to HATENA_CONSUMER_KEY env if set).",
    )
    parser.add_argument(
        "--consumer-secret",
        default=None,
        help="Hatena OAuth consumer secret (defaults to HATENA_CONSUMER_SECRET env if set).",
    )
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path(".takeout_to_hatena_state.sqlite3"),
        help="SQLite DB tracking processed ZIP signatures and known bookmarked URLs.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Hatena.")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return parser.parse_args()


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_zip_signatures ("
        " signature TEXT PRIMARY KEY"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS known_bookmarked_urls ("
        " url TEXT PRIMARY KEY"
        ")"
    )
    conn.commit()
    return conn


def load_processed_zip_signatures(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT signature FROM processed_zip_signatures").fetchall()
    return {row[0] for row in rows}


def save_processed_zip_signatures(conn: sqlite3.Connection, signatures: Iterable[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO processed_zip_signatures(signature) VALUES (?)",
        ((sig,) for sig in signatures),
    )
    conn.commit()


def is_known_bookmarked_url(conn: sqlite3.Connection, url: str) -> bool:
    row = conn.execute("SELECT 1 FROM known_bookmarked_urls WHERE url = ?", (url,)).fetchone()
    return row is not None


def remember_bookmarked_url(conn: sqlite3.Connection, url: str) -> None:
    conn.execute("INSERT OR IGNORE INTO known_bookmarked_urls(url) VALUES (?)", (url,))
    conn.commit()


def resolve_google_token(credentials_file: Path | None, scopes: list[str]) -> str:
    if credentials_file:
        creds = service_account.Credentials.from_service_account_file(
            str(credentials_file),
            scopes=scopes,
        )
    else:
        creds, _ = google.auth.default(scopes=scopes)
    creds.refresh(Request())
    if not creds.token:
        raise SystemExit("Failed to acquire Google OAuth access token.")
    return creds.token


def list_takeout_zip_files(
    session: httpx.Client,
    google_token: str,
    folder_id: str,
) -> list[dict[str, str]]:
    query = (
        f"'{folder_id}' in parents and "
        "trashed = false and "
        "mimeType = 'application/zip' and "
        "name contains 'takeout-'"
    )
    params = {
        "q": query,
        "fields": "files(id,name,size,modifiedTime,md5Checksum),nextPageToken",
        "pageSize": "1000",
    }
    headers = {"Authorization": f"Bearer {google_token}"}

    files: list[dict[str, str]] = []
    next_page_token: str | None = None
    while True:
        if next_page_token:
            params["pageToken"] = next_page_token
        response = session.get(f"{GOOGLE_DRIVE_API}/files", params=params, headers=headers, timeout=30)
        response.raise_for_status()
        payload = response.json()
        files.extend(payload.get("files", []))
        next_page_token = payload.get("nextPageToken")
        if not next_page_token:
            break
    return sorted(files, key=lambda x: x.get("name", ""))


def file_signature(file_meta: dict[str, str]) -> str:
    return "::".join(
        [
            file_meta.get("id", ""),
            file_meta.get("name", ""),
            file_meta.get("size", ""),
            file_meta.get("modifiedTime", ""),
            file_meta.get("md5Checksum", ""),
        ]
    )


def discover_new_takeout_files(
    drive_files: list[dict[str, str]],
    processed: set[str],
) -> list[dict[str, str]]:
    return [f for f in drive_files if file_signature(f) not in processed]


def download_file_bytes(
    session: httpx.Client,
    google_token: str,
    file_id: str,
) -> bytes:
    headers = {"Authorization": f"Bearer {google_token}"}
    response = session.get(
        f"{GOOGLE_DRIVE_API}/files/{file_id}",
        params={"alt": "media"},
        headers=headers,
        timeout=120,
    )
    response.raise_for_status()
    return response.content


def extract_urls_from_zip_content(content: bytes, label: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        try:
            content = zf.read(READING_LIST_HTML)
        except KeyError:
            logging.warning("%s does not contain %s", label, READING_LIST_HTML)
            return []

    html_text = content.decode("utf-8", errors="replace")
    parser = AnchorExtractor()
    parser.feed(html_text)

    unique_urls: list[str] = []
    seen: set[str] = set()
    for raw_url in parser.urls:
        norm = normalize_url(raw_url)
        if not norm:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        unique_urls.append(norm)
    return unique_urls


def normalize_url(raw_url: str) -> str | None:
    url = raw_url.strip()
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def make_auth(consumer_key: str, consumer_secret: str, token: dict) -> OAuth1Auth:
    return OAuth1Auth(
        client_id=consumer_key,
        client_secret=consumer_secret,
        token=token["oauth_token"],
        token_secret=token["oauth_token_secret"],
    )


def is_bookmarked(session: httpx.Client, auth: OAuth1Auth, url: str) -> bool:
    response = session.get(HATENA_BOOKMARK_API, params={"url": url}, auth=auth, timeout=20)
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def is_bookmarked_with_retry(
    session: httpx.Client,
    auth: OAuth1Auth,
    url: str,
    retries: int = 4,
) -> bool:
    for attempt in range(retries + 1):
        try:
            return is_bookmarked(session, auth, url)
        except (httpx.RequestError, httpx.HTTPError):
            if attempt == retries:
                raise
            wait_seconds = attempt + 1
            logging.warning(
                "Failed to check bookmark (attempt %d/%d): %s. Retrying in %ds.",
                attempt + 1,
                retries + 1,
                url,
                wait_seconds,
            )
            time.sleep(wait_seconds)
    raise RuntimeError("Unreachable: retry loop exhausted without return or raise")


def add_private_bookmark(
    session: httpx.Client,
    auth: OAuth1Auth,
    url: str,
    comment: str = DEFAULT_COMMENT,
) -> None:
    params = {"url": url, "comment": comment, "private": "1"}
    response = session.post(HATENA_BOOKMARK_API, params=params, auth=auth, timeout=20)
    response.raise_for_status()


def add_private_bookmark_with_retry(
    session: httpx.Client,
    auth: OAuth1Auth,
    url: str,
    retries: int = 4,
) -> None:
    for attempt in range(retries + 1):
        try:
            add_private_bookmark(session, auth, url)
            return
        except (httpx.RequestError, httpx.HTTPError):
            if attempt == retries:
                raise
            wait_seconds = attempt + 1
            logging.warning(
                "Failed to add bookmark (attempt %d/%d): %s. Retrying in %ds.",
                attempt + 1,
                retries + 1,
                url,
                wait_seconds,
            )
            time.sleep(wait_seconds)


def iter_urls_from_new_zips(
    session: httpx.Client,
    google_token: str,
    new_files: Iterable[dict[str, str]],
) -> tuple[list[str], dict[str, list[str]]]:
    all_urls: list[str] = []
    per_zip: dict[str, list[str]] = {}
    for file_meta in new_files:
        file_id = file_meta.get("id", "")
        file_name = file_meta.get("name", "unknown.zip")
        if not file_id:
            continue
        label = f"{file_name} ({file_id})"
        zip_content = download_file_bytes(session, google_token, file_id)
        urls = extract_urls_from_zip_content(zip_content, label)
        per_zip[label] = urls
        all_urls.extend(urls)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in all_urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped, per_zip


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    # Hide noisy request-line logs such as `HTTP Request: ...` unless explicitly verbose.
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    consumer_key = args.consumer_key or os.getenv("HATENA_CONSUMER_KEY", "")
    consumer_secret = args.consumer_secret or os.getenv("HATENA_CONSUMER_SECRET", "")
    if not consumer_key or not consumer_secret:
        raise SystemExit(
            "Missing consumer key/secret. Pass --consumer-key/--consumer-secret "
            "or set via wrapper/environment."
        )

    token = load_json(args.token_file, default={})
    if "oauth_token" not in token or "oauth_token_secret" not in token:
        raise SystemExit(f"Invalid token file: {args.token_file}")

    drive_scopes = [scope.strip() for scope in args.google_drive_scopes.split(",") if scope.strip()]
    google_token = resolve_google_token(args.google_credentials_file, drive_scopes)

    state_db = open_state_db(args.state_db)
    processed_zip_signatures = load_processed_zip_signatures(state_db)
    auth = make_auth(consumer_key, consumer_secret, token)
    created = 0
    skipped_existing = 0

    with httpx.Client() as session:
        all_files = list_takeout_zip_files(session, google_token, args.google_drive_folder_id)
        new_files = discover_new_takeout_files(all_files, processed_zip_signatures)
        if not new_files:
            logging.info("No new ZIP files found in Google Drive folder: %s", args.google_drive_folder_id)
            state_db.close()
            return 0

        urls, details = iter_urls_from_new_zips(session, google_token, new_files)
        for label, zurls in details.items():
            logging.info("%s: extracted %d URL(s)", label, len(zurls))

        if not urls:
            logging.info("No valid HTTP(S) URLs extracted from new ZIP files.")
        else:
            logging.info("Total unique URLs to evaluate: %d", len(urls))

        for url in urls:
            try:
                if is_known_bookmarked_url(state_db, url):
                    skipped_existing += 1
                    logging.debug("Skip known bookmarked URL: %s", url)
                    continue

                if is_bookmarked_with_retry(session, auth, url):
                    remember_bookmarked_url(state_db, url)
                    skipped_existing += 1
                    logging.debug("Skip existing: %s", url)
                    continue

                if args.dry_run:
                    logging.info("[dry-run] would add: %s", url)
                else:
                    add_private_bookmark_with_retry(session, auth, url)
                    remember_bookmarked_url(state_db, url)
                    logging.info("Added: %s", url)
                created += 1
            except httpx.HTTPError as exc:
                logging.error("Failed for %s: %s", url, exc)

    # Mark all discovered ZIPs as processed even if they had no reading-list file.
    processed = set(processed_zip_signatures)
    for file_meta in new_files:
        processed.add(file_signature(file_meta))
    save_processed_zip_signatures(state_db, processed)
    state_db.close()

    logging.info("Done. created=%d skipped_existing=%d", created, skipped_existing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
