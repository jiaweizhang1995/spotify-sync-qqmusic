"""MusicBrainz artist alias lookup with a global 1 req/s rate limiter.

Used to bridge cross-language artist names (Jay Chou <-> 周杰伦 etc.) when the
title+artist match falls below threshold. Degrades gracefully: any network or
HTTP failure collapses to returning [original_name] so the sync never crashes.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from src import db

log = logging.getLogger(__name__)

API_BASE = "https://musicbrainz.org/ws/2"
MIN_INTERVAL_SECONDS = 1.0  # MB hard cap: 1 req/s per IP
REQUEST_TIMEOUT = 30
DEFAULT_USER_AGENT = "spotify-qq-sync/0.2 (CarfagnoArcino@gmail.com)"


class _RateLimiter:
    """Token-bucket style: at most one HTTP call per `min_interval` across threads."""

    def __init__(self, min_interval: float = MIN_INTERVAL_SECONDS):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval


class MusicBrainzClient:
    def __init__(
        self,
        user_agent: str,
        cache_conn: sqlite3.Connection,
        max_workers: int = 4,
    ):
        if not user_agent:
            raise ValueError("MusicBrainz requires a non-empty User-Agent")
        self.user_agent = user_agent
        self.cache_conn = cache_conn
        self.max_workers = max_workers
        self._limiter = _RateLimiter()
        self._cache_lock = threading.Lock()

    # ---------- HTTP primitives ----------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Rate-limited GET with one retry on transient failure. None on give-up."""
        url = f"{API_BASE}{path}"
        merged = dict(params)
        merged.setdefault("fmt", "json")
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        for attempt in range(2):
            self._limiter.acquire()
            try:
                resp = requests.get(
                    url, headers=headers, params=merged, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as e:
                log.warning("MB GET %s failed (attempt %d): %s", path, attempt, e)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    log.warning("MB GET %s returned non-JSON", path)
                    return None
            if resp.status_code == 404:
                return None
            log.warning(
                "MB GET %s returned %d (attempt %d)", path, resp.status_code, attempt
            )
        return None

    # ---------- alias extraction ----------

    @staticmethod
    def _collect_aliases(original: str, artist_json: dict[str, Any]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []

        def add(v: str | None) -> None:
            if not v:
                return
            k = v.strip()
            if not k:
                return
            low = k.lower()
            if low in seen:
                return
            seen.add(low)
            out.append(k)

        add(original)
        add(artist_json.get("name"))
        add(artist_json.get("sort-name"))
        for a in artist_json.get("aliases", []) or []:
            if isinstance(a, dict):
                add(a.get("name"))
                add(a.get("sort-name"))
        return out

    # ---------- public lookups ----------

    def get_aliases_for_artist(self, name: str) -> list[str]:
        """Search MB by artist name, return alias list (dedup, case-insensitive).

        Cache hit bypasses HTTP. On any failure returns [name].
        """
        original = (name or "").strip()
        if not original:
            return []

        cached = self._cache_read(original)
        if cached is not None:
            return cached

        fallback = [original]
        search = self._get(
            "/artist",
            {"query": f'artist:"{original}"', "limit": 1},
        )
        if not search:
            self._cache_write(original, fallback, source="fallback")
            return fallback
        artists = search.get("artists") or []
        if not artists:
            self._cache_write(original, fallback, source="no-hit")
            return fallback
        mbid = artists[0].get("id")
        if not mbid:
            self._cache_write(original, fallback, source="no-mbid")
            return fallback

        detail = self._get(f"/artist/{mbid}", {"inc": "aliases"})
        if not detail:
            self._cache_write(original, fallback, source="detail-fail")
            return fallback

        aliases = self._collect_aliases(original, detail)
        self._cache_write(original, aliases, source="name-search")
        return aliases

    def get_aliases_for_isrc(self, isrc: str, spotify_artist: str) -> list[str]:
        """Lookup by ISRC first (more accurate than name search), fall back to name."""
        original = (spotify_artist or "").strip()
        if not isrc:
            return self.get_aliases_for_artist(original)

        cached = self._cache_read(original)
        if cached is not None:
            return cached

        fallback = [original] if original else []
        rec = self._get(f"/isrc/{isrc}", {"inc": "artists"})
        mbid = None
        if rec:
            for recording in rec.get("recordings", []) or []:
                for credit in recording.get("artist-credit", []) or []:
                    artist = credit.get("artist") if isinstance(credit, dict) else None
                    if isinstance(artist, dict) and artist.get("id"):
                        mbid = artist["id"]
                        break
                if mbid:
                    break

        if not mbid:
            # Fall back to name-based search (still caches under the same key).
            return self.get_aliases_for_artist(original)

        detail = self._get(f"/artist/{mbid}", {"inc": "aliases"})
        if not detail:
            self._cache_write(original, fallback, source="isrc-detail-fail")
            return fallback

        aliases = self._collect_aliases(original, detail)
        self._cache_write(original, aliases, source="isrc")
        return aliases

    def get_aliases_batch(
        self, tracks: list[dict], max_workers: int | None = None
    ) -> dict[str, list[str]]:
        """Pre-warm the alias cache for a batch of tracks.

        Dedupes on artist name, parallelizes HTTP via ThreadPoolExecutor. The
        global rate limiter still caps wire traffic to 1 req/s — threading
        mostly hides network latency overlap across artists.

        Each track: {"artist": str, "isrc": str | None}.
        Returns dict[artist_name -> aliases]. Artists missing from the input are
        not included.
        """
        workers = max_workers or self.max_workers

        # Dedupe by artist_key, but remember the first (artist, isrc) pair we saw.
        seen: dict[str, tuple[str, str | None]] = {}
        for t in tracks:
            artist = (t.get("artist") or "").strip()
            if not artist:
                continue
            key = artist.lower()
            if key in seen:
                continue
            seen[key] = (artist, t.get("isrc"))

        if not seen:
            return {}

        def resolve(pair: tuple[str, str | None]) -> tuple[str, list[str]]:
            artist, isrc = pair
            if isrc:
                return artist, self.get_aliases_for_isrc(isrc, artist)
            return artist, self.get_aliases_for_artist(artist)

        out: dict[str, list[str]] = {}
        if workers <= 1 or len(seen) == 1:
            for pair in seen.values():
                artist, aliases = resolve(pair)
                out[artist] = aliases
            return out

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for artist, aliases in pool.map(resolve, list(seen.values())):
                out[artist] = aliases
        return out

    # ---------- cache plumbing (serialized; sqlite3 is single-writer) ----------

    def _cache_read(self, key: str) -> list[str] | None:
        with self._cache_lock:
            return db.alias_cache_get(self.cache_conn, key)

    def _cache_write(self, key: str, aliases: list[str], source: str) -> None:
        with self._cache_lock:
            db.alias_cache_put(self.cache_conn, key, aliases, source=source)
