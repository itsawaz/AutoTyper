"""One-time helper to mint a read-only Gmail refresh token.

Run this once on your own machine. It opens a browser, asks you to grant
read-only Gmail access, and prints a GMAIL_REFRESH_TOKEN you paste into env
(locally and in Vercel). No heavy Google libraries required.

Prereqs (in Google Cloud Console):
  1. Create a project and enable the "Gmail API".
  2. Create an OAuth 2.0 Client ID of type "Desktop app".
  3. Note the client id and client secret.

Usage:
  GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... python get_gmail_token.py
"""
from __future__ import annotations

import http.server
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

import httpx

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/"


def main() -> int:
    client_id = os.environ.get("GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in the environment.")
        return 1

    state = secrets.token_urlsafe(16)
    holder: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            holder["code"] = params.get("code", [""])[0]
            holder["state"] = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;text-align:center;"
                b"padding:40px'><h2>Done. You can close this tab and return to "
                b"the terminal.</h2></body></html>"
            )
            done.set()

        def log_message(self, *args):
            pass  # quiet

    server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    auth_params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",       # needed to get a refresh token
        "prompt": "consent",            # force refresh token on re-auth
        "state": state,
    }
    url = AUTH_URL + "?" + urllib.parse.urlencode(auth_params)
    print("Opening your browser to authorize read-only Gmail access...")
    print("If it doesn't open, visit:\n", url)
    webbrowser.open(url)

    if not done.wait(timeout=300):
        print("Timed out waiting for authorization.")
        return 1
    server.shutdown()

    if holder.get("state") != state:
        print("State mismatch — aborting for safety.")
        return 1
    code = holder.get("code")
    if not code:
        print("No authorization code received.")
        return 1

    resp = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    refresh = data.get("refresh_token")
    if not refresh:
        print("No refresh token returned. Revoke the app's access at "
              "https://myaccount.google.com/permissions and run again.")
        return 1

    print("\n✅ Success. Add this to your env (locally and in Vercel):\n")
    print(f"GMAIL_REFRESH_TOKEN={refresh}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
