#!/usr/bin/env python3
"""Import Google Takeout Reading List bookmarks into Hatena Bookmark.

Workflow:
1. Scan mounted Google Drive for new takeout ZIP files matching takeout-*.zip.
2. Read /takeout/Chrome/リーディング リスト.html from each ZIP.
3. Extract links from <A HREF="...">title</A> entries.
4. Add each URL to Hatena Bookmark as private with comment "[Read later]".
5. Skip URLs already bookmarked in Hatena.

This script requires a pre-generated OAuth token JSON file for Hatena.
"""

from __future__ import annotations

import argparse
import html.parser
import json
import logging
import os
from pathlib import Path
import time
from typing import Iterable
import urllib.parse
import zipfile

import httpx
from authlib.integrations.httpx_client import OAuth1Auth

HATENA_BOOKMARK_API = "https://bookmark.hatenaapis.com/rest/1/my/bookmark"
READING_LIST_HTML = "Takeout/Chrome/リーディング リスト.html"
DEFAULT_COMMENT = "[あとで読む]"


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
        "--takeout-dir",
        type=Path,
        default=Path("/path/to/gdrive/Takeout"),
        help="Directory containing takeout-*.zip files.",
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
        "--state-file",
        type=Path,
        default=Path(".takeout_to_hatena_state.json"),
        help="State file tracking already processed ZIP files.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Hatena.")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging.")
    return parser.parse_args()


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(value, fp, ensure_ascii=False, indent=2)


def zip_signature(path: Path) -> str:
    stat = path.stat()
    return f"{path.resolve()}::{stat.st_size}::{int(stat.st_mtime)}"


def discover_new_takeout_zips(takeout_dir: Path, state: dict) -> list[Path]:
    processed = set(state.get("processed_zip_signatures", []))
    result: list[Path] = []
    for zpath in sorted(takeout_dir.glob("takeout-*.zip")):
        sig = zip_signature(zpath)
        if sig not in processed:
            result.append(zpath)
    return result


def extract_urls_from_zip(zpath: Path) -> list[str]:
    with zipfile.ZipFile(zpath) as zf:
        try:
            content = zf.read(READING_LIST_HTML)
        except KeyError:
            logging.warning("%s does not contain %s", zpath, READING_LIST_HTML)
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
        except httpx.RequestError:
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


def iter_urls_from_new_zips(new_zips: Iterable[Path]) -> tuple[list[str], dict[str, list[str]]]:
    all_urls: list[str] = []
    per_zip: dict[str, list[str]] = {}
    for zpath in new_zips:
        urls = extract_urls_from_zip(zpath)
        per_zip[str(zpath)] = urls
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

    state = load_json(args.state_file, default={"processed_zip_signatures": []})

    new_zips = discover_new_takeout_zips(args.takeout_dir, state)
    if not new_zips:
        logging.info("No new ZIP files found in %s", args.takeout_dir)
        return 0

    urls, details = iter_urls_from_new_zips(new_zips)
    for zpath, zurls in details.items():
        logging.info("%s: extracted %d URL(s)", zpath, len(zurls))

    if not urls:
        logging.info("No valid HTTP(S) URLs extracted from new ZIP files.")
    else:
        logging.info("Total unique URLs to evaluate: %d", len(urls))

    auth = make_auth(consumer_key, consumer_secret, token)
    created = 0
    skipped_existing = 0

    with httpx.Client() as session:
        for url in urls:
            try:
                if is_bookmarked(session, auth, url):
                    skipped_existing += 1
                    logging.debug("Skip existing: %s", url)
                    continue

                if args.dry_run:
                    logging.info("[dry-run] would add: %s", url)
                else:
                    add_private_bookmark_with_retry(session, auth, url)
                    logging.info("Added: %s", url)
                created += 1
            except httpx.HTTPError as exc:
                logging.error("Failed for %s: %s", url, exc)

    # Mark all discovered ZIPs as processed even if they had no reading-list file.
    processed = set(state.get("processed_zip_signatures", []))
    for zpath in new_zips:
        processed.add(zip_signature(zpath))
    state["processed_zip_signatures"] = sorted(processed)
    save_json(args.state_file, state)

    logging.info("Done. created=%d skipped_existing=%d", created, skipped_existing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
