#!/usr/bin/env python3
"""Interactive CLI tool to obtain Hatena OAuth access token and save token.json."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from authlib.integrations.httpx_client import OAuth1Client
import tomllib

REQUEST_TOKEN_URL = "https://www.hatena.com/oauth/initiate"
AUTHORIZE_URL = "https://www.hatena.ne.jp/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.hatena.com/oauth/token"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-file",
        type=Path,
        default=Path("config.toml"),
        help="Path to TOML config file containing Hatena OAuth client credentials.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("hatena_token.json"),
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
        default="auto",
        help=(
            "OAuth callback URI sent to Hatena during request-token exchange "
            '(default: auto, which starts a local callback server; use "oob" for manual verifier input).'
        ),
    )
    parser.add_argument(
        "--callback-timeout",
        type=int,
        default=300,
        help="Seconds to wait for OAuth callback before timing out (default: 300).",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}. "
            "Create a TOML config file with [hatena] credentials."
        )
    with path.open("rb") as fp:
        return tomllib.load(fp)


def resolve_credentials(config: dict) -> tuple[str, str]:
    hatena = config.get("hatena", {})
    consumer_key = str(hatena.get("consumer_key", "")).strip()
    consumer_secret = str(hatena.get("consumer_secret", "")).strip()
    if not consumer_key or not consumer_secret:
        raise SystemExit(
            "Missing Hatena credentials in config TOML. "
            "Set [hatena].consumer_key and [hatena].consumer_secret."
        )
    return consumer_key, consumer_secret


def save_token(path: Path, token: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(token, fp, ensure_ascii=False, indent=2)


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "OAuthCallbackServer"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        # Silence default HTTP request logging to keep CLI output clean.
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        verifier = query.get("oauth_verifier", [None])[0]
        denied = query.get("oauth_problem", [None])[0]
        if verifier:
            self.server.oauth_verifier = verifier
            self.server.callback_received.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authorization complete</h1>"
                b"<p>You can close this window and return to the terminal.</p>"
                b"</body></html>"
            )
            return
        if denied:
            self.server.oauth_problem = denied
            self.server.callback_received.set()
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Authorization failed</h1>"
            b"<p>Missing oauth_verifier in callback parameters.</p>"
            b"</body></html>"
        )


class OAuthCallbackServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, OAuthCallbackHandler)
        self.oauth_verifier: str | None = None
        self.oauth_problem: str | None = None
        self.callback_received = threading.Event()


def build_callback_server() -> tuple[OAuthCallbackServer, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    server = OAuthCallbackServer((host, port))
    callback_uri = f"http://{host}:{port}/callback"
    return server, callback_uri


def wait_for_verifier(server: OAuthCallbackServer, timeout: int) -> str:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not server.callback_received.wait(timeout=timeout):
            raise SystemExit(
                f"Timed out waiting for OAuth callback after {timeout} seconds."
            )
        if server.oauth_problem:
            raise SystemExit(f"Authorization failed: {server.oauth_problem}")
        if not server.oauth_verifier:
            raise SystemExit("OAuth callback did not include oauth_verifier.")
        return server.oauth_verifier
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    args = parse_args()
    config = load_config(args.config_file)
    consumer_key, consumer_secret = resolve_credentials(config)
    callback_server: OAuthCallbackServer | None = None
    redirect_uri = args.callback
    if args.callback == "auto":
        callback_server, redirect_uri = build_callback_server()

    try:
        with OAuth1Client(
            client_id=consumer_key,
            client_secret=consumer_secret,
            redirect_uri=redirect_uri,
        ) as oauth:
            request_token = oauth.fetch_request_token(
                REQUEST_TOKEN_URL,
                params={"scope": args.scope},
            )
            authorization_url = oauth.create_authorization_url(AUTHORIZE_URL)
    except Exception as exc:
        raise SystemExit(f"Failed to fetch request token: {exc}") from exc

    resource_owner_key = request_token.get("oauth_token")
    resource_owner_secret = request_token.get("oauth_token_secret")
    if not resource_owner_key or not resource_owner_secret:
        raise SystemExit("Unexpected request token response from Hatena.")

    auth_url = authorization_url["url"] if isinstance(authorization_url, dict) else str(authorization_url)
    print("Open the following URL and authorize the app:")
    print(auth_url)
    if args.open_browser:
        webbrowser.open(auth_url)
    if callback_server:
        print(
            "Waiting for OAuth callback on "
            f"{redirect_uri} (timeout: {args.callback_timeout}s)..."
        )
        verifier = wait_for_verifier(callback_server, args.callback_timeout)
        print("Received oauth_verifier from callback.")
    else:
        verifier = input("Enter oauth_verifier: ").strip()
        if not verifier:
            raise SystemExit("oauth_verifier is required.")

    try:
        with OAuth1Client(
            client_id=consumer_key,
            client_secret=consumer_secret,
            token={
                "oauth_token": resource_owner_key,
                "oauth_token_secret": resource_owner_secret,
            },
            verifier=verifier,
        ) as authed:
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
