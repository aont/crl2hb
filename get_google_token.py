#!/usr/bin/env python3
"""Interactive CLI to obtain Google OAuth token for Drive API and save google_token.json."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "OAuthCallbackServer"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        error = query.get("error", [None])[0]
        if code:
            self.server.code = code
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

        self.server.error = error or "Missing code"
        self.server.callback_received.set()
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Authorization failed</h1>"
            b"<p>Authorization code was not provided.</p>"
            b"</body></html>"
        )


class OAuthCallbackServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, OAuthCallbackHandler)
        self.code: str | None = None
        self.error: str | None = None
        self.callback_received = threading.Event()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True, help="Google OAuth client ID.")
    parser.add_argument("--client-secret", required=True, help="Google OAuth client secret.")
    parser.add_argument(
        "--scope",
        default=DEFAULT_SCOPE,
        help="Space-separated OAuth scopes.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("google_token.json"),
        help="Destination path for Google token JSON.",
    )
    parser.add_argument(
        "--callback-timeout",
        type=int,
        default=300,
        help="Seconds to wait for callback (default: 300).",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open authorization URL in your default browser automatically.",
    )
    return parser.parse_args()


def build_callback_server() -> tuple[OAuthCallbackServer, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()

    server = OAuthCallbackServer((host, port))
    callback_uri = f"http://{host}:{port}/callback"
    return server, callback_uri


def wait_for_code(server: OAuthCallbackServer, timeout: int) -> str:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not server.callback_received.wait(timeout=timeout):
            raise SystemExit(f"Timed out waiting for OAuth callback after {timeout} seconds.")
        if server.error:
            raise SystemExit(f"Authorization failed: {server.error}")
        if not server.code:
            raise SystemExit("OAuth callback did not include code.")
        return server.code
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def save_token(path: Path, token: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        json.dump(token, fp, ensure_ascii=False, indent=2)


def main() -> int:
    args = parse_args()
    callback_server, redirect_uri = build_callback_server()

    auth_params = {
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": args.scope,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(auth_params)}"

    print("Open the following URL and authorize the app:")
    print(auth_url)
    if args.open_browser:
        webbrowser.open(auth_url)

    print(f"Waiting for OAuth callback on {redirect_uri} (timeout: {args.callback_timeout}s)...")
    code = wait_for_code(callback_server, args.callback_timeout)
    print("Received authorization code.")

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    token = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "scope": payload.get("scope", args.scope),
        "token_type": payload.get("token_type", "Bearer"),
    }
    if payload.get("expires_in"):
        token["expires_at"] = int(time.time()) + int(payload["expires_in"])

    if not token.get("access_token"):
        raise SystemExit("Unexpected token response: access_token is missing.")

    save_token(args.token_file, token)
    print(f"Saved token to {args.token_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
