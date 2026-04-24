from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import db, incremental


def _track(tid: str, title: str = "t", artist: str = "a") -> dict:
    return {
        "id": tid,
        "title": title,
        "artists": [artist],
        "album": "",
        "duration_ms": 0,
        "isrc": None,
    }


def _cache_row(sp_id: str, qq_id: int, qq_type: int = 0) -> dict:
    return {
        "spotify_track_id": sp_id,
        "spotify_title": "t",
        "spotify_artist": "a",
        "spotify_isrc": None,
        "qq_song_id": qq_id,
        "qq_song_mid": f"mid-{qq_id}",
        "qq_song_type": qq_type,
        "qq_title": "歌",
        "qq_artist": "手",
        "match_score": 0.9,
        "match_method": "title+artist",
    }


class TestBuildPlan(unittest.TestCase):
    def _conn(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        return conn

    def test_first_run_no_snapshot_searches_everything(self):
        conn = self._conn()
        tracks = [_track("s1"), _track("s2"), _track("s3")]

        plan = incremental.build_plan(
            conn, "pl1", tracks, qq_dirid=100, full=False
        )

        self.assertEqual(len(plan.to_search), 3)
        self.assertEqual(plan.to_remove_from_qq, [])
        self.assertEqual(plan.reused_matched, [])

    def test_incremental_add_only_searches_new(self):
        conn = self._conn()
        # Seed prior snapshot + cached matches for the original two tracks.
        db.cache_put(conn, _cache_row("s1", 11, 0))
        db.cache_put(conn, _cache_row("s2", 22, 0))
        db.snapshot_put(conn, "pl1", ["s1", "s2"], qq_dirid=100)

        current = [_track("s1"), _track("s2"), _track("s3"), _track("s4")]
        plan = incremental.build_plan(conn, "pl1", current, 100, full=False)

        new_ids = sorted(t["id"] for t in plan.to_search)
        self.assertEqual(new_ids, ["s3", "s4"])
        self.assertEqual(plan.to_remove_from_qq, [])

        reused_ids = sorted(t["id"] for t, _ in plan.reused_matched)
        self.assertEqual(reused_ids, ["s1", "s2"])
        reused_pairs = {t["id"]: pair for t, pair in plan.reused_matched}
        self.assertEqual(reused_pairs["s1"], (11, 0))
        self.assertEqual(reused_pairs["s2"], (22, 0))

    def test_incremental_remove_looks_up_qq_pair_from_cache(self):
        conn = self._conn()
        db.cache_put(conn, _cache_row("s1", 11, 0))
        db.cache_put(conn, _cache_row("s2", 22, 1))
        db.snapshot_put(conn, "pl1", ["s1", "s2"], qq_dirid=100)

        current = [_track("s1")]
        plan = incremental.build_plan(conn, "pl1", current, 100, full=False)

        self.assertEqual(plan.to_search, [])
        self.assertEqual(plan.to_remove_from_qq, [(22, 1)])
        self.assertEqual(len(plan.reused_matched), 1)
        self.assertEqual(plan.reused_matched[0][0]["id"], "s1")
        self.assertEqual(plan.reused_matched[0][1], (11, 0))

    def test_mixed_add_and_remove(self):
        conn = self._conn()
        db.cache_put(conn, _cache_row("s1", 11, 0))
        db.cache_put(conn, _cache_row("s2", 22, 1))
        db.cache_put(conn, _cache_row("s3", 33, 0))
        db.snapshot_put(conn, "pl1", ["s1", "s2", "s3"], qq_dirid=100)

        # Remove s2, keep s1+s3, add s4+s5.
        current = [_track("s1"), _track("s3"), _track("s4"), _track("s5")]
        plan = incremental.build_plan(conn, "pl1", current, 100, full=False)

        search_ids = sorted(t["id"] for t in plan.to_search)
        self.assertEqual(search_ids, ["s4", "s5"])
        self.assertEqual(plan.to_remove_from_qq, [(22, 1)])

        reused_ids = sorted(t["id"] for t, _ in plan.reused_matched)
        self.assertEqual(reused_ids, ["s1", "s3"])

    def test_full_flag_bypasses_snapshot(self):
        conn = self._conn()
        db.cache_put(conn, _cache_row("s1", 11, 0))
        db.snapshot_put(conn, "pl1", ["s1", "s2"], qq_dirid=100)

        current = [_track("s1"), _track("s3")]
        plan = incremental.build_plan(conn, "pl1", current, 100, full=True)

        self.assertEqual(len(plan.to_search), 2)
        self.assertEqual(plan.to_remove_from_qq, [])
        self.assertEqual(plan.reused_matched, [])

    def test_kept_track_missing_from_cache_falls_back_to_search(self):
        conn = self._conn()
        # Only s1 has a cached match; s2 is in snapshot but NOT in track_map_cache.
        db.cache_put(conn, _cache_row("s1", 11, 0))
        db.snapshot_put(conn, "pl1", ["s1", "s2"], qq_dirid=100)

        current = [_track("s1"), _track("s2")]
        plan = incremental.build_plan(conn, "pl1", current, 100, full=False)

        # s2 bumped into to_search because its qq pair isn't cached.
        search_ids = sorted(t["id"] for t in plan.to_search)
        self.assertEqual(search_ids, ["s2"])
        reused_ids = [t["id"] for t, _ in plan.reused_matched]
        self.assertEqual(reused_ids, ["s1"])
        self.assertEqual(plan.to_remove_from_qq, [])

    def test_removed_track_without_cache_is_skipped(self):
        # Spotify removed a track, but we never had a QQ match for it → nothing
        # to delete on QQ, quietly drop it.
        conn = self._conn()
        db.snapshot_put(conn, "pl1", ["s1", "s2"], qq_dirid=100)

        current = [_track("s1")]
        plan = incremental.build_plan(conn, "pl1", current, 100, full=False)

        self.assertEqual(plan.to_remove_from_qq, [])


class TestCommitSnapshot(unittest.TestCase):
    def test_snapshot_roundtrip(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        tracks = [_track("s1"), _track("s2"), _track("s3")]

        incremental.commit_snapshot(conn, "pl1", 100, tracks)

        snap = db.snapshot_get(conn, "pl1")
        self.assertIsNotNone(snap)
        self.assertEqual(sorted(snap["spotify_track_ids"]), ["s1", "s2", "s3"])
        self.assertEqual(snap["qq_dirid"], 100)
        self.assertIsNotNone(snap["synced_at"])

    def test_commit_overwrites_prior_snapshot(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        incremental.commit_snapshot(conn, "pl1", 100, [_track("s1")])
        incremental.commit_snapshot(conn, "pl1", 100, [_track("s2"), _track("s3")])

        snap = db.snapshot_get(conn, "pl1")
        self.assertEqual(sorted(snap["spotify_track_ids"]), ["s2", "s3"])

    def test_snapshot_clear_removes_entry(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        db.snapshot_put(conn, "pl1", ["s1"], qq_dirid=100)
        self.assertIsNotNone(db.snapshot_get(conn, "pl1"))
        db.snapshot_clear(conn, "pl1")
        self.assertIsNone(db.snapshot_get(conn, "pl1"))

    def test_commit_skips_tracks_without_id(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        tracks = [_track("s1"), {"id": None, "title": "t"}, _track("s2")]
        incremental.commit_snapshot(conn, "pl1", 100, tracks)

        snap = db.snapshot_get(conn, "pl1")
        self.assertEqual(sorted(snap["spotify_track_ids"]), ["s1", "s2"])


if __name__ == "__main__":
    unittest.main()
