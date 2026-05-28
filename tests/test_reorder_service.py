"""Tests for the reorder orchestrator."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import db
from src import reorder_service as svc
from src.config import Config


def _cfg(tmpdir: str, threshold: float = 0.2) -> Config:
    return Config(
        spotify_client_id="cid",
        spotify_client_secret="csec",
        spotify_refresh_token="rtok",
        spotify_playlist_name="测试",
        qq_playlist_name="测试",
        qq_credential_json='{"musicid": 1, "musickey": "K"}',
        gh_pat_secrets_write=None,
        mirror_delete_threshold=threshold,
        db_path=os.path.join(tmpdir, "sync.db"),
        log_path=os.path.join(tmpdir, "sync.log"),
        unmatched_path=os.path.join(tmpdir, "unmatched.txt"),
        musicbrainz_user_agent="test-ua",
    )


def _sp_track(tid: str, added_at: str | None) -> dict:
    return {
        "id": tid,
        "title": f"T-{tid}",
        "artists": ["A"],
        "album": "Alb",
        "duration_ms": 200000,
        "isrc": None,
        "added_at": added_at,
    }


def _qq_song(qid: int, type_: int = 0) -> dict:
    return {
        "id": qid,
        "mid": f"m{qid}",
        "title": f"S{qid}",
        "artists": ["A"],
        "duration": 200,
        "type": type_,
    }


def _seed_cache(conn, mapping: dict[str, tuple[int, int]]) -> None:
    for sp_id, (qq_id, qq_type) in mapping.items():
        db.cache_put(
            conn,
            {
                "spotify_track_id": sp_id,
                "spotify_title": f"T-{sp_id}",
                "spotify_artist": "A",
                "spotify_isrc": None,
                "qq_song_id": qq_id,
                "qq_song_mid": f"m{qq_id}",
                "qq_song_type": qq_type,
                "qq_title": f"S{qq_id}",
                "qq_artist": "A",
                "match_score": 1.0,
                "match_method": "isrc",
            },
        )


class TestSortKey(unittest.TestCase):
    def test_added_at_ascending_oldest_first(self):
        tracks = [
            _sp_track("c", "2024-03-01T00:00:00Z"),
            _sp_track("a", "2024-01-01T00:00:00Z"),
            _sp_track("b", "2024-02-01T00:00:00Z"),
        ]
        ordered = sorted(tracks, key=svc._sort_key)
        self.assertEqual([t["id"] for t in ordered], ["a", "b", "c"])

    def test_missing_added_at_sorts_first(self):
        tracks = [
            _sp_track("a", "2024-01-01T00:00:00Z"),
            _sp_track("nodate", None),
        ]
        ordered = sorted(tracks, key=svc._sort_key)
        self.assertEqual(ordered[0]["id"], "nodate")


class TestRunReorder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_sp(self, tracks: list[dict]) -> MagicMock:
        sp = MagicMock()
        sp.find_playlist_by_name.return_value = {"id": "pl1", "name": "测试"}
        sp.get_playlist_tracks.return_value = tracks
        return sp

    def _make_qq(self, current: list[dict]) -> MagicMock:
        qq = MagicMock()
        qq.find_or_create_playlist.return_value = {"dirid": 42, "dirname": "测试"}
        qq.get_playlist_songs.return_value = current
        qq.add_songs.return_value = True
        qq.del_songs.return_value = True
        return qq

    def _patches(self, sp_mock, qq_mock):
        cred = MagicMock()
        return [
            patch.object(svc, "SpotifyClient", return_value=sp_mock),
            patch.object(svc, "load_credential", return_value=cred),
            patch.object(svc, "ensure_fresh", return_value=(cred, False)),
            patch.object(svc, "QQClient", return_value=qq_mock),
        ]

    def _seed(self, cfg, mapping):
        conn = db.connect(cfg.db_path)
        db.init_schema(conn)
        _seed_cache(conn, mapping)
        conn.close()

    def test_happy_path_orders_by_added_at_asc(self):
        cfg = _cfg(self.tmpdir, threshold=0.5)
        self._seed(cfg, {"a": (101, 0), "b": (102, 0), "c": (103, 0)})

        # Spotify returns out-of-order; reorder must sort asc by added_at.
        sp_tracks = [
            _sp_track("c", "2024-03-01T00:00:00Z"),
            _sp_track("a", "2024-01-01T00:00:00Z"),
            _sp_track("b", "2024-02-01T00:00:00Z"),
        ]
        qq_current = [_qq_song(101), _qq_song(102), _qq_song(103)]

        sp = self._make_sp(sp_tracks)
        qq = self._make_qq(qq_current)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_reorder(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.del_songs.assert_called_once()
        qq.add_songs.assert_called_once()
        _, add_pairs = qq.add_songs.call_args.args
        self.assertEqual(add_pairs, [(101, 0), (102, 0), (103, 0)])

    def test_dry_run_makes_no_writes(self):
        cfg = _cfg(self.tmpdir, threshold=0.5)
        self._seed(cfg, {"a": (101, 0), "b": (102, 0)})

        sp_tracks = [
            _sp_track("a", "2024-01-01T00:00:00Z"),
            _sp_track("b", "2024-02-01T00:00:00Z"),
        ]
        qq_current = [_qq_song(101), _qq_song(102)]

        sp = self._make_sp(sp_tracks)
        qq = self._make_qq(qq_current)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_reorder(cfg, dry_run=True)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_not_called()

    def test_safety_threshold_blocks_excessive_loss(self):
        # 5 QQ songs, only 1 mapped → 80% loss > 20% threshold → abort.
        cfg = _cfg(self.tmpdir, threshold=0.2)
        self._seed(cfg, {"a": (101, 0)})

        sp_tracks = [_sp_track("a", "2024-01-01T00:00:00Z")]
        qq_current = [_qq_song(i) for i in (101, 201, 202, 203, 204)]

        sp = self._make_sp(sp_tracks)
        qq = self._make_qq(qq_current)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_reorder(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 2)
        qq.del_songs.assert_not_called()
        qq.add_songs.assert_not_called()

    def test_unmapped_tracks_skipped(self):
        # 'b' has no cache row → unmapped, skipped from re-add ordering.
        cfg = _cfg(self.tmpdir, threshold=0.5)
        self._seed(cfg, {"a": (101, 0), "c": (103, 0)})

        sp_tracks = [
            _sp_track("a", "2024-01-01T00:00:00Z"),
            _sp_track("b", "2024-02-01T00:00:00Z"),
            _sp_track("c", "2024-03-01T00:00:00Z"),
        ]
        qq_current = [_qq_song(101), _qq_song(103)]

        sp = self._make_sp(sp_tracks)
        qq = self._make_qq(qq_current)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_reorder(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        _, add_pairs = qq.add_songs.call_args.args
        self.assertEqual(add_pairs, [(101, 0), (103, 0)])

    def test_empty_mapping_aborts(self):
        cfg = _cfg(self.tmpdir, threshold=0.99)
        # No cache seed at all.
        sp_tracks = [_sp_track("a", "2024-01-01T00:00:00Z")]
        qq_current = [_qq_song(101)]

        sp = self._make_sp(sp_tracks)
        qq = self._make_qq(qq_current)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_reorder(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 2)
        qq.del_songs.assert_not_called()
        qq.add_songs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
