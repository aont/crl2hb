#!/usr/bin/env python3
"""Import Google Takeout Reading List bookmarks into Hatena Bookmark.

Workflow:
1. List new takeout ZIP files from a Google Drive folder via Drive API.
2. Download each ZIP and read /Takeout/Chrome/リーディング リスト.html.
3. Extract links from <A HREF="...">title</A> entries.
4. Add each URL to Hatena Bookmark as private with comment "[あとで読む]".
5. Skip URLs already bookmarked in Hatena.

This script requires:
- a pre-generated Hatena OAuth token JSON file
- a Google OAuth token JSON file with Drive read permissions
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

import httpx
from authlib.integrations.httpx_client import OAuth1Auth

HATENA_BOOKMARK_API = "https://bookmark.hatenaapis.com/rest/1/my/bookmark"
READING_LIST_HTML = "Takeout/Chrome/リーディング リスト.html"
DEFAULT_COMMENT = "[あとで読む]"
GOOGLE_DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


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
        "--drive-folder-id",
        default=None,
        help=(
            "Google Drive folder ID that contains takeout-*.zip files. "
            "If omitted, this script searches My Drive root for a folder named 'Takeout'."
        ),
    )
    parser.add_argument(
        "--google-token-file",
        type=Path,
        default=Path("google_token.json"),
        help="Path to Google OAuth token JSON.",
    )
    parser.add_argument(
        "--google-client-id",
        default=None,
        help="Google OAuth client ID (defaults to GOOGLE_CLIENT_ID env if set).",
    )
    parser.add_argument(
        "--google-client-secret",
        default=None,
        help="Google OAuth client secret (defaults to GOOGLE_CLIENT_SECRET env if set).",
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


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


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


def extract_urls_from_zip_bytes(zip_bytes: bytes, label: str) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
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


def ensure_google_access_token(
    session: httpx.Client,
    token: dict,
    client_id: str,
    client_secret: str,
    token_file: Path,
) -> str:
    access_token = token.get("access_token")
    expires_at = token.get("expires_at")
    now = int(time.time())

    if access_token and isinstance(expires_at, (int, float)) and now < int(expires_at) - 60:
        return access_token
    if access_token and expires_at is None:
        return access_token

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "Google access token is expired and refresh_token is missing. "
            "Re-run get_google_token.py with prompt=consent."
        )

    response = session.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    response.raise_for_status()
    refreshed = response.json()

    token["access_token"] = refreshed["access_token"]
    token["token_type"] = refreshed.get("token_type", token.get("token_type", "Bearer"))
    if "expires_in" in refreshed:
        token["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
    if "refresh_token" in refreshed:
        token["refresh_token"] = refreshed["refresh_token"]

    save_json(token_file, token)
    return token["access_token"]


def drive_file_signature(item: dict) -> str:
    return "::".join(
        [
            item.get("id", ""),
            str(item.get("size", "")),
            item.get("md5Checksum", ""),
            item.get("modifiedTime", ""),
        ]
    )


def list_takeout_zip_files(
    session: httpx.Client,
    access_token: str,
    folder_id: str,
) -> list[dict]:
    files: list[dict] = []
    page_token: str | None = None
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        params = {
            "q": query,
            "fields": "nextPageToken, files(id,name,mimeType,size,md5Checksum,modifiedTime)",
            "orderBy": "modifiedTime desc",
            "pageSize": "100",
        }
        if page_token:
            params["pageToken"] = page_token

        response = session.get(
            GOOGLE_DRIVE_FILES_API,
            params=params,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("files", []):
            name = str(item.get("name", ""))
            mime_type = str(item.get("mimeType", ""))
            if "takeout-" not in name.lower():
                continue
            if not name.lower().endswith(".zip") and mime_type not in {"application/zip", "application/x-zip-compressed"}:
                continue
            files.append(item)
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return files


def find_takeout_folder_id_under_root(session: httpx.Client, access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "q": (
            "'root' in parents and trashed = false and "
            "mimeType = 'application/vnd.google-apps.folder' and name = 'Takeout'"
        ),
        "fields": "files(id,name,modifiedTime), nextPageToken",
        "orderBy": "modifiedTime desc",
        "pageSize": "10",
    }
    response = session.get(
        GOOGLE_DRIVE_FILES_API,
        params=params,
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    files = response.json().get("files", [])
    if not files:
        raise SystemExit(
            "Could not find folder named 'Takeout' directly under My Drive root. "
            "Specify --drive-folder-id explicitly."
        )
    if len(files) > 1:
        logging.warning(
            "Multiple 'Takeout' folders found under root. Using most recently modified: %s (%s)",
            files[0].get("name", "Takeout"),
            files[0].get("id", "-"),
        )
    return str(files[0]["id"])


def download_drive_file(session: httpx.Client, access_token: str, file_id: str) -> bytes:
    headers = {"Authorization": f"Bearer {access_token}"}
    response = session.get(
        f"{GOOGLE_DRIVE_FILES_API}/{file_id}",
        params={"alt": "media"},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return response.content


def iter_urls_from_new_drive_zips(
    session: httpx.Client,
    access_token: str,
    new_files: Iterable[dict],
) -> tuple[list[str], dict[str, list[str]]]:
    all_urls: list[str] = []
    per_zip: dict[str, list[str]] = {}
    for item in new_files:
        file_label = f"{item.get('name', '(unknown)')} ({item.get('id', '-')})"
        zip_bytes = download_drive_file(session, access_token, item["id"])
        urls = extract_urls_from_zip_bytes(zip_bytes, file_label)
        per_zip[file_label] = urls
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
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    consumer_key = args.consumer_key or os.getenv("HATENA_CONSUMER_KEY", "")
    consumer_secret = args.consumer_secret or os.getenv("HATENA_CONSUMER_SECRET", "")
    if not consumer_key or not consumer_secret:
        raise SystemExit(
            "Missing consumer key/secret. Pass --consumer-key/--consumer-secret "
            "or set HATENA_CONSUMER_KEY/HATENA_CONSUMER_SECRET."
        )

    google_client_id = args.google_client_id or os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret = args.google_client_secret or os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not google_client_id or not google_client_secret:
        raise SystemExit(
            "Missing Google client ID/secret. Pass --google-client-id/--google-client-secret "
            "or set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET."
        )

    token = load_json(args.token_file, default={})
    if "oauth_token" not in token or "oauth_token_secret" not in token:
        raise SystemExit(f"Invalid Hatena token file: {args.token_file}")

    google_token = load_json(args.google_token_file, default={})
    if "access_token" not in google_token and "refresh_token" not in google_token:
        raise SystemExit(
            f"Invalid Google token file: {args.google_token_file}. "
            "Run get_google_token.py first."
        )

    state_db = open_state_db(args.state_db)
    processed_zip_signatures = load_processed_zip_signatures(state_db)
    logging.debug("State DB: %s", args.state_db.resolve())
    logging.debug("Loaded %d processed ZIP signatures", len(processed_zip_signatures))

    auth = make_auth(consumer_key, consumer_secret, token)
    created = 0
    skipped_existing = 0

    with httpx.Client() as session:
        access_token = ensure_google_access_token(
            session,
            google_token,
            google_client_id,
            google_client_secret,
            args.google_token_file,
        )
        drive_folder_id = args.drive_folder_id
        if not drive_folder_id:
            drive_folder_id = find_takeout_folder_id_under_root(session, access_token)
            logging.info(
                "Using auto-detected Drive folder 'Takeout' under root: %s",
                drive_folder_id,
            )

        all_files = list_takeout_zip_files(session, access_token, drive_folder_id)
        logging.debug("Found %d takeout ZIP candidate files in Drive folder", len(all_files))
        new_files = [f for f in all_files if drive_file_signature(f) not in processed_zip_signatures]
        logging.debug("Detected %d new takeout ZIP files after state filter", len(new_files))
        if not new_files:
            logging.info("No new takeout ZIP files found in Drive folder %s", drive_folder_id)
            state_db.close()
            return 0

        urls, details = iter_urls_from_new_drive_zips(session, access_token, new_files)
        for file_label, zurls in details.items():
            logging.info("%s: extracted %d URL(s)", file_label, len(zurls))

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

    processed = set(processed_zip_signatures)
    for item in new_files:
        processed.add(drive_file_signature(item))
    save_processed_zip_signatures(state_db, processed)
    state_db.close()

    logging.info("Done. created=%d skipped_existing=%d", created, skipped_existing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
