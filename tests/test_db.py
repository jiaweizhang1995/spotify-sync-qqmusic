from __future__ import annotations

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unittest

from src import db


class TestInit(unittest.TestCase):
    def test_schema_creates_tables(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        self.assertIsInstance(conn, sqlite3.Connection)
        self.assertIs(conn.row_factory, sqlite3.Row)
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in rows}
        self.assertEqual(
            names, {"track_map_cache", "sync_runs", "unmatched_tracks"}
        )

    def test_init_is_idempotent(self):
        conn = db.connect(":memory:")
        db.init_schema(conn)
        db.init_schema(conn)  # must not raise


class TestCache(unittest.TestCase):
    def _conn(self):
        c = db.connect(":memory:")
        db.init_schema(c)
        return c

    def test_put_and_get_roundtrip(self):
        conn = self._conn()
        row = {
            "spotify_track_id": "sp1",
            "spotify_title": "Song",
            "spotify_artist": "Artist",
            "spotify_isrc": "ISRC1",
            "qq_song_id": 123,
            "qq_song_mid": "mid1",
            "qq_song_type": 1,
            "qq_title": "歌曲",
            "qq_artist": "歌手",
            "match_score": 0.95,
            "match_method": "title+artist",
        }
        db.cache_put(conn, row)
        fetched = db.cache_get(conn, "sp1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["spotify_track_id"], "sp1")
        self.assertEqual(fetched["qq_song_id"], 123)
        self.assertEqual(fetched["qq_title"], "歌曲")
        self.assertAlmostEqual(fetched["match_score"], 0.95)
        self.assertIsNotNone(fetched["updated_at"])

    def test_put_replaces_existing(self):
        conn = self._conn()
        db.cache_put(conn, {"spotify_track_id": "sp1", "qq_song_id": 1, "match_score": 0.8})
        db.cache_put(conn, {"spotify_track_id": "sp1", "qq_song_id": 2, "match_score": 0.9})
        fetched = db.cache_get(conn, "sp1")
        self.assertEqual(fetched["qq_song_id"], 2)
        self.assertAlmostEqual(fetched["match_score"], 0.9)

    def test_get_many(self):
        conn = self._conn()
        for sid in ("a", "b", "c"):
            db.cache_put(conn, {"spotify_track_id": sid, "qq_song_id": ord(sid)})
        many = db.cache_get_many(conn, ["a", "c", "missing"])
        self.assertEqual(set(many.keys()), {"a", "c"})
        self.assertEqual(many["a"]["qq_song_id"], ord("a"))

    def test_get_many_empty(self):
        conn = self._conn()
        self.assertEqual(db.cache_get_many(conn, []), {})

    def test_cache_miss(self):
        conn = self._conn()
        self.assertIsNone(db.cache_get(conn, "nope"))


class TestSyncRuns(unittest.TestCase):
    def _conn(self):
        c = db.connect(":memory:")
        db.init_schema(c)
        return c

    def test_insert_and_finalize(self):
        conn = self._conn()
        run_id = db.insert_run(conn, {"status": "running"})
        self.assertIsInstance(run_id, int)
        self.assertGreater(run_id, 0)

        db.finalize_run(
            conn,
            run_id,
            status="success",
            added_count=3,
            removed_count=1,
            skipped_count=2,
            failed_count=0,
            notes="ok",
        )
        row = conn.execute(
            "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["added_count"], 3)
        self.assertEqual(row["removed_count"], 1)
        self.assertEqual(row["skipped_count"], 2)
        self.assertEqual(row["notes"], "ok")
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["finished_at"])

    def test_insert_run_defaults(self):
        conn = self._conn()
        run_id = db.insert_run(conn)
        row = conn.execute(
            "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertIsNotNone(row["started_at"])


class TestUnmatched(unittest.TestCase):
    def _conn(self):
        c = db.connect(":memory:")
        db.init_schema(c)
        return c

    def test_insert_and_select(self):
        conn = self._conn()
        n = db.insert_unmatched(
            conn,
            [
                {
                    "spotify_track_id": "sp1",
                    "title": "Song",
                    "artist": "Artist",
                    "album": "Album",
                    "reason": "no match",
                },
                {
                    "spotify_track_id": "sp2",
                    "title": "Other",
                    "artist": "X",
                    "album": None,
                    "reason": "low score",
                },
            ],
        )
        self.assertEqual(n, 2)
        rows = conn.execute(
            "SELECT * FROM unmatched_tracks ORDER BY spotify_track_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Song")
        self.assertEqual(rows[1]["reason"], "low score")
        self.assertIsNotNone(rows[0]["created_at"])

    def test_insert_empty_noop(self):
        conn = self._conn()
        self.assertEqual(db.insert_unmatched(conn, []), 0)


if __name__ == "__main__":
    unittest.main()
