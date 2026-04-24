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


def build_plan(
    conn: sqlite3.Connection,
    spotify_playlist_id: str,
    current_tracks: list[dict[str, Any]],
    qq_dirid: int,
    full: bool,
) -> IncrementalPlan:
    """Produce an incremental plan for the given playlist.

    - `full=True` forces a full re-search (snapshot ignored).
    - On snapshot miss (first run), every track goes to `to_search`.
    - Tracks kept between runs are resolved from `track_map_cache`; any that
      can't be resolved fall back to `to_search` (partial cache state).
    """
    if full:
        return IncrementalPlan(
            to_search=list(current_tracks),
            to_remove_from_qq=[],
            reused_matched=[],
        )

    snapshot = db.snapshot_get(conn, spotify_playlist_id)
    if snapshot is None:
        return IncrementalPlan(
            to_search=list(current_tracks),
            to_remove_from_qq=[],
            reused_matched=[],
        )

    last_ids: set[str] = set(snapshot.get("spotify_track_ids") or [])
    current_by_id: dict[str, dict[str, Any]] = {}
    for t in current_tracks:
        tid = t.get("id")
        if tid is None:
            # Track with no Spotify id — can't dedupe, always search.
            continue
        current_by_id[str(tid)] = t

    current_ids = set(current_by_id.keys())
    added_ids = current_ids - last_ids
    kept_ids = current_ids & last_ids
    removed_ids = last_ids - current_ids

    # Tracks without an `id` still need processing — search them.
    unkeyed = [t for t in current_tracks if t.get("id") is None]

    to_search: list[dict[str, Any]] = list(unkeyed)
    for tid in added_ids:
        to_search.append(current_by_id[tid])

    # Resolve kept + removed via track_map_cache in one query.
    lookup_ids = list(kept_ids | removed_ids)
    cache_rows = db.cache_get_many(conn, lookup_ids)

    reused_matched: list[tuple[dict[str, Any], tuple[int, int]]] = []
    for tid in kept_ids:
        pair = _qq_pair(cache_rows.get(tid))
        if pair is None:
            # Partial cache state — fall back to re-searching.
            to_search.append(current_by_id[tid])
            continue
        reused_matched.append((current_by_id[tid], pair))

    to_remove_from_qq: list[tuple[int, int]] = []
    for tid in removed_ids:
        pair = _qq_pair(cache_rows.get(tid))
        if pair is None:
            # No cached QQ id → nothing to delete on QQ side.
            continue
        to_remove_from_qq.append(pair)

    return IncrementalPlan(
        to_search=to_search,
        to_remove_from_qq=to_remove_from_qq,
        reused_matched=reused_matched,
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
