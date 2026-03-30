#!/usr/bin/env python3
"""Interactive CLI tool to obtain Hatena OAuth access token and save token.json."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

from requests_oauthlib import OAuth1Session

REQUEST_TOKEN_URL = "https://www.hatena.com/oauth/initiate"
AUTHORIZE_URL = "https://www.hatena.ne.jp/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.hatena.com/oauth/token"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--consumer-key",
        default=None,
        help="Hatena OAuth consumer key (defaults to HATENA_CONSUMER_KEY env).",
    )
    parser.add_argument(
        "--consumer-secret",
        default=None,
        help="Hatena OAuth consumer secret (defaults to HATENA_CONSUMER_SECRET env).",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("token.json"),
        help="Destination path for the generated token JSON.",
    )
    parser.add_argument(
        "--scope",
        default="read_public,write_public,read_private,write_private",
        help="Comma-separated Hatena OAuth scope(s).",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open authorization URL in your default browser automatically.",
    )
    return parser.parse_args()


def resolve_credentials(args: argparse.Namespace) -> tuple[str, str]:
    consumer_key = args.consumer_key or os.getenv("HATENA_CONSUMER_KEY", "")
    consumer_secret = args.consumer_secret or os.getenv("HATENA_CONSUMER_SECRET", "")
    if not consumer_key or not consumer_secret:
        raise SystemExit(
            "Missing consumer key/secret. Pass --consumer-key/--consumer-secret "
            "or set HATENA_CONSUMER_KEY/HATENA_CONSUMER_SECRET."
        )
    return consumer_key, consumer_secret


def save_token(path: Path, token: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(token, fp, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    consumer_key, consumer_secret = resolve_credentials(args)

    oauth = OAuth1Session(client_key=consumer_key, client_secret=consumer_secret)

    try:
        request_token = oauth.fetch_request_token(REQUEST_TOKEN_URL, params={"scope": args.scope})
    except Exception as exc:  # requests-oauthlib raises multiple exception types
        raise SystemExit(f"Failed to fetch request token: {exc}") from exc

    resource_owner_key = request_token.get("oauth_token")
    resource_owner_secret = request_token.get("oauth_token_secret")
    if not resource_owner_key or not resource_owner_secret:
        raise SystemExit("Unexpected request token response from Hatena.")

    authorization_url = oauth.authorization_url(AUTHORIZE_URL)
    print("Open the following URL and authorize the app:")
    print(authorization_url)
    if args.open_browser:
        webbrowser.open(authorization_url)

    verifier = input("Enter oauth_verifier: ").strip()
    if not verifier:
        raise SystemExit("oauth_verifier is required.")

    authed = OAuth1Session(
        client_key=consumer_key,
        client_secret=consumer_secret,
        resource_owner_key=resource_owner_key,
        resource_owner_secret=resource_owner_secret,
        verifier=verifier,
    )

    try:
        access_token = authed.fetch_access_token(ACCESS_TOKEN_URL)
    except Exception as exc:
        raise SystemExit(f"Failed to fetch access token: {exc}") from exc

    oauth_token = access_token.get("oauth_token")
    oauth_token_secret = access_token.get("oauth_token_secret")
    if not oauth_token or not oauth_token_secret:
        raise SystemExit("Unexpected access token response from Hatena.")

    token_payload = {
        "oauth_token": oauth_token,
        "oauth_token_secret": oauth_token_secret,
    }
    save_token(args.token_file, token_payload)

    print(f"Saved token to {args.token_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
