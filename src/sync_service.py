"""Orchestrator: Spotify -> QQ Music mirror sync.

Wires together the clients, matcher, diff engine, DB, and report writers
to execute the 9-step flow from the plan. Returns an exit code suitable
for `sys.exit`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import traceback
from typing import Any


def _log(msg: str) -> None:
    """Flush-safe progress line to stderr (doesn't pollute stdout summary)."""
    print(msg, file=sys.stderr, flush=True)

from . import db as dbm
from . import incremental
from . import report
from .config import Config
from .diff_engine import compute_mirror_diff, safety_check
from .matcher import normalize_artist, normalize_title, pick_best, score_candidate
from .qqmusic_client import (
    QQClient,
    dump_credential,
    ensure_fresh,
    load_credential,
)
from .spotify_client import SpotifyClient


def _primary_artist(track: dict[str, Any]) -> str:
    artists = track.get("artists") or []
    if not artists:
        return ""
    first = artists[0]
    if isinstance(first, dict):
        return first.get("name", "") or ""
    return str(first)


def _search_query(track: dict[str, Any]) -> str:
    title = track.get("title") or track.get("name") or ""
    artist = _primary_artist(track)
    return f"{title} {artist}".strip()


def _push_qq_secret_if_possible(new_blob: str, cfg: Config) -> tuple[bool, str]:
    """Push rotated `QQ_CREDENTIAL_JSON` to the GH secret. Never raise."""
    if not cfg.gh_pat_secrets_write:
        return False, "no PAT configured"
    if shutil.which("gh") is None:
        return False, "gh CLI not available"
    env = os.environ.copy()
    env["GH_TOKEN"] = cfg.gh_pat_secrets_write
    try:
        proc = subprocess.run(
            ["gh", "secret", "set", "QQ_CREDENTIAL_JSON", "--body", new_blob],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"gh invocation failed: {exc}"
    if proc.returncode != 0:
        return False, f"gh secret set rc={proc.returncode}: {proc.stderr.strip()}"
    return True, "secret rotated"


def _cache_row_for(
    sp_track: dict[str, Any],
    qq: dict[str, Any],
    score: float,
    method: str,
) -> dict[str, Any]:
    return {
        "spotify_track_id": sp_track.get("id"),
        "spotify_title": sp_track.get("title"),
        "spotify_artist": _primary_artist(sp_track),
        "spotify_isrc": sp_track.get("isrc"),
        "qq_song_id": qq.get("id"),
        "qq_song_mid": qq.get("mid"),
        "qq_song_type": qq.get("type"),
        "qq_title": qq.get("title"),
        "qq_artist": (qq.get("artists") or [""])[0],
        "match_score": score,
        "match_method": method,
    }


def _qq_pair_from_cache(row: Any) -> tuple[int, int] | None:
    song_id = row["qq_song_id"]
    song_type = row["qq_song_type"]
    if song_id is None or song_type is None:
        return None
    return (int(song_id), int(song_type))


def _match_title_only_fallback(
    track: dict[str, Any],
    primary_candidates: list[dict[str, Any]],
    qq: QQClient,
) -> tuple[dict[str, Any] | None, float, str]:
    """Try `title+artist` first; if < 0.8, retry with `title` alone.

    QQ's own index already contains Chinese-artist versions for most English
    Spotify artists. Re-searching with just the title surfaces those without
    needing any external alias database. ISRC + duration carry the match once
    the candidate set is broader.
    """
    best, best_score, best_method = pick_best(track, primary_candidates, threshold=0.8)
    if best is not None and best_score >= 0.8:
        return best, best_score, best_method

    overall_cand = best
    overall_score = best_score
    overall_method = best_method

    title = (track.get("title") or track.get("name") or "").strip()
    if not title:
        return None, overall_score, overall_method

    query = title
    _log(f"    [title-only] {query!r}")
    try:
        alt_cands = qq.search_song(query, num=10)
    except Exception as exc:  # pragma: no cover — defensive
        _log(f"    [title-only] search failed: {exc}")
        return (None if overall_score < 0.8 else overall_cand), overall_score, overall_method

    for cand in alt_cands:
        score, method = score_candidate(track, cand)
        if score > overall_score:
            overall_score = score
            overall_method = f"title-only|{method}"
            overall_cand = cand
            if score >= 1.0:
                return overall_cand, overall_score, overall_method

    if overall_cand is None or overall_score < 0.8:
        return None, overall_score, overall_method
    return overall_cand, overall_score, overall_method


def run_sync(cfg: Config, dry_run: bool = False, full: bool = False) -> int:
    """Execute one sync run. Returns 0 on success, non-zero on failure.

    `full=True` bypasses the incremental snapshot so every Spotify track is
    re-searched (useful after cache drift or manual QQ edits).
    """
    os.makedirs(os.path.dirname(os.path.abspath(cfg.db_path)) or ".", exist_ok=True)

    conn = dbm.connect(cfg.db_path)
    dbm.init_schema(conn)
    run_id = dbm.insert_run(conn, {"status": "running"})

    added_count = 0
    removed_count = 0
    skipped_count = 0
    failed_count = 0
    notes_parts: list[str] = []
    status = "failed"

    t_start = time.time()
    try:
        mode = "full" if full else "incremental"
        _log(f"[1/9] fetching Spotify playlist {cfg.spotify_playlist_name!r} (mode={mode})...")
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
        spotify_playlist_id = str(sp_playlist["id"])
        sp_tracks = sp.get_playlist_tracks(spotify_playlist_id)
        _log(f"      -> {len(sp_tracks)} tracks")
        notes_parts.append(f"spotify_count={len(sp_tracks)}")
        notes_parts.append(f"mode={mode}")

        _log("[2/9] refreshing QQ credential...")
        credential = load_credential(cfg.qq_credential_json)
        credential, rotated = ensure_fresh(credential)
        if rotated:
            _log("      -> rotated, persisting new musickey")
            new_blob = dump_credential(credential)
            ok, msg = _push_qq_secret_if_possible(new_blob, cfg)
            notes_parts.append(
                f"qq_credential_rotated={'pushed' if ok else 'local-only'} ({msg})"
            )
        else:
            _log("      -> still valid")
        qq = QQClient(credential)

        _log(f"[3/9] resolving QQ playlist {cfg.qq_playlist_name!r}...")
        target = qq.find_or_create_playlist(cfg.qq_playlist_name)
        dirid = int(target["dirid"])
        qq_current = qq.get_playlist_songs(dirid)
        _log(f"      -> dirid={dirid}, current {len(qq_current)} songs")
        notes_parts.append(f"qq_count={len(qq_current)}")

        _log(f"[4/9] building incremental plan (full={full})...")
        plan = incremental.build_plan(
            conn, spotify_playlist_id, sp_tracks, dirid, full
        )
        _log(
            f"      -> to_search {len(plan.to_search)}, reused {len(plan.reused_matched)}, "
            f"snapshot-removed {len(plan.to_remove_from_qq)}"
        )
        notes_parts.append(
            f"incremental: search={len(plan.to_search)} reused={len(plan.reused_matched)} "
            f"snap_del={len(plan.to_remove_from_qq)}"
        )

        _log(f"[5/9] matching {len(plan.to_search)} track(s) against QQ...")
        matched: list[tuple[dict[str, Any], tuple[int, int]]] = list(plan.reused_matched)
        unmatched_rows: list[dict[str, Any]] = []
        total = len(plan.to_search)
        searched = 0

        for idx, track in enumerate(plan.to_search, 1):
            sp_id = track.get("id")
            short = f"{track.get('title','')[:40]} — {_primary_artist(track)[:20]}"
            if not sp_id:
                skipped_count += 1
                _log(f"  [{idx}/{total}] SKIP (no id): {short}")
                continue

            query = _search_query(track)
            searched += 1
            try:
                candidates = qq.search_song(query, num=10)
            except Exception as exc:  # pragma: no cover — defensive
                failed_count += 1
                _log(f"  [{idx}/{total}] FAIL search: {short} ({exc})")
                unmatched_rows.append(
                    {
                        "spotify_track_id": sp_id,
                        "title": track.get("title", ""),
                        "artist": _primary_artist(track),
                        "album": track.get("album", ""),
                        "reason": f"search error: {exc}",
                    }
                )
                continue

            best, score, method = _match_title_only_fallback(track, candidates, qq)

            if best is None:
                _log(
                    f"  [{idx}/{total}] UNMATCHED: {short} "
                    f"(best={score:.2f}/{method})"
                )
                unmatched_rows.append(
                    {
                        "spotify_track_id": sp_id,
                        "title": track.get("title", ""),
                        "artist": _primary_artist(track),
                        "album": track.get("album", ""),
                        "reason": f"no candidate ≥0.8 (best={score:.2f}/{method})",
                    }
                )
                continue

            song_type = best.get("type")
            song_id = best.get("id")
            if song_id is None or song_type is None:
                _log(f"  [{idx}/{total}] bad candidate (no id/type): {short}")
                unmatched_rows.append(
                    {
                        "spotify_track_id": sp_id,
                        "title": track.get("title", ""),
                        "artist": _primary_artist(track),
                        "album": track.get("album", ""),
                        "reason": "matched candidate missing id/type",
                    }
                )
                continue

            dbm.cache_put(conn, _cache_row_for(track, best, score, method))
            matched.append((track, (int(song_id), int(song_type))))
            if idx % 10 == 0 or idx == total:
                _log(
                    f"  [{idx}/{total}] reused {len(plan.reused_matched)} / "
                    f"searched {searched} / unmatched {len(unmatched_rows)}"
                )

        _log(
            f"      -> matched {len(matched)} (reused {len(plan.reused_matched)} + "
            f"searched {searched - len(unmatched_rows) - failed_count}), "
            f"unmatched {len(unmatched_rows)}"
        )

        _log("[6/9] computing mirror diff...")
        target_qq_ids = {pair[0] for _, pair in matched}
        current_qq_ids = {int(s["id"]) for s in qq_current if s.get("id") is not None}
        diff = compute_mirror_diff(target_qq_ids, current_qq_ids)
        safe, safety_msg = safety_check(diff, len(current_qq_ids), cfg.mirror_delete_threshold)

        to_add_pairs = [pair for _, pair in matched if pair[0] in diff["to_add"]]
        # For removal we fuse snapshot-driven removals with mirror-diff removals.
        qq_id_to_type = {
            int(s["id"]): int(s.get("type") or 0)
            for s in qq_current
            if s.get("id") is not None
        }
        to_remove_pairs: list[tuple[int, int]] = [
            (qid, qq_id_to_type.get(qid, 0)) for qid in diff["to_remove"]
        ]
        # Snapshot-driven removals land here too — dedupe by qq_song_id.
        seen_rm_ids = {pair[0] for pair in to_remove_pairs}
        for pair in plan.to_remove_from_qq:
            if pair[0] in seen_rm_ids:
                continue
            to_remove_pairs.append(pair)
            seen_rm_ids.add(pair[0])

        _log(
            f"      -> add {len(to_add_pairs)}, remove {len(to_remove_pairs)} "
            f"(safety: {safety_msg})"
        )
        notes_parts.append(f"safety={safety_msg}")

        skipped_count += len(unmatched_rows)

        if dry_run:
            _log("[7/9] DRY RUN — skipping writes")
            notes_parts.append(
                f"dry_run: would add {len(to_add_pairs)}, remove {len(to_remove_pairs)}"
            )
            added_count = len(to_add_pairs)
            removed_count = len(to_remove_pairs)
            status = "dry-run"
        elif not safe:
            _log("[7/9] ABORT — safety threshold exceeded")
            notes_parts.append("aborted: safety threshold exceeded")
            status = "aborted"
        else:
            if to_add_pairs:
                _log(f"[7/9] adding {len(to_add_pairs)} songs to QQ playlist...")
                ok = qq.add_songs(dirid, to_add_pairs)
                if ok:
                    added_count = len(to_add_pairs)
                    _log(f"      -> added {added_count}")
                else:
                    failed_count += len(to_add_pairs)
                    _log(f"      -> FAILED: add_songs returned non-success")
                    notes_parts.append("add_songs returned non-success")
            if to_remove_pairs:
                _log(f"      removing {len(to_remove_pairs)} songs from QQ playlist...")
                ok = qq.del_songs(dirid, to_remove_pairs)
                if ok:
                    removed_count = len(to_remove_pairs)
                    _log(f"      -> removed {removed_count}")
                else:
                    failed_count += len(to_remove_pairs)
                    _log(f"      -> FAILED: del_songs returned non-success")
                    notes_parts.append("del_songs returned non-success")
            status = "failed" if failed_count else "success"

            if status == "success":
                # Persist the current Spotify id set only on a clean apply so
                # the next incremental run diffs against what actually shipped.
                try:
                    incremental.commit_snapshot(
                        conn, spotify_playlist_id, dirid, sp_tracks
                    )
                except Exception as exc:  # pragma: no cover — cache is best-effort
                    _log(f"      WARN: snapshot commit failed: {exc}")
                    notes_parts.append(f"snapshot_commit_error: {exc}")

        _log(f"[8/9] writing unmatched.txt ({len(unmatched_rows)} rows) + log...")
        dbm.insert_unmatched(conn, unmatched_rows)
        report.write_unmatched_txt(cfg.unmatched_path, unmatched_rows)

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
        _log(f"[9/9] done in {time.time() - t_start:.1f}s")
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


__all__ = ["run_sync", "normalize_title", "normalize_artist"]
