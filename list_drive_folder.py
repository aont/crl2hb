#!/usr/bin/env python3
"""List files and folders under a specified Google Drive folder ID."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import httpx
import tomllib

GOOGLE_DRIVE_FILES_API = "https://www.googleapis.com/drive/v3/files"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder-id", required=True, help="Google Drive folder ID to list.")
    parser.add_argument(
        "--config-file",
        type=Path,
        default=Path("config.toml"),
        help="Path to TOML config file containing Google OAuth client credentials.",
    )
    parser.add_argument(
        "--google-token-file",
        type=Path,
        default=Path("google_token.json"),
        help="Path to Google OAuth token JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw list in JSON format instead of a plain text table.",
    )
    return parser.parse_args()


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}. "
            "Create a TOML config file with [google] credentials."
        )
    with path.open("rb") as fp:
        return tomllib.load(fp)


def resolve_google_credentials(config: dict) -> tuple[str, str]:
    google = config.get("google", {})
    client_id = str(google.get("client_id", "")).strip()
    client_secret = str(google.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing Google credentials in config TOML. "
            "Set [google].client_id and [google].client_secret."
        )
    return client_id, client_secret


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


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


def list_folder_children(session: httpx.Client, access_token: str, folder_id: str) -> list[dict]:
    page_token: str | None = None
    items: list[dict] = []
    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id,name,mimeType,size,modifiedTime)",
            "orderBy": "folder,name_natural",
            "pageSize": "1000",
        }
        if page_token:
            params["pageToken"] = page_token

        response = session.get(GOOGLE_DRIVE_FILES_API, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()

        items.extend(payload.get("files", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return items


def print_table(items: list[dict]) -> None:
    if not items:
        print("No files/folders found.")
        return

    print(f"Total: {len(items)}")
    print("TYPE\tNAME\tID\tSIZE\tMODIFIED")
    for item in items:
        kind = "FOLDER" if item.get("mimeType") == FOLDER_MIME else "FILE"
        size = item.get("size", "-")
        modified = item.get("modifiedTime", "-")
        print(f"{kind}\t{item.get('name', '')}\t{item.get('id', '')}\t{size}\t{modified}")


def main() -> int:
    args = parse_args()

    config = load_config(args.config_file)
    google_client_id, google_client_secret = resolve_google_credentials(config)

    google_token = load_json(args.google_token_file, default={})
    if "access_token" not in google_token and "refresh_token" not in google_token:
        raise SystemExit(
            f"Invalid Google token file: {args.google_token_file}. "
            "Run get_google_token.py first."
        )

    with httpx.Client() as session:
        access_token = ensure_google_access_token(
            session,
            google_token,
            google_client_id,
            google_client_secret,
            args.google_token_file,
        )
        items = list_folder_children(session, access_token, args.folder_id)

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print_table(items)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
