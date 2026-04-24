"""One-time Spotify OAuth bootstrap.

Run locally:
    SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... python scripts/bootstrap_spotify.py

Prints `SPOTIFY_REFRESH_TOKEN=...` on success. Register
http://127.0.0.1:8765/callback as a redirect URI in the Spotify app settings.

The OAuth core lives in `src/spotify_oauth.py` so it can be reused by the
interactive `spotify-sync setup` wizard.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow `from src.spotify_oauth import ...` when run as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.spotify_oauth import fetch_refresh_token  # noqa: E402


def main() -> int:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars.",
            file=sys.stderr,
        )
        return 1
    try:
        token = fetch_refresh_token(client_id, client_secret)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"SPOTIFY_REFRESH_TOKEN={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
