#!/usr/bin/env python3
"""One-time OAuth helper to mint a Spotify refresh token.

Run this ON YOUR LOCAL MACHINE, not in any sandbox: Spotify needs to
redirect to a localhost URL that your browser can reach.

Setup:
  1. Register an app at https://developer.spotify.com/dashboard.
  2. In the app's settings, add this exact Redirect URI:
         http://127.0.0.1:8765/callback
  3. Export your app credentials:
         export SPOTIFY_CLIENT_ID=...
         export SPOTIFY_CLIENT_SECRET=...
  4. Run:
         python3 spotify_oauth_helper.py
  5. Approve the consent screen in your browser.
  6. Copy the refresh token printed to your terminal.

Scopes requested: playlist-modify-private, playlist-read-private, user-read-private.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

REDIRECT_URI = "http://127.0.0.1:8765/callback"
SCOPES = "playlist-modify-private playlist-read-private user-read-private"
STATE = secrets.token_urlsafe(16)


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing env var: {name}")
    return v


CLIENT_ID = _require("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = _require("SPOTIFY_CLIENT_SECRET")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
    "client_id": CLIENT_ID,
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
    "scope": SCOPES,
    "state": STATE,
    "show_dialog": "true",
})

result: dict[str, str] = {}


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)

        if params.get("state", [None])[0] != STATE:
            self._respond(400, "State mismatch (possible CSRF). Restart the helper.")
            result["error"] = "state_mismatch"
            return

        if "error" in params:
            err = params["error"][0]
            self._respond(400, f"Spotify returned error: {err}")
            result["error"] = err
            return

        code = params["code"][0]
        creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }).encode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=body,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                tokens = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            self._respond(500, f"Token exchange failed: {err_body}")
            result["error"] = err_body
            return

        refresh = tokens.get("refresh_token")
        if not refresh:
            self._respond(500, "Spotify did not return a refresh token. Try again with show_dialog=true.")
            result["error"] = "no_refresh_token"
            return

        result["refresh_token"] = refresh
        self._respond(200, "All set. You can close this tab. Refresh token printed in your terminal.")

    def _respond(self, status: int, message: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<h1>{'OK' if status == 200 else 'Error'}</h1><p>{message}</p>".encode())

    def log_message(self, *_args, **_kwargs):
        return


def main() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 8765), CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"Opening browser. If it does not open, paste this URL manually:\n  {AUTHORIZE_URL}\n")
    webbrowser.open(AUTHORIZE_URL)

    thread.join(timeout=300)
    server.server_close()

    if "error" in result:
        sys.exit(f"Failed: {result['error']}")
    if "refresh_token" not in result:
        sys.exit("Timed out waiting for the redirect (5 minutes). Re-run the helper.")

    print()
    print("==== Refresh token (copy this, then paste it back) ====")
    print(result["refresh_token"])
    print("=======================================================")


if __name__ == "__main__":
    main()
