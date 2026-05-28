"""Incremental sync planner.

Diffs the current Spotify playlist against the previous snapshot so sync only
searches NEW tracks, deletes Spotify-removed tracks from QQ via cached IDs, and
reuses prior matches for unchanged tracks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import db


@dataclass
class IncrementalPlan:
    to_search: list[dict[str, Any]] = field(default_factory=list)
    to_remove_from_qq: list[tuple[int, int]] = field(default_factory=list)
    reused_matched: list[tuple[dict[str, Any], tuple[int, int]]] = field(
        default_factory=list
    )
    skipped_unmatched: list[dict[str, Any]] = field(default_factory=list)


def build_plan(
    conn: sqlite3.Connection,
    spotify_playlist_id: str,
    current_tracks: list[dict[str, Any]],
    qq_dirid: int,
    full: bool,
) -> IncrementalPlan:
    """Produce an incremental plan for the given playlist.

    - `full=True` forces a full re-search (snapshot ignored).
    - On snapshot miss, already cached matches are reused and prior unmatched
      tracks are skipped; only unknown tracks go to `to_search`.
    - Tracks kept between runs are resolved from `track_map_cache`; any that
      can't be resolved fall back to `to_search` unless they are known
      unmatched.
    """
    if full:
        return IncrementalPlan(
            to_search=list(current_tracks),
            to_remove_from_qq=[],
            reused_matched=[],
            skipped_unmatched=[],
        )

    current_by_id: dict[str, dict[str, Any]] = {}
    for t in current_tracks:
        tid = t.get("id")
        if tid is None:
            # Track with no Spotify id — can't dedupe, always search.
            continue
        current_by_id[str(tid)] = t

    current_ids = set(current_by_id.keys())
    snapshot = db.snapshot_get(conn, spotify_playlist_id)
    if snapshot is None:
        return _build_without_snapshot(conn, current_tracks, current_by_id, current_ids)

    last_ids: set[str] = set(snapshot.get("spotify_track_ids") or [])
    added_ids = current_ids - last_ids
    kept_ids = current_ids & last_ids
    removed_ids = last_ids - current_ids

    # Tracks without an `id` still need processing — search them.
    unkeyed = [t for t in current_tracks if t.get("id") is None]

    to_search: list[dict[str, Any]] = list(unkeyed)
    skipped_unmatched: list[dict[str, Any]] = []

    # Resolve current + removed via progress tables in one pass. This lets
    # interrupted runs reuse work even before a clean snapshot was committed.
    lookup_ids = list(current_ids | removed_ids)
    cache_rows = db.cache_get_many(conn, lookup_ids)
    unmatched_ids = db.unmatched_get_ids(conn, current_ids)

    reused_matched: list[tuple[dict[str, Any], tuple[int, int]]] = []
    for tid in _track_order(current_tracks, added_ids | kept_ids):
        pair = _trusted_qq_pair(cache_rows.get(tid))
        if pair is not None:
            reused_matched.append((current_by_id[tid], pair))
            continue
        if tid in unmatched_ids:
            skipped_unmatched.append(current_by_id[tid])
            continue
        if tid in added_ids or tid in kept_ids:
            to_search.append(current_by_id[tid])

    to_remove_from_qq: list[tuple[int, int]] = []
    for tid in removed_ids:
        pair = _trusted_qq_pair(cache_rows.get(tid))
        if pair is None:
            # No cached QQ id → nothing to delete on QQ side.
            continue
        to_remove_from_qq.append(pair)

    return IncrementalPlan(
        to_search=to_search,
        to_remove_from_qq=to_remove_from_qq,
        reused_matched=reused_matched,
        skipped_unmatched=skipped_unmatched,
    )


def commit_snapshot(
    conn: sqlite3.Connection,
    spotify_playlist_id: str,
    qq_dirid: int,
    current_tracks: list[dict[str, Any]],
) -> None:
    """Persist the current Spotify track-id set after a successful sync."""
    ids = [str(t["id"]) for t in current_tracks if t.get("id") is not None]
    db.snapshot_put(conn, spotify_playlist_id, ids, qq_dirid)


def _qq_pair(row: sqlite3.Row | None) -> tuple[int, int] | None:
    if row is None:
        return None
    qq_id = row["qq_song_id"]
    qq_type = row["qq_song_type"]
    if qq_id is None or qq_type is None:
        return None
    return (int(qq_id), int(qq_type))


def _cache_match_is_trusted(row: sqlite3.Row | None) -> bool:
    if row is None:
        return False
    method = str(row["match_method"] or "")
    parts = set(method.replace("title-only|", "").split("+"))
    return "isrc" in parts or "artist" in parts


def _trusted_qq_pair(row: sqlite3.Row | None) -> tuple[int, int] | None:
    if not _cache_match_is_trusted(row):
        return None
    return _qq_pair(row)


def _build_without_snapshot(
    conn: sqlite3.Connection,
    current_tracks: list[dict[str, Any]],
    current_by_id: dict[str, dict[str, Any]],
    current_ids: set[str],
) -> IncrementalPlan:
    cache_rows = db.cache_get_many(conn, current_ids)
    unmatched_ids = db.unmatched_get_ids(conn, current_ids)

    to_search: list[dict[str, Any]] = []
    reused_matched: list[tuple[dict[str, Any], tuple[int, int]]] = []
    skipped_unmatched: list[dict[str, Any]] = []

    for track in current_tracks:
        tid_raw = track.get("id")
        if tid_raw is None:
            to_search.append(track)
            continue
        tid = str(tid_raw)
        pair = _trusted_qq_pair(cache_rows.get(tid))
        if pair is not None:
            reused_matched.append((current_by_id[tid], pair))
        elif tid in unmatched_ids:
            skipped_unmatched.append(current_by_id[tid])
        else:
            to_search.append(current_by_id[tid])

    return IncrementalPlan(
        to_search=to_search,
        to_remove_from_qq=[],
        reused_matched=reused_matched,
        skipped_unmatched=skipped_unmatched,
    )


def _track_order(tracks: list[dict[str, Any]], ids: set[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for track in tracks:
        tid = track.get("id")
        if tid is None:
            continue
        key = str(tid)
        if key in ids and key not in seen:
            ordered.append(key)
            seen.add(key)
    return ordered
