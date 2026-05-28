"""One-shot reorder: rebuild the QQ playlist in Spotify added_at ascending order.

QQ Music's `added_at desc` view shows the most recently added song first, so
re-adding tracks in `added_at` ascending order makes the newest-added Spotify
track the freshest QQ entry — and thus the top of the user's playlist.

The QQ API exposes only `add_songs` / `del_songs` (no in-place reorder), so the
strategy is: clear the playlist, re-add resolved tracks in target order. Songs
without a `track_map_cache` mapping are skipped — run a normal sync first if
you want them populated.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from typing import Any

from . import db as dbm
from . import report
from .config import Config
from .qqmusic_client import QQClient, ensure_fresh, load_credential
from .spotify_client import SpotifyClient


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _qq_pair_from_cache(row: Any) -> tuple[int, int] | None:
    if row is None:
        return None
    qid = row["qq_song_id"]
    qtype = row["qq_song_type"]
    if qid is None or qtype is None:
        return None
    return (int(qid), int(qtype))


def _sort_key(track: dict[str, Any]) -> tuple[int, str, str]:
    """Sort by added_at asc; tracks with no added_at go first (treated as oldest).

    Tie-breaker uses the Spotify track id so order is stable across runs.
    """
    added_at = track.get("added_at") or ""
    has_added = 1 if added_at else 0
    return (has_added, added_at, str(track.get("id") or ""))


def run_reorder(cfg: Config, dry_run: bool = False) -> int:
    """Wipe + re-add the QQ playlist in Spotify added_at ascending order.

    Returns 0 on success / dry-run, 2 on safety abort, 1 on failure.
    """
    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)) or ".", exist_ok=True)

    conn = dbm.connect(cfg.db_path)
    dbm.init_schema(conn)
    run_id = dbm.insert_run(conn, {"status": "running", "notes": "reorder"})

    notes_parts: list[str] = ["op=reorder"]
    added_count = 0
    removed_count = 0
    skipped_count = 0
    failed_count = 0
    status = "failed"
    t_start = time.time()

    try:
        _log(f"[1/6] fetching Spotify playlist {cfg.spotify_playlist_name!r}...")
        sp = SpotifyClient(
            cfg.spotify_client_id,
            cfg.spotify_client_secret,
            cfg.spotify_refresh_token,
        )
        sp_playlist = sp.find_playlist_by_name(cfg.spotify_playlist_name)
        if not sp_playlist:
            raise RuntimeError(
                f"Spotify playlist not found: {cfg.spotify_playlist_name!r}"
            )
        sp_tracks = sp.get_playlist_tracks(str(sp_playlist["id"]))
        _log(f"      -> {len(sp_tracks)} Spotify tracks")
        notes_parts.append(f"spotify_count={len(sp_tracks)}")

        sp_tracks_sorted = sorted(sp_tracks, key=_sort_key)

        _log("[2/6] refreshing QQ credential...")
        credential = load_credential(cfg.qq_credential_json)
        credential, _rotated = ensure_fresh(credential)
        qq = QQClient(credential)

        _log(f"[3/6] resolving QQ playlist {cfg.qq_playlist_name!r}...")
        target = qq.find_or_create_playlist(cfg.qq_playlist_name)
        dirid = int(target["dirid"])
        qq_current = qq.get_playlist_songs(dirid)
        _log(f"      -> dirid={dirid}, current {len(qq_current)} songs")
        notes_parts.append(f"qq_count={len(qq_current)}")

        _log("[4/6] mapping Spotify tracks to cached QQ ids (asc by added_at)...")
        sp_ids = [str(t["id"]) for t in sp_tracks_sorted if t.get("id") is not None]
        cache_rows = dbm.cache_get_many(conn, sp_ids)

        ordered_pairs: list[tuple[int, int]] = []
        unmapped: list[dict[str, Any]] = []
        seen_qq: set[int] = set()
        for track in sp_tracks_sorted:
            tid = track.get("id")
            if tid is None:
                unmapped.append(track)
                continue
            pair = _qq_pair_from_cache(cache_rows.get(str(tid)))
            if pair is None:
                unmapped.append(track)
                continue
            if pair[0] in seen_qq:
                # Same QQ song mapped to multiple Spotify tracks — keep first.
                continue
            ordered_pairs.append(pair)
            seen_qq.add(pair[0])

        skipped_count = len(unmapped)
        notes_parts.append(f"mapped={len(ordered_pairs)} unmapped={len(unmapped)}")
        _log(
            f"      -> mapped {len(ordered_pairs)}, unmapped {len(unmapped)} "
            f"(no cached match — run `spotify-sync sync` first if needed)"
        )

        # Safety: don't wipe a populated QQ playlist if we can only re-add a
        # tiny fraction. Reuses MIRROR_DELETE_THRESHOLD as the loss budget.
        current_count = len(qq_current)
        if current_count == 0:
            safe = True
            safety_msg = "qq playlist empty"
        else:
            loss_ratio = max(0, current_count - len(ordered_pairs)) / current_count
            safe = loss_ratio <= cfg.mirror_delete_threshold
            safety_msg = (
                f"loss {loss_ratio:.1%} vs threshold "
                f"{cfg.mirror_delete_threshold:.1%}"
            )
        notes_parts.append(f"safety={safety_msg}")
        _log(f"      safety: {safety_msg}")

        qq_to_delete: list[tuple[int, int]] = [
            (int(s["id"]), int(s.get("type") or 0))
            for s in qq_current
            if s.get("id") is not None
        ]

        if dry_run:
            _log("[5/6] DRY RUN — preview only")
            _log(
                f"      would delete {len(qq_to_delete)} song(s), "
                f"re-add {len(ordered_pairs)} song(s) in added_at ascending order"
            )
            preview = sp_tracks_sorted[: min(5, len(sp_tracks_sorted))]
            for i, t in enumerate(preview, 1):
                first_artist = ""
                arts = t.get("artists") or []
                if arts:
                    first_artist = arts[0] if isinstance(arts[0], str) else (
                        arts[0].get("name", "") if isinstance(arts[0], dict) else ""
                    )
                _log(
                    f"      asc[{i}] added_at={t.get('added_at') or '<none>'} "
                    f"{first_artist} 《{t.get('title', '')}》"
                )
            notes_parts.append(
                f"dry_run: would_delete={len(qq_to_delete)} "
                f"would_add={len(ordered_pairs)}"
            )
            removed_count = len(qq_to_delete)
            added_count = len(ordered_pairs)
            status = "dry-run"
        elif not safe:
            _log("[5/6] ABORT — safety threshold exceeded")
            notes_parts.append("aborted: safety threshold exceeded")
            status = "aborted"
        elif not ordered_pairs:
            _log("[5/6] ABORT — no mappable tracks; refusing to wipe playlist")
            notes_parts.append("aborted: empty target")
            status = "aborted"
        else:
            _log(f"[5/6] deleting {len(qq_to_delete)} current QQ song(s)...")
            if qq_to_delete:
                ok = qq.del_songs(dirid, qq_to_delete)
                if not ok:
                    failed_count += len(qq_to_delete)
                    raise RuntimeError("del_songs returned non-success — aborting before re-add")
                removed_count = len(qq_to_delete)
                _log(f"      -> deleted {removed_count}")

            _log(f"      adding {len(ordered_pairs)} song(s) in target order...")
            ok = qq.add_songs(dirid, ordered_pairs)
            if ok:
                added_count = len(ordered_pairs)
                _log(f"      -> added {added_count}")
                status = "success"
            else:
                failed_count += len(ordered_pairs)
                _log("      -> FAILED: add_songs returned non-success")
                notes_parts.append("add_songs returned non-success")
                status = "failed"

        _log("[6/6] writing run log...")
        notes = "; ".join(notes_parts)
        dbm.finalize_run(
            conn,
            run_id,
            status=status,
            added_count=added_count,
            removed_count=removed_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            notes=notes,
        )
        summary = {
            "run_id": run_id,
            "status": status,
            "added_count": added_count,
            "removed_count": removed_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "notes": notes,
        }
        report.append_sync_log(cfg.log_path, summary)
        _log(f"done in {time.time() - t_start:.1f}s")
        report.print_summary(summary)

        if status in ("success", "dry-run"):
            return 0
        if status == "aborted":
            return 2
        return 1

    except Exception as exc:
        tb = traceback.format_exc()
        notes_parts.append(f"exception: {exc}")
        notes = "; ".join(notes_parts)
        dbm.finalize_run(
            conn,
            run_id,
            status="failed",
            added_count=added_count,
            removed_count=removed_count,
            skipped_count=skipped_count,
            failed_count=failed_count + 1,
            notes=notes,
        )
        summary = {
            "run_id": run_id,
            "status": "failed",
            "added_count": added_count,
            "removed_count": removed_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count + 1,
            "notes": notes,
        }
        report.append_sync_log(cfg.log_path, summary)
        report.print_summary(summary)
        print(tb)
        return 1
    finally:
        conn.close()


__all__ = ["run_reorder"]
