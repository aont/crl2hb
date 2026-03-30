#!/usr/bin/env python3
"""Interactive CLI tool to obtain Hatena OAuth access token and save token.json."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import aiohttp
from authlib.oauth1.rfc5849 import Client as OAuth1Client

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
    parser.add_argument(
        "--callback",
        default="oob",
        help=(
            "OAuth callback URI sent to Hatena during request-token exchange "
            "(default: oob)."
        ),
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


def oauth_client(
    consumer_key: str,
    consumer_secret: str,
    token: str | None = None,
    token_secret: str | None = None,
    verifier: str | None = None,
    callback_uri: str | None = None,
) -> OAuth1Client:
    return OAuth1Client(
        client_id=consumer_key,
        client_secret=consumer_secret,
        token=token,
        token_secret=token_secret,
        verifier=verifier,
        redirect_uri=callback_uri,
    )


def parse_oauth_form(body: str) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


async def request_oauth_form(
    session: aiohttp.ClientSession,
    oauth: OAuth1Client,
    method: str,
    url: str,
    query: dict[str, str] | None = None,
) -> dict[str, str]:
    endpoint = f"{url}?{urllib.parse.urlencode(query)}" if query else url
    signed_uri, signed_headers, _ = oauth.sign(endpoint, http_method=method)
    async with session.request(method, signed_uri, headers=signed_headers) as response:
        text = await response.text()
        if response.status >= 400:
            raise RuntimeError(f"{response.status} {response.reason}: {text}")
        return parse_oauth_form(text)


async def async_main() -> int:
    args = parse_args()
    consumer_key, consumer_secret = resolve_credentials(args)

    oauth = oauth_client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        callback_uri=args.callback,
    )

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            request_token = await request_oauth_form(
                session,
                oauth,
                "POST",
                REQUEST_TOKEN_URL,
                query={"scope": args.scope},
            )
    except Exception as exc:
        raise SystemExit(f"Failed to fetch request token: {exc}") from exc

    resource_owner_key = request_token.get("oauth_token")
    resource_owner_secret = request_token.get("oauth_token_secret")
    if not resource_owner_key or not resource_owner_secret:
        raise SystemExit("Unexpected request token response from Hatena.")

    authorization_url = (
        f"{AUTHORIZE_URL}?{urllib.parse.urlencode({'oauth_token': resource_owner_key})}"
    )
    print("Open the following URL and authorize the app:")
    print(authorization_url)
    if args.open_browser:
        webbrowser.open(authorization_url)

    verifier = input("Enter oauth_verifier: ").strip()
    if not verifier:
        raise SystemExit("oauth_verifier is required.")

    authed = oauth_client(
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
        token=resource_owner_key,
        token_secret=resource_owner_secret,
        verifier=verifier,
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            access_token = await request_oauth_form(session, authed, "POST", ACCESS_TOKEN_URL)
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
    sys.exit(asyncio.run(async_main()))
