"""Integration tests for the sync orchestrator.

Mocks both clients + credential loading + MusicBrainzClient at `src.sync_service`
import sites so no network or subprocess activity occurs.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src import sync_service as svc
from src.config import Config


def _cfg(tmpdir: str, threshold: float = 0.2) -> Config:
    return Config(
        spotify_client_id="cid",
        spotify_client_secret="csec",
        spotify_refresh_token="rtok",
        spotify_playlist_name="测试同步",
        qq_playlist_name="测试同步",
        qq_credential_json='{"musicid": 1, "musickey": "K"}',
        gh_pat_secrets_write=None,
        mirror_delete_threshold=threshold,
        db_path=os.path.join(tmpdir, "sync.db"),
        log_path=os.path.join(tmpdir, "sync.log"),
        unmatched_path=os.path.join(tmpdir, "unmatched.txt"),
        musicbrainz_user_agent="test-ua",
    )


def _sp_track(
    tid: str, title: str, artist: str, duration_ms: int = 200000, isrc: str | None = None
) -> dict:
    return {
        "id": tid,
        "title": title,
        "artists": [artist],
        "album": "Album",
        "duration_ms": duration_ms,
        "isrc": isrc,
    }


def _qq_cand(
    sid: int,
    title: str,
    artist: str,
    duration_s: int = 200,
    type_: int = 0,
    mid: str = "mid",
) -> dict:
    return {
        "id": sid,
        "mid": mid,
        "title": title,
        "artists": [artist],
        "album": "Album",
        "duration": duration_s,
        "type": type_,
    }


def _make_sp_mock(tracks: list[dict]) -> MagicMock:
    sp = MagicMock()
    sp.find_playlist_by_name.return_value = {"id": "pl1", "name": "测试同步"}
    sp.get_playlist_tracks.return_value = tracks
    return sp


def _make_qq_mock(
    current: list[dict],
    search_map: dict[str, list[dict]],
    dirid: int = 777,
) -> MagicMock:
    qq = MagicMock()
    qq.find_or_create_playlist.return_value = {"dirid": dirid, "dirname": "测试同步"}
    qq.get_playlist_songs.return_value = current
    qq.search_song.side_effect = lambda kw, num=10: search_map.get(kw, [])
    qq.add_songs.return_value = True
    qq.del_songs.return_value = True
    return qq


def _make_mb_mock(alias_map: dict[str, list[str]] | None = None) -> MagicMock:
    """Return a MagicMock replacing the `MusicBrainzClient` class.

    Calling it (constructing) yields an instance whose `get_aliases_batch`
    returns the provided `alias_map`. Default = empty dict (no aliases).
    """
    instance = MagicMock()
    instance.get_aliases_batch.return_value = alias_map or {}
    cls = MagicMock(return_value=instance)
    return cls, instance


class TestRunSync(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patches(
        self,
        sp_mock,
        qq_mock,
        rotated: bool = False,
        mb_alias_map: dict[str, list[str]] | None = None,
    ):
        """Stack all the patches the orchestrator needs."""
        credential = MagicMock()
        mb_cls, _mb_inst = _make_mb_mock(mb_alias_map)
        return [
            patch.object(svc, "SpotifyClient", return_value=sp_mock),
            patch.object(svc, "load_credential", return_value=credential),
            patch.object(svc, "ensure_fresh", return_value=(credential, rotated)),
            patch.object(svc, "dump_credential", return_value='{"new":"blob"}'),
            patch.object(svc, "QQClient", return_value=qq_mock),
            patch.object(svc, "MusicBrainzClient", mb_cls),
        ]

    def test_mirror_adds_new_tracks(self):
        sp_tracks = [
            _sp_track("sp1", "Song One", "Alice"),
            _sp_track("sp2", "Song Two", "Bob"),
        ]
        qq_current: list[dict] = []
        search_map = {
            "Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)],
            "Song Two Bob": [_qq_cand(102, "Song Two", "Bob", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        dirid_arg, pairs_arg = qq.add_songs.call_args.args
        self.assertEqual(dirid_arg, 777)
        self.assertEqual(sorted(pairs_arg), [(101, 0), (102, 0)])
        qq.del_songs.assert_not_called()

    def test_mirror_removes_stale_tracks(self):
        # Spotify has 1 track; QQ has 5 → remove 4 stale (4/5 = 80% > default 20%)
        # so raise threshold so the delete is allowed.
        sp_tracks = [_sp_track("sp1", "Keeper", "Alice")]
        qq_current = [
            _qq_cand(101, "Keeper", "Alice", 200, 0),
            _qq_cand(201, "Stale A", "X", 200, 0),
            _qq_cand(202, "Stale B", "Y", 200, 0),
            _qq_cand(203, "Stale C", "Z", 200, 0),
            _qq_cand(204, "Stale D", "W", 200, 0),
        ]
        search_map = {"Keeper Alice": [_qq_cand(101, "Keeper", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        cfg = _cfg(self.tmpdir, threshold=0.9)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_called_once()
        _, pairs_arg = qq.del_songs.call_args.args
        self.assertEqual({p[0] for p in pairs_arg}, {201, 202, 203, 204})

    def test_safety_threshold_aborts_mass_delete(self):
        sp_tracks = [_sp_track("sp1", "Keeper", "Alice")]
        qq_current = [
            _qq_cand(101, "Keeper", "Alice", 200, 0),
            _qq_cand(201, "Stale A", "X", 200, 0),
            _qq_cand(202, "Stale B", "Y", 200, 0),
            _qq_cand(203, "Stale C", "Z", 200, 0),
            _qq_cand(204, "Stale D", "W", 200, 0),
        ]
        search_map = {"Keeper Alice": [_qq_cand(101, "Keeper", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        cfg = _cfg(self.tmpdir, threshold=0.2)  # 4/5 = 80% > 20% → abort.
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 2)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_not_called()

    def test_dry_run_does_not_mutate(self):
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        qq_current: list[dict] = []
        search_map = {"Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock(qq_current, search_map)

        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=True)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()
        qq.del_songs.assert_not_called()

    def test_unmatched_track_lands_in_unmatched_txt(self):
        sp_tracks = [_sp_track("sp1", "Unfindable Track", "Nobody")]
        search_map = {"Unfindable Track Nobody": []}  # zero candidates
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        # No matches → no add/delete, run succeeds.
        self.assertEqual(rc, 0)
        qq.add_songs.assert_not_called()

        with open(cfg.unmatched_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Unfindable Track", content)
        self.assertIn("Nobody", content)

    def test_incremental_snapshot_skips_reused_tracks(self):
        """Second run should diff against snapshot: only new tracks get searched."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {"Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(qq_first.search_song.call_count, 1)

        # Second run: QQ playlist now contains the matched song. Incremental
        # snapshot → no new tracks to search.
        qq_second = _make_qq_mock(
            [_qq_cand(101, "Song One", "Alice", 200, 0)],
            search_map,
        )
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        # Snapshot hit → search_song should NOT have been called.
        qq_second.search_song.assert_not_called()
        qq_second.add_songs.assert_not_called()
        qq_second.del_songs.assert_not_called()

    def test_full_flag_bypasses_snapshot(self):
        """--full forces a re-search even when snapshot matches."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {"Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)]}
        sp = _make_sp_mock(sp_tracks)
        qq_first = _make_qq_mock([], search_map)

        cfg = _cfg(self.tmpdir)
        patches = self._patches(sp, qq_first)
        for p in patches:
            p.start()
        try:
            svc.run_sync(cfg, dry_run=False)
        finally:
            for p in patches:
                p.stop()

        # Second run with full=True — snapshot exists but should be ignored.
        qq_second = _make_qq_mock(
            [_qq_cand(101, "Song One", "Alice", 200, 0)],
            search_map,
        )
        patches = self._patches(sp, qq_second)
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(cfg, dry_run=False, full=True)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        # Full mode → every track gets re-searched.
        qq_second.search_song.assert_called_with("Song One Alice", num=10)
        self.assertEqual(qq_second.search_song.call_count, 1)

    def test_alias_retry_rescues_weak_primary_search(self):
        """When primary title+artist scores <0.8, alias-retry should salvage it."""
        # Spotify: romanized name "Jay Chou"; QQ catalog only has Chinese name.
        sp_tracks = [_sp_track("sp1", "晴天", "Jay Chou", duration_ms=269000)]
        # Primary search "晴天 Jay Chou" returns a candidate with wrong artist
        # so title+artist match is weak (title=0.4, artist=0, duration=0.4=0.8
        # if duration matches; fail below if it doesn't). Keep duration off to
        # force an alias retry path.
        primary_cand = _qq_cand(99, "晴天", "Unknown", duration_s=260, type_=0)
        # Alias retry "晴天 周杰伦" returns the right candidate — full match.
        alias_cand = _qq_cand(901, "晴天", "周杰伦", duration_s=269, type_=0)
        search_map = {
            "晴天 Jay Chou": [primary_cand],
            "晴天 周杰伦": [alias_cand],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        patches = self._patches(
            sp,
            qq,
            mb_alias_map={"Jay Chou": ["Jay Chou", "周杰伦"]},
        )
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        qq.add_songs.assert_called_once()
        _, pairs_arg = qq.add_songs.call_args.args
        self.assertEqual(pairs_arg, [(901, 0)])
        # Both primary and alias queries should have fired.
        called_queries = {c.args[0] for c in qq.search_song.call_args_list}
        self.assertIn("晴天 Jay Chou", called_queries)
        self.assertIn("晴天 周杰伦", called_queries)

    def test_alias_retry_skipped_when_primary_already_strong(self):
        """If primary search already scores ≥0.8, no alias retry fires."""
        sp_tracks = [_sp_track("sp1", "Song One", "Alice")]
        search_map = {
            "Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        patches = self._patches(
            sp,
            qq,
            mb_alias_map={"Alice": ["Alice", "Alice Smith"]},
        )
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        # Only one search call — the alias retry was skipped because the
        # primary match already cleared threshold.
        self.assertEqual(qq.search_song.call_count, 1)

    def test_mb_prefetch_called_with_to_search_artists(self):
        """MB batch is invoked with the to_search artist list."""
        sp_tracks = [
            _sp_track("sp1", "Song One", "Alice"),
            _sp_track("sp2", "Song Two", "Bob"),
        ]
        search_map = {
            "Song One Alice": [_qq_cand(101, "Song One", "Alice", 200, 0)],
            "Song Two Bob": [_qq_cand(102, "Song Two", "Bob", 200, 0)],
        }
        sp = _make_sp_mock(sp_tracks)
        qq = _make_qq_mock([], search_map)

        mb_cls, mb_inst = _make_mb_mock({})
        credential = MagicMock()
        patches = [
            patch.object(svc, "SpotifyClient", return_value=sp),
            patch.object(svc, "load_credential", return_value=credential),
            patch.object(svc, "ensure_fresh", return_value=(credential, False)),
            patch.object(svc, "dump_credential", return_value="{}"),
            patch.object(svc, "QQClient", return_value=qq),
            patch.object(svc, "MusicBrainzClient", mb_cls),
        ]
        for p in patches:
            p.start()
        try:
            rc = svc.run_sync(_cfg(self.tmpdir), dry_run=False)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(rc, 0)
        mb_inst.get_aliases_batch.assert_called_once()
        batch_arg = mb_inst.get_aliases_batch.call_args.args[0]
        artists = sorted(entry["artist"] for entry in batch_arg)
        self.assertEqual(artists, ["Alice", "Bob"])


if __name__ == "__main__":
    unittest.main()
