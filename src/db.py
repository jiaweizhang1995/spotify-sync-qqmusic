"""SQLite schema + DAO helpers. stdlib sqlite3 only, ISO-8601 UTC timestamps."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS track_map_cache (
    spotify_track_id TEXT PRIMARY KEY,
    spotify_title TEXT,
    spotify_artist TEXT,
    spotify_isrc TEXT,
    qq_song_id INTEGER,
    qq_song_mid TEXT,
    qq_song_type INTEGER,
    qq_title TEXT,
    qq_artist TEXT,
    match_score REAL,
    match_method TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT,
    added_count INTEGER DEFAULT 0,
    removed_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS unmatched_tracks (
    spotify_track_id TEXT,
    title TEXT,
    artist TEXT,
    album TEXT,
    reason TEXT,
    created_at TEXT,
    PRIMARY KEY (spotify_track_id, created_at)
);

CREATE TABLE IF NOT EXISTS artist_alias_cache (
    artist_key TEXT PRIMARY KEY,
    aliases_json TEXT,
    source TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS playlist_snapshot (
    spotify_playlist_id TEXT PRIMARY KEY,
    snapshot_json TEXT,
    qq_dirid INTEGER,
    synced_at TEXT
);
"""

ALIAS_CACHE_TTL_DAYS = 30


def utc_now_iso() -> str:
    # ISO-8601 with trailing Z, second precision — matches workflow log style.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str) -> sqlite3.Connection:
    # check_same_thread=False so worker threads (e.g. MusicBrainzClient's
    # ThreadPoolExecutor) can use the same conn; callers must serialize writes.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


# ---------- track_map_cache ----------

_CACHE_COLS = (
    "spotify_track_id",
    "spotify_title",
    "spotify_artist",
    "spotify_isrc",
    "qq_song_id",
    "qq_song_mid",
    "qq_song_type",
    "qq_title",
    "qq_artist",
    "match_score",
    "match_method",
    "updated_at",
)


def cache_get(conn: sqlite3.Connection, spotify_id: str) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM track_map_cache WHERE spotify_track_id = ?",
        (spotify_id,),
    )
    return cur.fetchone()


def cache_get_many(
    conn: sqlite3.Connection, ids: Iterable[str]
) -> dict[str, sqlite3.Row]:
    ids = list(ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"SELECT * FROM track_map_cache WHERE spotify_track_id IN ({placeholders})",
        ids,
    )
    return {row["spotify_track_id"]: row for row in cur.fetchall()}


def cache_put(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    payload = {c: row.get(c) for c in _CACHE_COLS}
    if not payload.get("updated_at"):
        payload["updated_at"] = utc_now_iso()
    cols = ",".join(_CACHE_COLS)
    qs = ",".join("?" * len(_CACHE_COLS))
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO track_map_cache ({cols}) VALUES ({qs})",
            [payload[c] for c in _CACHE_COLS],
        )


# ---------- sync_runs ----------


def insert_run(conn: sqlite3.Connection, row: dict[str, Any] | None = None) -> int:
    row = dict(row or {})
    row.setdefault("started_at", utc_now_iso())
    row.setdefault("status", "running")
    cols = [
        "started_at",
        "finished_at",
        "status",
        "added_count",
        "removed_count",
        "skipped_count",
        "failed_count",
        "notes",
    ]
    values = [row.get(c) for c in cols]
    with conn:
        cur = conn.execute(
            f"INSERT INTO sync_runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            values,
        )
    return int(cur.lastrowid)


def finalize_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    added_count: int = 0,
    removed_count: int = 0,
    skipped_count: int = 0,
    failed_count: int = 0,
    notes: str | None = None,
    finished_at: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE sync_runs
               SET finished_at = ?,
                   status = ?,
                   added_count = ?,
                   removed_count = ?,
                   skipped_count = ?,
                   failed_count = ?,
                   notes = ?
             WHERE run_id = ?
            """,
            (
                finished_at or utc_now_iso(),
                status,
                added_count,
                removed_count,
                skipped_count,
                failed_count,
                notes,
                run_id,
            ),
        )


# ---------- unmatched_tracks ----------


def insert_unmatched(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> int:
    rows = list(rows)
    if not rows:
        return 0
    now = utc_now_iso()
    payload = []
    for r in rows:
        payload.append(
            (
                r.get("spotify_track_id"),
                r.get("title"),
                r.get("artist"),
                r.get("album"),
                r.get("reason"),
                r.get("created_at") or now,
            )
        )
    with conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO unmatched_tracks
              (spotify_track_id, title, artist, album, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            payload,
        )
    return len(payload)


# ---------- artist_alias_cache ----------


def _artist_key(name: str) -> str:
    return (name or "").strip().lower()


def alias_cache_get(
    conn: sqlite3.Connection,
    key: str,
    ttl_days: int = ALIAS_CACHE_TTL_DAYS,
) -> list[str] | None:
    """Return cached aliases for `key` or None on miss / expiry."""
    norm = _artist_key(key)
    cur = conn.execute(
        "SELECT aliases_json, updated_at FROM artist_alias_cache WHERE artist_key = ?",
        (norm,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    updated_at = row["updated_at"]
    if updated_at:
        try:
            ts = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            ts = None
        if ts is not None and datetime.now(timezone.utc) - ts > timedelta(days=ttl_days):
            return None
    try:
        aliases = json.loads(row["aliases_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(aliases, list):
        return None
    return [str(a) for a in aliases]


def alias_cache_put(
    conn: sqlite3.Connection,
    key: str,
    aliases: list[str],
    source: str,
) -> None:
    norm = _artist_key(key)
    payload = json.dumps(list(aliases), ensure_ascii=False)
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO artist_alias_cache
              (artist_key, aliases_json, source, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (norm, payload, source, utc_now_iso()),
        )


# ---------- playlist_snapshot ----------


def snapshot_get(
    conn: sqlite3.Connection, spotify_playlist_id: str
) -> dict[str, Any] | None:
    """Return parsed snapshot dict or None on miss / malformed payload.

    Shape: {"spotify_track_ids": [...], "qq_playlist_id": int,
            "qq_dirid": int, "synced_at": "..."}.
    """
    cur = conn.execute(
        "SELECT snapshot_json, qq_dirid, synced_at FROM playlist_snapshot "
        "WHERE spotify_playlist_id = ?",
        (spotify_playlist_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["snapshot_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    ids = payload.get("spotify_track_ids")
    if not isinstance(ids, list):
        return None
    payload["spotify_track_ids"] = [str(x) for x in ids]
    payload["qq_dirid"] = row["qq_dirid"]
    payload["synced_at"] = row["synced_at"]
    return payload


def snapshot_put(
    conn: sqlite3.Connection,
    spotify_playlist_id: str,
    track_ids: Iterable[str],
    qq_dirid: int,
) -> None:
    ids = [str(t) for t in track_ids]
    payload = json.dumps(
        {"spotify_track_ids": ids, "qq_playlist_id": qq_dirid},
        ensure_ascii=False,
    )
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO playlist_snapshot
              (spotify_playlist_id, snapshot_json, qq_dirid, synced_at)
            VALUES (?, ?, ?, ?)
            """,
            (spotify_playlist_id, payload, qq_dirid, utc_now_iso()),
        )


def snapshot_clear(conn: sqlite3.Connection, spotify_playlist_id: str) -> None:
    with conn:
        conn.execute(
            "DELETE FROM playlist_snapshot WHERE spotify_playlist_id = ?",
            (spotify_playlist_id,),
        )
