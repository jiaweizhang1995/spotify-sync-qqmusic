"""Unit tests for MusicBrainzClient + alias_cache helpers."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import db  # noqa: E402
from src.musicbrainz_client import (  # noqa: E402
    MIN_INTERVAL_SECONDS,
    MusicBrainzClient,
    _RateLimiter,
)

UA = "spotify-qq-sync/0.2 (test@example.com)"


# ---------- fixtures ----------


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _mock_response(status: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body or {}
    return resp


def _jay_chou_artist_detail() -> dict:
    return {
        "id": "mb-jay",
        "name": "Jay Chou",
        "sort-name": "Chou, Jay",
        "aliases": [
            {"name": "周杰伦", "sort-name": "周杰伦"},
            {"name": "周杰倫", "sort-name": "周杰倫"},
            {"name": "Jay Chou", "sort-name": "Chou, Jay"},  # duplicate
        ],
    }


# ---------- db.alias_cache_* ----------


class TestAliasCache:
    def test_schema_creates_alias_table(self):
        conn = _conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='artist_alias_cache'"
        ).fetchall()
        assert len(rows) == 1

    def test_put_then_get_roundtrip(self):
        conn = _conn()
        db.alias_cache_put(conn, "Jay Chou", ["Jay Chou", "周杰伦"], source="test")
        got = db.alias_cache_get(conn, "Jay Chou")
        assert got == ["Jay Chou", "周杰伦"]

    def test_key_is_normalized(self):
        conn = _conn()
        db.alias_cache_put(conn, "  Jay Chou  ", ["Jay Chou"], source="t")
        assert db.alias_cache_get(conn, "jay chou") == ["Jay Chou"]
        assert db.alias_cache_get(conn, "JAY CHOU") == ["Jay Chou"]

    def test_miss_returns_none(self):
        conn = _conn()
        assert db.alias_cache_get(conn, "nobody") is None

    def test_ttl_expiry(self):
        conn = _conn()
        # Write a stale row directly so we control the timestamp.
        old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn.execute(
            "INSERT INTO artist_alias_cache VALUES (?, ?, ?, ?)",
            ("stale", json.dumps(["x"]), "test", old),
        )
        conn.commit()
        # Default TTL is 30d → expired.
        assert db.alias_cache_get(conn, "stale") is None
        # With a longer TTL window, it's still valid.
        assert db.alias_cache_get(conn, "stale", ttl_days=60) == ["x"]


# ---------- _RateLimiter ----------


class TestRateLimiter:
    def test_single_thread_enforces_interval(self):
        lim = _RateLimiter(min_interval=0.05)
        start = time.monotonic()
        lim.acquire()
        lim.acquire()
        lim.acquire()
        elapsed = time.monotonic() - start
        # 3 acquires -> at least 2 * interval between them
        assert elapsed >= 2 * 0.05

    def test_multi_thread_serializes(self):
        lim = _RateLimiter(min_interval=0.05)
        N = 4
        timestamps: list[float] = []
        lock = threading.Lock()

        def worker():
            lim.acquire()
            with lock:
                timestamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(N)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - start
        # Across N threads we still need (N-1) gaps.
        assert elapsed >= (N - 1) * 0.05
        timestamps.sort()
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        for g in gaps:
            # tolerate tiny scheduling jitter
            assert g >= 0.05 - 0.005


# ---------- client behavior (mocked HTTP) ----------


class TestClientSingleLookup:
    @patch("src.musicbrainz_client.requests.get")
    def test_name_search_returns_aliases_and_caches(self, mock_get):
        search_hit = {"artists": [{"id": "mb-jay", "name": "Jay Chou"}]}
        mock_get.side_effect = [
            _mock_response(200, search_hit),
            _mock_response(200, _jay_chou_artist_detail()),
        ]
        client = MusicBrainzClient(UA, _conn())
        aliases = client.get_aliases_for_artist("Jay Chou")

        assert "Jay Chou" in aliases
        assert "周杰伦" in aliases
        assert "Chou, Jay" in aliases
        # case-insensitive dedupe → Jay Chou appears exactly once
        lowered = [a.lower() for a in aliases]
        assert len(lowered) == len(set(lowered))
        # 2 HTTP calls: search + detail
        assert mock_get.call_count == 2
        # UA header sent
        headers = mock_get.call_args_list[0].kwargs["headers"]
        assert headers["User-Agent"] == UA

    @patch("src.musicbrainz_client.requests.get")
    def test_second_call_hits_cache(self, mock_get):
        search_hit = {"artists": [{"id": "mb-jay"}]}
        mock_get.side_effect = [
            _mock_response(200, search_hit),
            _mock_response(200, _jay_chou_artist_detail()),
        ]
        client = MusicBrainzClient(UA, _conn())
        first = client.get_aliases_for_artist("Jay Chou")
        second = client.get_aliases_for_artist("Jay Chou")
        assert first == second
        # No extra HTTP calls the second time
        assert mock_get.call_count == 2

    @patch("src.musicbrainz_client.requests.get")
    def test_503_retries_once_then_degrades(self, mock_get):
        mock_get.side_effect = [
            _mock_response(503),
            _mock_response(503),
        ]
        client = MusicBrainzClient(UA, _conn())
        aliases = client.get_aliases_for_artist("Unknown Artist")
        assert aliases == ["Unknown Artist"]
        # Two attempts (initial + 1 retry)
        assert mock_get.call_count == 2

    @patch("src.musicbrainz_client.requests.get")
    def test_network_error_degrades_to_original(self, mock_get):
        import requests as _requests

        mock_get.side_effect = _requests.ConnectionError("boom")
        client = MusicBrainzClient(UA, _conn())
        aliases = client.get_aliases_for_artist("Someone")
        assert aliases == ["Someone"]
        # initial + 1 retry = 2
        assert mock_get.call_count == 2

    @patch("src.musicbrainz_client.requests.get")
    def test_empty_search_caches_fallback(self, mock_get):
        mock_get.return_value = _mock_response(200, {"artists": []})
        conn = _conn()
        client = MusicBrainzClient(UA, conn)
        aliases = client.get_aliases_for_artist("Nobody")
        assert aliases == ["Nobody"]
        # Cached → second call no HTTP
        mock_get.reset_mock()
        again = client.get_aliases_for_artist("Nobody")
        assert again == ["Nobody"]
        assert mock_get.call_count == 0


class TestClientIsrcPath:
    @patch("src.musicbrainz_client.requests.get")
    def test_isrc_extracts_artist_mbid(self, mock_get):
        isrc_hit = {
            "recordings": [
                {
                    "id": "rec1",
                    "artist-credit": [
                        {"artist": {"id": "mb-jay", "name": "Jay Chou"}}
                    ],
                }
            ]
        }
        mock_get.side_effect = [
            _mock_response(200, isrc_hit),
            _mock_response(200, _jay_chou_artist_detail()),
        ]
        client = MusicBrainzClient(UA, _conn())
        aliases = client.get_aliases_for_isrc("TWA121400001", "Jay Chou")

        # Should have hit ISRC endpoint first, then artist detail (NOT search).
        first_call_url = mock_get.call_args_list[0].args[0]
        assert "/isrc/TWA121400001" in first_call_url
        second_call_url = mock_get.call_args_list[1].args[0]
        assert "/artist/mb-jay" in second_call_url
        assert "周杰伦" in aliases

    @patch("src.musicbrainz_client.requests.get")
    def test_isrc_miss_falls_back_to_name_search(self, mock_get):
        # ISRC returns no recordings → client falls back to name search path.
        search_hit = {"artists": [{"id": "mb-jay"}]}
        mock_get.side_effect = [
            _mock_response(200, {"recordings": []}),
            _mock_response(200, search_hit),
            _mock_response(200, _jay_chou_artist_detail()),
        ]
        client = MusicBrainzClient(UA, _conn())
        aliases = client.get_aliases_for_isrc("BADISRC", "Jay Chou")
        assert "周杰伦" in aliases
        assert mock_get.call_count == 3


class TestClientBatch:
    @patch("src.musicbrainz_client.requests.get")
    def test_batch_dedupes_artists(self, mock_get):
        # Artist detail: Jay Chou + Fujii Kaze
        detail_jay = _jay_chou_artist_detail()
        detail_fujii = {
            "id": "mb-fujii",
            "name": "Fujii Kaze",
            "sort-name": "Fujii, Kaze",
            "aliases": [{"name": "藤井風", "sort-name": "藤井風"}],
        }

        def fake_get(url, headers=None, params=None, timeout=None):
            # search endpoints return a hit with the matching MBID
            if "/artist/mb-jay" in url:
                return _mock_response(200, detail_jay)
            if "/artist/mb-fujii" in url:
                return _mock_response(200, detail_fujii)
            if url.endswith("/artist"):
                q = (params or {}).get("query", "")
                if "Jay Chou" in q:
                    return _mock_response(200, {"artists": [{"id": "mb-jay"}]})
                if "Fujii Kaze" in q:
                    return _mock_response(200, {"artists": [{"id": "mb-fujii"}]})
            return _mock_response(404)

        mock_get.side_effect = fake_get

        client = MusicBrainzClient(UA, _conn(), max_workers=2)
        tracks = [
            {"artist": "Jay Chou", "isrc": None},
            {"artist": "Jay Chou", "isrc": None},  # dup
            {"artist": "Fujii Kaze", "isrc": None},
            {"artist": "", "isrc": None},  # skipped
        ]
        out = client.get_aliases_batch(tracks)

        assert set(out.keys()) == {"Jay Chou", "Fujii Kaze"}
        assert "周杰伦" in out["Jay Chou"]
        assert "藤井風" in out["Fujii Kaze"]
        # 2 search + 2 detail = 4 HTTP calls (dedupe drops the duplicate)
        assert mock_get.call_count == 4

    @patch("src.musicbrainz_client.requests.get")
    def test_batch_respects_global_rate_limit(self, mock_get):
        """Even with N threads, MB still sees ≤ 1 req/s."""
        # Four distinct artists → four name-search + four detail = 8 calls.
        details = {
            f"mb-{i}": {
                "id": f"mb-{i}",
                "name": f"Artist{i}",
                "sort-name": f"Artist{i}",
                "aliases": [],
            }
            for i in range(4)
        }

        def fake_get(url, headers=None, params=None, timeout=None):
            for mbid in details:
                if f"/artist/{mbid}" in url:
                    return _mock_response(200, details[mbid])
            # search — map query -> mbid
            q = (params or {}).get("query", "")
            for i in range(4):
                if f"Artist{i}" in q:
                    return _mock_response(200, {"artists": [{"id": f"mb-{i}"}]})
            return _mock_response(404)

        mock_get.side_effect = fake_get

        # Use a tiny interval so the test stays fast but still measurable.
        client = MusicBrainzClient(UA, _conn(), max_workers=4)
        client._limiter.min_interval = 0.05

        tracks = [{"artist": f"Artist{i}", "isrc": None} for i in range(4)]
        start = time.monotonic()
        out = client.get_aliases_batch(tracks, max_workers=4)
        elapsed = time.monotonic() - start

        assert len(out) == 4
        # 8 HTTP calls, each gated by 0.05s → elapsed ≥ 7 * 0.05 = 0.35s
        # (minus the very first call which starts at t=0).
        assert elapsed >= 7 * 0.05 - 0.02
        assert mock_get.call_count == 8


# ---------- module-level sanity ----------


def test_default_interval_matches_mb_policy():
    # Regression guard: MB policy is 1 req/s.
    assert MIN_INTERVAL_SECONDS == 1.0


def test_user_agent_required():
    with pytest.raises(ValueError):
        MusicBrainzClient("", _conn())
