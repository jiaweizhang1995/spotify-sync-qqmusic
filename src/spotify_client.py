"""Spotify Web API client (sync, requests-based)."""

from __future__ import annotations

import time
from typing import Any

import requests

from .text_util import to_simplified

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
PLAYLIST_ITEM_FIELDS = (
    "items(item(id,name,duration_ms,artists(name),"
    "album(name),external_ids(isrc))),next"
)
MAX_RETRIES = 3


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _refresh_access_token(self) -> str:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            auth=(self.client_id, self.client_secret),
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._access_token = body["access_token"]
        self._token_expires_at = time.time() + int(body.get("expires_in", 3600)) - 60
        return self._access_token

    def _auth_header(self) -> dict[str, str]:
        if not self._access_token or time.time() >= self._token_expires_at:
            self._refresh_access_token()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES + 1):
            resp = requests.get(
                url, headers=self._auth_header(), params=params, timeout=30
            )
            if resp.status_code == 429:
                if attempt >= MAX_RETRIES:
                    resp.raise_for_status()
                retry_after = int(resp.headers.get("Retry-After", "1"))
                backoff = retry_after * (2 ** attempt)
                time.sleep(backoff)
                continue
            if resp.status_code == 401 and attempt == 0:
                self._refresh_access_token()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("exhausted retries")

    def list_playlists(self) -> list[dict[str, Any]]:
        """Return the user's Spotify playlists (paginated through `next`)."""
        out: list[dict[str, Any]] = []
        url: str | None = f"{API_BASE}/me/playlists"
        params: dict[str, Any] | None = {"limit": 50}
        while url:
            body = self._get(url, params=params)
            out.extend(body.get("items", []))
            url = body.get("next")
            params = None
        return out

    def find_playlist_by_name(self, name: str) -> dict[str, Any] | None:
        for pl in self.list_playlists():
            if pl.get("name") == name:
                return pl
        return None

    def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        url: str | None = f"{API_BASE}/playlists/{playlist_id}/items"
        params: dict[str, Any] | None = {
            "fields": PLAYLIST_ITEM_FIELDS,
            "limit": 100,
        }
        out: list[dict[str, Any]] = []
        while url:
            body = self._get(url, params=params)
            for item in body.get("items", []):
                track = item.get("item") or item.get("track")
                if track is None:
                    continue
                out.append(_normalize_track(track))
            url = body.get("next")
            params = None
        return out


def _normalize_track(track: dict[str, Any]) -> dict[str, Any]:
    # Traditional → Simplified so search queries match QQ's mainland catalog.
    artists = [
        to_simplified(a.get("name", "")) for a in track.get("artists", []) if a
    ]
    album = to_simplified((track.get("album") or {}).get("name", ""))
    title = to_simplified(track.get("name", ""))
    isrc = (track.get("external_ids") or {}).get("isrc")
    return {
        "id": track.get("id"),
        "title": title,
        "artists": artists,
        "album": album,
        "duration_ms": track.get("duration_ms", 0),
        "isrc": isrc,
    }
